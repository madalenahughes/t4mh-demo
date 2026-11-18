import os
import csv
import time
import json
import socket
from datetime import datetime
from collections import deque

import numpy as np
from scipy.signal import find_peaks
from godirect import GoDirect


# ============================================================
# CONFIGURATION
# ============================================================

INTEGRATION_HOST = "127.0.0.1"
INTEGRATION_PORT = 15000

PERIOD_MS = 100
SAMPLE_RATE = 1000 / PERIOD_MS       # 10 Hz

WINDOW_SECONDS = 30

LOG_DIR = os.environ.get("T4MH_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
os.makedirs(LOG_DIR, exist_ok=True)


# ============================================================
# INTEGRATION SOCKET
# ============================================================

integration_sock = socket.socket(
    socket.AF_INET,
    socket.SOCK_DGRAM
)


# ============================================================
# BUFFERS
# ============================================================

force_buffer = deque(
    maxlen=int(
        WINDOW_SECONDS * SAMPLE_RATE
    )
)

time_buffer = deque(
    maxlen=int(
        WINDOW_SECONDS * SAMPLE_RATE
    )
)


# ============================================================
# DATA LOGGING
# ============================================================

session_id = datetime.now().strftime(
    "%Y%m%d_%H%M%S"
)

log_path = os.path.join(
    LOG_DIR,
    f"respiration_{session_id}.csv"
)

log_file = open(
    log_path,
    "w",
    newline=""
)

log_writer = csv.writer(
    log_file
)

log_writer.writerow([
    "unix_time_s",
    "elapsed_s",
    "force_n",
    "respiration_rate_bpm",
    "breaths_detected",
])

log_file.flush()

start_monotonic = time.monotonic()

print(
    f"[Respiration] Logging to: "
    f"{log_path}"
)


# ============================================================
# CONNECT TO GO DIRECT
# ============================================================

godirect = GoDirect(
    use_ble=True,
    use_usb=False
)

print(
    "Searching for Go Direct Respiration Belt..."
)

devices = godirect.list_devices()

if not devices:

    print(
        "No Go Direct devices found."
    )

    log_file.close()
    integration_sock.close()
    godirect.quit()

    raise SystemExit


device = devices[0]

print(
    f"Found: {device}"
)

print(
    "Opening device..."
)

device.open(
    auto_start=False
)

# Populate sensor metadata
device.list_sensors()

# Sensor 1 = Force
# Respiration rate is calculated from the force waveform.
device.enable_sensors([1])

enabled = device.get_enabled_sensors()

print(
    "\nEnabled sensors:"
)

for sensor in enabled:

    print(
        f"  "
        f"{sensor.sensor_description} "
        f"({sensor.sensor_units})"
    )


print(
    "\nStarting stream..."
)

device.start(
    period=PERIOD_MS
)


# ============================================================
# CURRENT VALUES
# ============================================================

latest = {
    "Force": None,
    "Respiration Rate": None,
    "Breaths Detected": 0,
}


def fmt(
    value,
    decimals=3
):

    if value is None:
        return "Waiting..."

    if not np.isfinite(value):
        return "Waiting..."

    return f"{value:.{decimals}f}"


# ============================================================
# RESPIRATION-RATE CALCULATION
# ============================================================

def calculate_respiration_rate():

    # Wait until at least 10 seconds of force data
    # have been collected.
    if len(force_buffer) < int(
        10 * SAMPLE_RATE
    ):

        return None, 0

    force_array = np.array(
        force_buffer,
        dtype=float
    )

    time_array = np.array(
        time_buffer,
        dtype=float
    )

    # Remove DC / baseline force.
    centered = (
        force_array
        -
        np.mean(force_array)
    )

    signal_range = np.ptp(
        centered
    )

    if signal_range <= 0:

        return None, 0

    # Adaptive prominence threshold.
    prominence = max(
        0.05,
        signal_range * 0.25
    )

    # At most one inhalation peak every 1.5 s.
    # Corresponds to approximately 40 breaths/min.
    minimum_peak_distance = int(
        SAMPLE_RATE * 1.5
    )

    peaks, _ = find_peaks(
        centered,
        distance=minimum_peak_distance,
        prominence=prominence
    )

    if len(peaks) < 2:

        return None, len(peaks)

    peak_times = (
        time_array[peaks]
    )

    intervals = np.diff(
        peak_times
    )

    # Physiologically reasonable interval:
    #
    # 1.5 s -> 40 breaths/min
    # 10 s  -> 6 breaths/min

    valid_intervals = intervals[
        (intervals >= 1.5)
        &
        (intervals <= 10.0)
    ]

    if len(valid_intervals) == 0:

        return None, len(peaks)

    # Median is more resistant to a single
    # incorrectly detected breath than mean.
    median_interval = np.median(
        valid_intervals
    )

    respiration_rate = (
        60.0
        /
        median_interval
    )

    return (
        respiration_rate,
        len(peaks)
    )


# ============================================================
# MAIN STREAMING LOOP
# ============================================================

try:

    while True:

        new_force_sample = False

        # ----------------------------------------------------
        # READ SENSOR
        # ----------------------------------------------------

        if device.read():

            for sensor in enabled:

                if (
                    sensor.sensor_description
                    ==
                    "Force"
                ):

                    force = sensor.value

                    if (
                        force is not None
                        and
                        np.isfinite(force)
                    ):

                        latest["Force"] = float(
                            force
                        )

                        sample_time = (
                            time.monotonic()
                        )

                        force_buffer.append(
                            float(force)
                        )

                        time_buffer.append(
                            sample_time
                        )

                        new_force_sample = True

        # ----------------------------------------------------
        # ONLY PROCESS / LOG WHEN A NEW FORCE SAMPLE ARRIVES
        # ----------------------------------------------------

        if new_force_sample:

            rate, breaths = (
                calculate_respiration_rate()
            )

            latest[
                "Respiration Rate"
            ] = rate

            latest[
                "Breaths Detected"
            ] = breaths

            # ------------------------------------------------
            # TIMESTAMPS
            # ------------------------------------------------

            unix_time = time.time()

            elapsed = (
                time.monotonic()
                -
                start_monotonic
            )

            # ------------------------------------------------
            # CSV LOGGING
            # ------------------------------------------------

            if rate is None:

                rate_for_csv = ""

            else:

                rate_for_csv = (
                    f"{float(rate):.6f}"
                )

            log_writer.writerow([
                f"{unix_time:.6f}",
                f"{elapsed:.6f}",
                f"{latest['Force']:.6f}",
                rate_for_csv,
                breaths,
            ])

            log_file.flush()

            # ------------------------------------------------
            # PUBLISH TO INTEGRATION
            # ------------------------------------------------

            message = {
                "sensor": "respiration",
                "force": latest["Force"],
                "rate": latest[
                    "Respiration Rate"
                ],
            }

            integration_sock.sendto(
                json.dumps(
                    message
                ).encode(
                    "utf-8"
                ),
                (
                    INTEGRATION_HOST,
                    INTEGRATION_PORT
                )
            )

            # ------------------------------------------------
            # DASHBOARD
            # ------------------------------------------------

            if os.isatty(1):
                os.system(
                    "clear"
                )

            print(
                "=" * 58
            )

            print(
                "GO DIRECT RESPIRATION"
            )

            print(
                "=" * 58
            )

            print(
                "\nRespiration"
            )

            print(
                "-" * 58
            )

            print(
                f"{'Force':<24}"
                f"{fmt(latest['Force']):>12} N"
            )

            print(
                f"{'Respiration Rate':<24}"
                f"{fmt(latest['Respiration Rate'], 1):>12} bpm"
            )

            print(
                f"{'Breaths Detected':<24}"
                f"{latest['Breaths Detected']:>12}"
            )

            print(
                "\nSystem"
            )

            print(
                "-" * 58
            )

            print(
                f"{'Sample Period':<24}"
                f"{PERIOD_MS} ms"
            )

            print(
                f"{'Analysis Window':<24}"
                f"{WINDOW_SECONDS} s"
            )

            print(
                f"{'Status':<24}"
                f"Streaming"
            )

            print(
                f"{'Logging':<24}"
                f"{os.path.basename(log_path)}"
            )

            print(
                "=" * 58
            )

        # Prevent unnecessary CPU usage while still
        # checking frequently enough for the 10 Hz sensor.
        time.sleep(
            0.01
        )


# ============================================================
# SHUTDOWN
# ============================================================

except KeyboardInterrupt:

    print(
        "\nStopping respiration stream..."
    )


finally:

    try:
        device.stop()

    except Exception:
        pass

    try:
        device.close()

    except EOFError:
        # Known BLE cleanup issue on this setup.
        pass

    except Exception:
        pass

    try:
        godirect.quit()

    except Exception:
        pass

    log_file.close()
    integration_sock.close()

    print(
        f"Respiration data saved to:\n"
        f"{log_path}"
    )
