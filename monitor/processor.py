import os
import csv
import time
import socket
import json
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from scipy.signal import welch, iirnotch, filtfilt


def frontal_band_asymmetry(results, power_key):
    """Natural log of right/left band power; unavailable inputs return None."""
    try:
        left = float(results["AF7"][power_key])
        right = float(results["AF8"][power_key])
    except (KeyError, TypeError, ValueError):
        return None
    if not (np.isfinite(left) and np.isfinite(right) and left > 0 and right > 0):
        return None
    # Use the same small-power floor as the existing FAA calculation.
    return float(np.log(max(right, 1e-12)) - np.log(max(left, 1e-12)))


class EEGProcessor:

    def __init__(self):
        # ====================================================
        # INTEGRATION
        # ====================================================

        self.integration_host = "127.0.0.1"
        self.integration_port = 15000

        self.integration_sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )

        # ====================================================
        # EEG PROCESSING
        # ====================================================

        self.fs = 256
        self.window_count = 0

        self.faa_smoothed = None
        self.faa_smoothing_factor = 0.2

        self.fba_smoothed = None
        self.fba_smoothing_factor = 0.2

        # 60 Hz notch filter
        self.b_notch, self.a_notch = iirnotch(
            w0=60,
            Q=30,
            fs=self.fs
        )

        self.first_plot = True

        # ====================================================
        # DATA LOGGING
        # ====================================================

        self.log_dir = os.environ.get("T4MH_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
        os.makedirs(self.log_dir, exist_ok=True)

        session_id = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        self.log_path = os.path.join(
            self.log_dir,
            f"muse_{session_id}.csv"
        )

        self.start_monotonic = time.monotonic()

        self.log_file = open(
            self.log_path,
            "w",
            newline=""
        )

        self.log_writer = csv.writer(
            self.log_file
        )

        self.log_writer.writerow([
            "unix_time_s",
            "elapsed_s",

            "TP9_alpha_power",
            "TP9_peak_alpha_hz",
            "TP9_beta_power",
            "TP9_peak_beta_hz",

            "AF7_alpha_power",
            "AF7_peak_alpha_hz",
            "AF7_beta_power",
            "AF7_peak_beta_hz",

            "AF8_alpha_power",
            "AF8_peak_alpha_hz",
            "AF8_beta_power",
            "AF8_peak_beta_hz",

            "TP10_alpha_power",
            "TP10_peak_alpha_hz",
            "TP10_beta_power",
            "TP10_peak_beta_hz",

            "faa",
            "faa_smoothed",
            "fba",
            "fba_smoothed",
        ])

        self.log_file.flush()

        print(
            f"[Muse] Logging processed EEG to: "
            f"{self.log_path}"
        )

    # ========================================================
    # PROCESS EEG WINDOW
    # ========================================================

    def process(self, eeg):

        self.window_count += 1

        print("\nProcessing window")

        results = {}

        # ====================================================
        # PROCESS EACH EEG CHANNEL
        # ====================================================

        for channel in eeg:

            data = eeg[channel]

            # -----------------------------------------------
            # Remove 60 Hz electrical noise
            # -----------------------------------------------

            data = filtfilt(
                self.b_notch,
                self.a_notch,
                data
            )

            # -----------------------------------------------
            # Welch PSD
            # -----------------------------------------------

            freqs, psd = welch(
                data,
                fs=self.fs,
                nperseg=self.fs
            )

            # -----------------------------------------------
            # Save one PSD plot as diagnostic
            # -----------------------------------------------

            if self.first_plot:

                plt.figure(
                    figsize=(8, 4)
                )

                plt.plot(
                    freqs,
                    psd
                )

                plt.xlim(
                    0,
                    70
                )

                plt.xlabel(
                    "Frequency (Hz)"
                )

                plt.title(
                    f"{channel} Power Spectrum"
                )

                plt.grid(True)
                plt.tight_layout()

                plt.savefig(
                    os.path.join(self.log_dir, "muse_psd.png")
                )

                plt.close()

                print(
                    "Saved PSD to psd.png"
                )

                self.first_plot = False

            # =================================================
            # ALPHA BAND
            # 8–13 Hz
            # =================================================

            alpha_mask = (
                (freqs >= 8)
                &
                (freqs <= 13)
            )

            alpha_freqs = (
                freqs[alpha_mask]
            )

            alpha_psd = (
                psd[alpha_mask]
            )

            peak_alpha_freq = (
                alpha_freqs[
                    np.argmax(
                        alpha_psd
                    )
                ]
            )

            alpha_power = np.sum(
                alpha_psd
            )

            # =================================================
            # BETA BAND
            # >13–30 Hz
            #
            # Alpha includes 13 Hz, so beta starts above 13 Hz
            # to avoid double-counting that frequency bin.
            # =================================================

            beta_mask = (
                (freqs > 13)
                &
                (freqs <= 30)
            )

            beta_freqs = (
                freqs[beta_mask]
            )

            beta_psd = (
                psd[beta_mask]
            )

            peak_beta_freq = (
                beta_freqs[
                    np.argmax(
                        beta_psd
                    )
                ]
            )

            beta_power = np.sum(
                beta_psd
            )

            # -----------------------------------------------
            # Signal standard deviation
            # -----------------------------------------------

            std = np.std(
                data
            )

            # -----------------------------------------------
            # Store channel results
            # -----------------------------------------------

            results[channel] = {
                "alpha_power": alpha_power,
                "peak_alpha": peak_alpha_freq,
                "beta_power": beta_power,
                "peak_beta": peak_beta_freq,
                "std": std,
            }

        # ====================================================
        # FRONTAL ALPHA ASYMMETRY
        #
        # FAA = ln(AF8 alpha) - ln(AF7 alpha)
        # ====================================================

        if (
            "AF7" in results
            and
            "AF8" in results
        ):

            faa = (
                np.log(
                    max(
                        results[
                            "AF8"
                        ][
                            "alpha_power"
                        ],
                        1e-12
                    )
                )
                -
                np.log(
                    max(
                        results[
                            "AF7"
                        ][
                            "alpha_power"
                        ],
                        1e-12
                    )
                )
            )

        else:

            faa = None

        # ====================================================
        # FAA SMOOTHING
        # ====================================================

        if faa is not None:

            if self.faa_smoothed is None:

                self.faa_smoothed = (
                    faa
                )

            else:

                self.faa_smoothed = (
                    self.faa_smoothing_factor
                    * faa
                    +
                    (
                        1
                        -
                        self.faa_smoothing_factor
                    )
                    *
                    self.faa_smoothed
                )

        # FBA uses the existing >13–30 Hz beta powers, right minus left.
        fba = frontal_band_asymmetry(results, "beta_power")
        if fba is None:
            self.fba_smoothed = None
        elif self.fba_smoothed is None:
            self.fba_smoothed = fba
        else:
            self.fba_smoothed = (
                self.fba_smoothing_factor * fba
                + (1 - self.fba_smoothing_factor) * self.fba_smoothed
            )

        # ====================================================
        # SAVE PROCESSED EEG DATA
        # ====================================================

        if faa is not None:

            unix_time = time.time()

            elapsed = (
                time.monotonic()
                -
                self.start_monotonic
            )

            def get_result(
                channel,
                field
            ):

                if channel not in results:
                    return ""

                return float(
                    results[
                        channel
                    ][
                        field
                    ]
                )

            self.log_writer.writerow([
                f"{unix_time:.6f}",
                f"{elapsed:.6f}",

                get_result(
                    "TP9",
                    "alpha_power"
                ),
                get_result(
                    "TP9",
                    "peak_alpha"
                ),
                get_result(
                    "TP9",
                    "beta_power"
                ),
                get_result(
                    "TP9",
                    "peak_beta"
                ),

                get_result(
                    "AF7",
                    "alpha_power"
                ),
                get_result(
                    "AF7",
                    "peak_alpha"
                ),
                get_result(
                    "AF7",
                    "beta_power"
                ),
                get_result(
                    "AF7",
                    "peak_beta"
                ),

                get_result(
                    "AF8",
                    "alpha_power"
                ),
                get_result(
                    "AF8",
                    "peak_alpha"
                ),
                get_result(
                    "AF8",
                    "beta_power"
                ),
                get_result(
                    "AF8",
                    "peak_beta"
                ),

                get_result(
                    "TP10",
                    "alpha_power"
                ),
                get_result(
                    "TP10",
                    "peak_alpha"
                ),
                get_result(
                    "TP10",
                    "beta_power"
                ),
                get_result(
                    "TP10",
                    "peak_beta"
                ),

                float(faa),
                float(
                    self.faa_smoothed
                ),
                "" if fba is None else float(fba),
                "" if self.fba_smoothed is None else float(self.fba_smoothed),
            ])

            self.log_file.flush()

        # ====================================================
        # PUBLISH TO INTEGRATION
        # ====================================================

        if faa is not None:

            message = {
                "sensor": "muse",
                "faa": float(faa),
                "faa_smoothed": float(
                    self.faa_smoothed
                ),
                "fba": fba,
                "fba_smoothed": self.fba_smoothed,
            }

            self.integration_sock.sendto(
                json.dumps(
                    message
                ).encode(
                    "utf-8"
                ),
                (
                    self.integration_host,
                    self.integration_port,
                )
            )

        # ====================================================
        # DASHBOARD
        # ====================================================

        if not results:
            return

        max_alpha = max(
            result[
                "alpha_power"
            ]
            for result
            in results.values()
        )

        if os.isatty(1):
            os.system(
                "clear"
            )

        print(
            "\n"
            +
            "=" * 72
        )

        print(
            "                       Muse EEG / FAA / FBA"
        )

        print(
            "=" * 72
        )

        # ----------------------------------------------------
        # EEG
        # ----------------------------------------------------

        print(
            "\nEEG"
        )

        print(
            "-" * 72
        )

        print(
            f"{'Channel':<8}"
            f"{'Quality':<10}"
            f"{'Alpha':>12}"
            f"{'α Peak':>10}"
            f"{'Beta':>12}"
            f"{'β Peak':>10}"
        )

        for channel in [
            "TP9",
            "AF7",
            "AF8",
            "TP10"
        ]:

            if channel in results:

                std = results[
                    channel
                ][
                    "std"
                ]

                if (
                    10
                    <= std
                    < 100
                ):

                    quality = "GOOD"

                elif (
                    100
                    <= std
                    < 200
                ):

                    quality = "FAIR"

                else:

                    quality = "POOR"

                alpha_power = results[
                    channel
                ][
                    "alpha_power"
                ]

                alpha_peak = results[
                    channel
                ][
                    "peak_alpha"
                ]

                beta_power = results[
                    channel
                ][
                    "beta_power"
                ]

                beta_peak = results[
                    channel
                ][
                    "peak_beta"
                ]

                print(
                    f"{channel:<8}"
                    f"{quality:<10}"
                    f"{alpha_power:>12.2f}"
                    f"{alpha_peak:>9.1f} "
                    f"{beta_power:>12.2f}"
                    f"{beta_peak:>9.1f}"
                )

        # ----------------------------------------------------
        # FAA
        # ----------------------------------------------------

        print(
            "\nFAA"
        )

        print(
            "-" * 72
        )

        if faa is not None:

            print(
                f"Current:  "
                f"{faa:+.3f}"
            )

            print(
                f"Smoothed: "
                f"{self.faa_smoothed:+.3f}"
            )

        else:

            print(
                "Waiting for AF7/AF8..."
            )

        print("\nFBA (>13–30 Hz)")
        print("-" * 72)
        if fba is not None:
            print(f"Current:  {fba:+.3f}")
            print(f"Smoothed: {self.fba_smoothed:+.3f}")
        else:
            print("Waiting for valid AF7/AF8 beta power...")

        # ----------------------------------------------------
        # SYSTEM
        # ----------------------------------------------------

        print(
            "\nSystem"
        )

        print(
            "-" * 72
        )

        print(
            f"{'Sample Rate':<20}"
            f"{self.fs} Hz"
        )

        print(
            f"{'Window Size':<20}"
            f"512 samples"
        )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        print(
            "\nStatus"
        )

        print(
            "-" * 72
        )

        print(
            "Streaming EEG"
        )

        print(
            f"Logging: "
            f"{self.log_path}"
        )

        print(
            "=" * 72
        )
