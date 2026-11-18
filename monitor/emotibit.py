import socket
import time
import os
import json
import csv
import threading
from datetime import datetime


# ============================================================
# Network configuration
# ============================================================

HOST = "0.0.0.0"
PORT = 12346

INTEGRATION_HOST = "127.0.0.1"
INTEGRATION_PORT = 15000


# ============================================================
# Data configuration
# ============================================================

WANTED_TAGS = {
    "EA",
    "T1", "TH",
    "AX", "AY", "AZ",
    "GX", "GY", "GZ",
    "MX", "MY", "MZ",
}


# ============================================================
# Logging configuration
# ============================================================

LOG_DIR = os.environ.get("T4MH_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
os.makedirs(LOG_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

LOG_PATH = os.path.join(
    LOG_DIR,
    f"emotibit_{timestamp}.csv"
)

log_file = open(
    LOG_PATH,
    "w",
    newline=""
)

log_writer = csv.writer(log_file)

log_writer.writerow([
    "unix_time_s",
    "elapsed_s",
    "tag",
    "sample_index",
    "value",
])

log_file.flush()

experiment_start_monotonic = time.monotonic()


# ============================================================
# Shared state
# ============================================================

latest = {}
lock = threading.Lock()
running = True

integration_sock = socket.socket(
    socket.AF_INET,
    socket.SOCK_DGRAM
)


# ============================================================
# UDP receiver
# ============================================================

def receiver():

    global running

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_RCVBUF,
        1024 * 1024
    )

    sock.bind((HOST, PORT))
    sock.settimeout(0.5)

    while running:

        try:
            data, addr = sock.recvfrom(65535)

        except socket.timeout:
            continue

        text = data.decode(
            "utf-8",
            errors="ignore"
        ).strip()

        fields = text.split(",")

        try:

            n_samples = int(fields[2])
            tag = fields[3]

            if tag not in WANTED_TAGS:
                continue

            values = [
                float(x)
                for x in fields[6:6 + n_samples]
            ]

            if not values:
                continue

            # Timestamp packet arrival.
            #
            # Every sample contained in this UDP packet gets
            # the same packet-arrival timestamp. sample_index
            # preserves its order within the packet.

            unix_time = time.time()

            elapsed = (
                time.monotonic()
                - experiment_start_monotonic
            )

            # Preserve every sample in the packet.

            with lock:

                for sample_index, sample in enumerate(values):

                    log_writer.writerow([
                        f"{unix_time:.6f}",
                        f"{elapsed:.6f}",
                        tag,
                        sample_index,
                        sample,
                    ])

                # Dashboard/integration only need the newest
                # value from each channel.

                latest[tag] = values[-1]

            log_file.flush()

        except (ValueError, IndexError):
            pass

    sock.close()


# ============================================================
# Dashboard helpers
# ============================================================

def value(tag, decimals=3):

    with lock:
        val = latest.get(tag)

    if val is None:
        return "Waiting..."

    return f"{val:.{decimals}f}"


# ============================================================
# Integration publisher
# ============================================================

def temperature_f(tag, decimals=3):
    """Format Celsius as Fahrenheit for display only."""
    with lock:
        val = latest.get(tag)
    if val is None:
        return "Waiting..."
    return f"{(val * 9.0 / 5.0 + 32.0):.{decimals}f}"


def publish_to_integration():

    with lock:

        message = {
            "sensor": "emotibit",

            "eda": latest.get("EA"),

            "t1": latest.get("T1"),
            "th": latest.get("TH"),

            "ax": latest.get("AX"),
            "ay": latest.get("AY"),
            "az": latest.get("AZ"),

            "gx": latest.get("GX"),
            "gy": latest.get("GY"),
            "gz": latest.get("GZ"),

            "mx": latest.get("MX"),
            "my": latest.get("MY"),
            "mz": latest.get("MZ"),
        }

    integration_sock.sendto(
        json.dumps(message).encode("utf-8"),
        (INTEGRATION_HOST, INTEGRATION_PORT)
    )


# ============================================================
# Dashboard
# ============================================================

def draw_dashboard():

    if os.isatty(1):
        if os.isatty(1):
            os.system("clear")

    print("=" * 60)
    print("EMOTIBIT")
    print("=" * 60)

    print("\nEDA")
    print("-" * 60)

    print(
        f"EA                "
        f"{value('EA', 6):>15}"
    )

    print("\nTemperature")
    print("-" * 60)

    print(
        f"T1                "
        f"{temperature_f('T1'):>15} °F"
    )

    print(
        f"TH                "
        f"{temperature_f('TH'):>15} °F"
    )

    print("\nIMU")
    print("-" * 60)

    print(
        f"Accelerometer     "
        f"X {value('AX'):>8}   "
        f"Y {value('AY'):>8}   "
        f"Z {value('AZ'):>8}"
    )

    print(
        f"Gyroscope         "
        f"X {value('GX'):>8}   "
        f"Y {value('GY'):>8}   "
        f"Z {value('GZ'):>8}"
    )

    print(
        f"Magnetometer      "
        f"X {value('MX', 1):>8}   "
        f"Y {value('MY', 1):>8}   "
        f"Z {value('MZ', 1):>8}"
    )

    print("\nSystem")
    print("-" * 60)

    print(
        f"UDP Port          "
        f"{PORT}"
    )

    print(
        "Status            "
        "Streaming EmotiBit"
    )

    print(
        f"Data Log          "
        f"{os.path.basename(LOG_PATH)}"
    )

    print("=" * 60)


# ============================================================
# Start receiver
# ============================================================

rx_thread = threading.Thread(
    target=receiver,
    daemon=True
)

rx_thread.start()


# ============================================================
# Main loop
# ============================================================

try:

    while True:

        publish_to_integration()

        draw_dashboard()

        time.sleep(0.5)


except KeyboardInterrupt:

    print(
        "\nStopping EmotiBit stream..."
    )


finally:

    running = False

    rx_thread.join(timeout=1)

    integration_sock.close()

    log_file.flush()
    log_file.close()

    print(
        f"EmotiBit data saved to:"
        f"\n{LOG_PATH}"
    )

    print(
        "EmotiBit stream stopped."
    )
