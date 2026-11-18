#!/usr/bin/env python3
"""Bounded integral control from current minus baseline RMSSD, both in ms."""
from dataclasses import dataclass
import math


@dataclass
class AudioParams:
    tempo: float = 1.0
    pitch: float = -2.0  # Logged only by the VLC engine; no separate pitch DSP.
    volume: float = 60.0


TEMPO_MIN, TEMPO_MAX = 0.85, 1.15
PITCH_MIN, PITCH_MAX = -6.0, 0.0
VOLUME_MIN, VOLUME_MAX = 52.0, 68.0
# Per millisecond of RMSSD error per second. Higher RMSSD -> all decrease.
HRV_KI_TEMPO = 0.00015
HRV_KI_PITCH = 0.002
HRV_KI_VOLUME = 0.010


def update_audio_params(params, rmssd_ms, baseline_ms=None, *, dt_s=1.0,
                        is_baseline=False):
    if not math.isfinite(dt_s) or dt_s <= 0:
        return params
    dt = min(dt_s, 2.0)  # Never integrate an outage as sustained physiology.
    if is_baseline:
        return AudioParams()
    if (rmssd_ms is None or baseline_ms is None or
            not math.isfinite(rmssd_ms) or not math.isfinite(baseline_ms)):
        alpha = 1 - math.exp(-dt / 10.0)
        return AudioParams(*(old + alpha * (neutral - old) for old, neutral in
                             zip((params.tempo, params.pitch, params.volume), (1, -2, 60))))
    delta = rmssd_ms - baseline_ms
    # 2 ms deadband; cap very large deviations to limit slew.
    error = -math.copysign(min(max(abs(delta) - 2.0, 0.0), 50.0), delta)
    return AudioParams(
        max(TEMPO_MIN, min(TEMPO_MAX, params.tempo + HRV_KI_TEMPO * error * dt)),
        max(PITCH_MIN, min(PITCH_MAX, params.pitch + HRV_KI_PITCH * error * dt)),
        max(VOLUME_MIN, min(VOLUME_MAX, params.volume + HRV_KI_VOLUME * error * dt)),
    )
