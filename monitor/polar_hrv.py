#!/usr/bin/env python3
"""
polar_hrv.py
Connects to Polar H10, streams RR intervals, computes RMSSD and z-score.

Usage:
    - Import rmssd_z_stream() from another script (e.g. polar_run.py)
    - Or run this file directly to just print RMSSD z-scores.

Design:
    * First N seconds = baseline (collect RMSSD samples, no z-scores yet)
    * After baseline, emit (rmssd - mean) / std as an async generator.
"""

import asyncio
import math
import statistics
import time
from typing import AsyncGenerator, Optional, List, Tuple

from bleak import BleakError, BleakClient, BleakScanner

# BLE UUID for Heart Rate Measurement characteristic
HRM_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Name fragment we use to find the Polar H10
POLAR_NAME_FRAGMENT = "Polar H10"


def compute_rmssd(rr_ms_window: List[float]) -> Optional[float]:
    """
    Compute RMSSD (in seconds) from a list of RR intervals in milliseconds.
    Returns None if not enough beats.
    """
    if len(rr_ms_window) < 3:
        return None

    diffs = []
    for i in range(1, len(rr_ms_window)):
        diffs.append(rr_ms_window[i] - rr_ms_window[i - 1])

    if not diffs:
        return None

    mean_sq = sum(d * d for d in diffs) / len(diffs)
    rmssd_ms = math.sqrt(mean_sq)
    return rmssd_ms / 1000.0  # convert to seconds


async def find_polar_device(timeout: float = 15.0):
    """Scan for a Polar H10 and return the device object."""
    print("rmssd_z_stream: scanning for Polar H10...")
    device = None

    def detection_callback(d, ad_data):
        nonlocal device
        # Try to be a little flexible about name
        name = d.name or ""
        if POLAR_NAME_FRAGMENT in name:
            device = d

    scanner = BleakScanner(detection_callback)
    await scanner.start()

    start = time.time()
    try:
        while device is None and (time.time() - start) < timeout:
            await asyncio.sleep(0.2)
    finally:
        await scanner.stop()

    if device is None:
        raise RuntimeError("Could not find Polar H10 during scan.")

    print(f"rmssd_z_stream: found device {device.address} ({device.name})")
    return device


async def _legacy_rmssd_z_stream(
    baseline_duration_s: float = 60.0,
    rr_window_size: int = 20,
    *,
    include_baseline: bool = False,
) -> AsyncGenerator[Tuple[float, ...], None]:
    """
    Yield (rmssd_ms, z) by default, preserving existing callers.
    With include_baseline=True, yield
    (rmssd_ms, z, baseline_rmssd_ms, delta_rmssd_ms).
    The fixed baseline mean and delta use milliseconds; delta = current - mean.

    - baseline_duration_s: how long to collect RMSSD baseline (seconds)
    - rr_window_size: number of RR intervals to use for each RMSSD
    """
    print("rmssd_z_stream: starting new HRV stream")

    device = await find_polar_device()
    device_address = device.address
    print(f"rmssd_z_stream: using device address {device_address}")

    # Queue where handler pushes z-scores after baseline
    queue: asyncio.Queue[Tuple[float, ...]] = asyncio.Queue()

    # State inside handler
    rr_ms_window: List[float] = []
    baseline_rmssds: List[float] = []
    baseline_done = False
    base_mean = 0.0
    base_std = 1.0
    baseline_start = None  # Start at the first usable RMSSD, not before BLE setup.

    def hrm_notification_handler(_sender, data: bytearray):
        nonlocal baseline_done, base_mean, base_std, baseline_start, rr_ms_window

        # Parse the Heart Rate Measurement characteristic
        # data format: flags (1 byte), then HR, then optional RR-intervals (2 bytes each)
        if len(data) < 3:
            return

        flags = data[0]
        # Bit 4 (0x10) indicates RR-interval presence
        rr_present = bool(flags & 0x10)

        if not rr_present:
            return

        # RR intervals are in the last part of the payload, units of 1/1024 s
        # We convert to ms.
        rr_values_ms = []
        # RR intervals start after HR bytes (which are 1 or 2 bytes depending on flags)
        hr_is_uint16 = bool(flags & 0x01)
        idx = 3 if hr_is_uint16 else 2

        while idx + 1 < len(data):
            rr_raw = int.from_bytes(data[idx:idx + 2], byteorder="little")
            rr_ms = (rr_raw / 1024.0) * 1000.0
            rr_values_ms.append(rr_ms)
            idx += 2

        if not rr_values_ms:
            return

        # Keep most recent rr_window_size intervals
        rr_ms_window.extend(rr_values_ms)
        if len(rr_ms_window) > rr_window_size:
            rr_ms_window[:] = rr_ms_window[-rr_window_size:]

        rmssd = compute_rmssd(rr_ms_window)
        if rmssd is None:
            return

        # Monitoring can stream immediately without inventing reference statistics.
        if baseline_duration_s <= 0:
            queue.put_nowait((rmssd * 1000.0, None, None, None))
            return

        now = time.monotonic()
        if baseline_start is None:
            baseline_start = now
        elapsed = now - baseline_start

        if not baseline_done:
            # Still in baseline period: collect RMSSD values but do not emit z yet
            baseline_rmssds.append(rmssd)
            if elapsed >= baseline_duration_s and len(baseline_rmssds) >= 3:
                base_mean = statistics.mean(baseline_rmssds)
                # protect against zero std
                base_std = statistics.pstdev(baseline_rmssds) or 0.01

                print(
                    f"Baseline complete ({int(elapsed)} s): "
                    f"RMSSD mean = {base_mean * 1000:.1f} ms, "
                    f"std = {base_std * 1000:.1f} ms"
                )
                baseline_done = True
            # Do NOT emit z-scores yet during baseline
            return

        # --- post-baseline: emit z-scores ---
        z = (rmssd - base_mean) / base_std
        try:
             queue.put_nowait((
                 rmssd * 1000.0,
                 z,
                 base_mean * 1000.0,
                 (rmssd - base_mean) * 1000.0,
             ))
        except asyncio.QueueFull:
            # if for some reason consumer is slow, just drop the sample
            pass

    # Single BLE connection per rmssd_z_stream() call
    try:
        # Give BlueZ a bit more time to finish service discovery
        async with BleakClient(device_address, timeout=30.0) as client:
            print(f"Connected to {device_address}")
            await client.start_notify(HRM_CHAR_UUID, hrm_notification_handler)

            try:
                while True:
                    sample = await queue.get()
                    yield sample if include_baseline else sample[:2]
            except asyncio.CancelledError:
                # Propagate cancellation upwards, but still run cleanup in finally
                print("rmssd_z_stream: cancelled.")
                raise
            finally:
                print("Stopping HRM notifications…")
                try:
                    await client.stop_notify(HRM_CHAR_UUID)
                except BleakError as e:
                    print(f"stop_notify skipped: {e}")
    except TimeoutError:
        print("rmssd_z_stream: connection to Polar H10 timed out (service discovery).")
        return
    except BleakError as e:
        print(f"rmssd_z_stream: BLE error during connect: {e}")
        return


async def rmssd_z_stream(baseline_duration_s=60.0, rr_window_size=20, *, include_baseline=False):
    """Monitoring without a baseline shares the music demo's actual RR pipeline."""
    if baseline_duration_s > 0:
        stream = _legacy_rmssd_z_stream(baseline_duration_s, rr_window_size,
                                        include_baseline=include_baseline)
        try:
            async for sample in stream:
                yield sample
        finally:
            await stream.aclose()
        return
    import importlib.util
    import sys
    from pathlib import Path
    name = "t4mh_shared_rr_processing"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "music" / "polar_hrv.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    shared = sys.modules[name]
    stream = shared.rmssd_stream(baseline_duration_s=None, rr_window_size=rr_window_size)
    try:
        async for sample in stream:
            values = (sample.rmssd_ms, None, None, None)
            yield values if include_baseline else values[:2]
    finally:
        await stream.aclose()


# Optional: simple test harness if you run this file directly
if __name__ == "__main__":
    async def _test():
        async for rmssd_ms, z, baseline_ms, delta_ms in rmssd_z_stream(
            baseline_duration_s=20.0, include_baseline=True
        ):
            print(f"RMSSD={rmssd_ms:.2f} ms; baseline={baseline_ms:.2f} ms; "
                  f"change={delta_ms:+.2f} ms; z={z:+.2f}")

    try:
        asyncio.run(_test())
    except KeyboardInterrupt:
        print("\nStopped by user.")
