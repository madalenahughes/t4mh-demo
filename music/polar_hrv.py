#!/usr/bin/env python3
"""Polar H10 RMSSD in milliseconds, with a fixed personal baseline.

Run directly to inspect readings; run polar_run.py for music.
Artifact thresholds are conservative demo heuristics, not clinical validation.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
import math
import statistics
import time
from typing import Optional

HRM_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
POLAR_NAME_FRAGMENT = "Polar H10"


@dataclass(frozen=True)
class HRVSample:
    t_monotonic: float
    rmssd_ms: float
    baseline_ms: Optional[float]
    rejected_rr: int


def compute_rmssd(rr_ms_window):
    """RMSSD in ms; None marks a rejected beat, never bridge across it."""
    diffs = [b - a for a, b in zip(rr_ms_window, rr_ms_window[1:])
             if a is not None and b is not None]
    if len(diffs) < 2:
        return None
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def parse_rr_ms(data):
    """Decode HRM flags, including uint16 HR and optional energy expended."""
    if not data or not data[0] & 0x10:
        return []
    flags = data[0]
    # Explicitly reported poor sensor contact: discard packet.
    if flags & 0x04 and not flags & 0x02:
        return []
    idx = 1 + (2 if flags & 0x01 else 1) + (2 if flags & 0x08 else 0)
    if len(data) <= idx or (len(data) - idx) % 2:
        return []
    return [int.from_bytes(data[i:i + 2], "little") * 1000.0 / 1024.0
            for i in range(idx, len(data), 2)]


class RMSSDTracker:
    def __init__(self, baseline_duration_s=30.0, rr_window_size=20):
        if (baseline_duration_s is not None and baseline_duration_s <= 0) or rr_window_size < 5:
            raise ValueError("Positive baseline duration and at least 5 RR slots required")
        self.duration = baseline_duration_s
        self.window = deque(maxlen=rr_window_size)
        self.history = deque(maxlen=9)
        self.baseline_values = []
        self.baseline_ms = None
        self.baseline_elapsed = 0.0
        self.last_valid = None
        self.last_packet = None
        self.rejected = 0
        self.reject_run = 0

    def feed(self, rr_values, now):
        if not rr_values:
            return None
        if self.last_packet is not None and now - self.last_packet > 5.0:
            self.window.clear()
            self.history.clear()
            self.last_valid = None
            if self.baseline_ms is None:
                self.baseline_values.clear()
                self.baseline_elapsed = 0.0
        self.last_packet = now
        for rr in rr_values:
            median = statistics.median(self.history) if self.history else rr
            valid = (math.isfinite(rr) and 300 <= rr <= 2000 and
                     (len(self.history) < 3 or abs(rr - median) <= max(200, 0.25 * median)))
            if valid:
                self.window.append(rr)
                self.history.append(rr)
                self.reject_run = 0
            else:
                self.rejected += 1
                self.reject_run += 1
                self.window.append(None)
                if self.reject_run >= 3:
                    # Reacquire after sustained changes; require a fresh full window.
                    self.history.clear()
                    self.window.clear()
                    self.reject_run = 0
        values = list(self.window)
        good_pairs = sum(a is not None and b is not None
                         for a, b in zip(values, values[1:]))
        if len(values) < self.window.maxlen or good_pairs < 0.8 * (len(values) - 1):
            self.last_valid = None
            return None
        rmssd = compute_rmssd(values)
        if rmssd is None:
            return None
        if self.duration is not None and self.baseline_ms is None:
            if self.last_valid is not None:
                self.baseline_elapsed += min(max(now - self.last_valid, 0), 2.0)
            self.baseline_values.append(rmssd)
            if self.baseline_elapsed >= self.duration and len(self.baseline_values) >= 3:
                self.baseline_ms = statistics.median(self.baseline_values)
                print(f"Baseline complete: {self.baseline_ms:.1f} ms "
                      f"({self.baseline_elapsed:.1f} valid seconds)")
        self.last_valid = now
        return HRVSample(now, rmssd, self.baseline_ms, self.rejected)


async def find_polar_device(timeout=15.0):
    from bleak import BleakScanner
    print("Scanning for Polar H10...")
    device = None

    def detected(d, ad):
        nonlocal device
        if POLAR_NAME_FRAGMENT in (ad.local_name or d.name or ""):
            device = d

    async with BleakScanner(detection_callback=detected):
        start = time.monotonic()
        while device is None and time.monotonic() - start < timeout:
            await asyncio.sleep(0.2)
    if device is None:
        raise RuntimeError("Could not find Polar H10; use --simulate explicitly for a rehearsal.")
    return device


async def rmssd_stream(baseline_duration_s=30.0, rr_window_size=20):
    from bleak import BleakClient, BleakError
    device = await find_polar_device()
    tracker = RMSSDTracker(baseline_duration_s, rr_window_size)
    queue = asyncio.Queue(maxsize=1)

    def notified(_sender, data):
        sample = tracker.feed(parse_rr_ms(data), time.monotonic())
        if sample is not None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(sample)

    async with BleakClient(device, timeout=30.0) as client:
        print("Connected. Keep still for RR window warm-up; monitoring has no baseline." if baseline_duration_s is None else
              "Connected. Keep still for window warm-up and baseline; music is off.")
        await client.start_notify(HRM_CHAR_UUID, notified)
        try:
            last_valid = time.monotonic()
            while True:
                try:
                    sample = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if not client.is_connected or time.monotonic() - last_valid > 40:
                        raise RuntimeError("Polar disconnected or no usable RR window for 40 seconds.")
                    continue
                last_valid = time.monotonic()
                yield sample
        finally:
            try:
                await client.stop_notify(HRM_CHAR_UUID)
            except BleakError:
                pass


if __name__ == "__main__":
    async def main():
        async for sample in rmssd_stream():
            baseline = "collecting" if sample.baseline_ms is None else f"{sample.baseline_ms:.1f} ms"
            print(f"RMSSD={sample.rmssd_ms:.1f} ms; baseline={baseline}; rejected={sample.rejected_rr}")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
