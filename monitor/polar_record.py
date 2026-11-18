#!/usr/bin/env python3

"""
polar_record.py

Polar H10 acquisition-only recorder for feedforward experiments.

- Uses the existing rmssd_z_stream() processing pipeline
- Does NOT control or start audio
- Publishes live RMSSD without a baseline wait; reference statistics are unavailable
- Saves timestamped RMSSD data to CSV
- Does NOT fall back to simulated data
"""

import asyncio
import csv
import json
import os
import socket
import time
from datetime import datetime

from polar_hrv import rmssd_z_stream


# ============================================================
# Configuration
# ============================================================

INTEGRATION_HOST = "127.0.0.1"
INTEGRATION_PORT = 15000

LOG_DIR = os.environ.get("T4MH_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
os.makedirs(LOG_DIR, exist_ok=True)


# ============================================================
# Integration socket
# ============================================================

integration_sock = socket.socket(
    socket.AF_INET,
    socket.SOCK_DGRAM
)


# ============================================================
# Integration publisher
# ============================================================

def publish_to_integration(rmssd_ms, rmssd_z, baseline_rmssd_ms, delta_rmssd_ms):

    message = {
        "sensor": "polar",
        "baseline_enabled": baseline_rmssd_ms is not None,
        "rmssd_ms": float(rmssd_ms),
        "baseline_rmssd_ms": (float(baseline_rmssd_ms) if baseline_rmssd_ms is not None else None),
        "delta_rmssd_ms": (float(delta_rmssd_ms) if delta_rmssd_ms is not None else None),
        "rmssd_z": (float(rmssd_z) if rmssd_z is not None else None),
    }

    integration_sock.sendto(
        json.dumps(message).encode("utf-8"),
        (INTEGRATION_HOST, INTEGRATION_PORT)
    )


# ============================================================
# Main recording session
# ============================================================

async def record():

    start_dt = datetime.now()
    start_monotonic = time.monotonic()

    session_id = start_dt.strftime("%Y%m%d_%H%M%S")

    log_path = os.path.join(
        LOG_DIR,
        f"polar_{session_id}.csv"
    )

    print("=" * 60)
    print("POLAR H10 RECORDING")
    print("=" * 60)
    print(f"Log: {log_path}")
    print("Waiting for Polar H10 data...")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    sample_count = 0

    with open(log_path, "w", newline="") as log_file:

        writer = csv.writer(log_file)

        writer.writerow([
            "unix_time_s",
            "elapsed_s",
            "rmssd_ms",
            "baseline_rmssd_ms",
            "delta_rmssd_ms",
            "rmssd_z",
        ])

        log_file.flush()

        async for result in rmssd_z_stream(baseline_duration_s=0, include_baseline=True):

            # All values come from the same fixed baseline in polar_hrv.py.
            rmssd_ms, rmssd_z, baseline_rmssd_ms, delta_rmssd_ms = result

            unix_time = time.time()

            elapsed = (
                time.monotonic()
                - start_monotonic
            )

            writer.writerow([
                f"{unix_time:.6f}",
                f"{elapsed:.6f}",
                f"{float(rmssd_ms):.6f}",
                "" if baseline_rmssd_ms is None else f"{float(baseline_rmssd_ms):.6f}",
                "" if delta_rmssd_ms is None else f"{float(delta_rmssd_ms):.6f}",
                "" if rmssd_z is None else f"{float(rmssd_z):.6f}",
            ])

            log_file.flush()

            publish_to_integration(
                rmssd_ms,
                rmssd_z,
                baseline_rmssd_ms,
                delta_rmssd_ms,
            )

            sample_count += 1

            print(f"\rt={elapsed:7.1f}s   RMSSD={float(rmssd_ms):7.2f} ms   n={sample_count}",
                  end="", flush=True)


    return log_path


# ============================================================
# Entry point
# ============================================================

async def main():

    log_path = None

    try:

        log_path = await record()

    except KeyboardInterrupt:

        print("\n\nRecording stopped by user.")

    finally:

        integration_sock.close()

        print("\nPolar recorder stopped.")

        if log_path is not None:
            print(f"Data saved to:\n{log_path}")


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
