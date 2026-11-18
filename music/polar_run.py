#!/usr/bin/env python3
"""Live Polar RMSSD biofeedback. Rehearsal requires explicit --simulate."""
import argparse
import asyncio
import csv
from datetime import datetime
import math
import os
from pathlib import Path
import random
import time

from controller import AudioParams, update_audio_params
from polar_hrv import RMSSDTracker, rmssd_stream

ROOT = Path(__file__).resolve().parent
BASELINE_SECONDS = 60
PRE_WINDOW_SECONDS = 60
STALE_SECONDS = 5
POST_WINDOW_SECONDS = 30
FIELDS = ["t_rel_s", "music_t_s", "phase", "source", "rmssd_ms", "baseline_ms",
          "delta_rmssd_ms", "rejected_rr", "tempo", "pitch", "volume"]


async def simulated_rmssd_stream():
    """Same tracker and sample type as live mode; never mixed with real data."""
    print("*** SIMULATION: synthetic RR intervals; this is not sensor data ***")
    tracker = RMSSDTracker(BASELINE_SECONDS)
    rng = random.Random(42)
    i = 0
    while True:
        amplitude = 25 + 12 * math.sin(i / 30)
        rr = 850 + amplitude * math.sin(i * 1.3) + rng.uniform(-2, 2)
        sample = tracker.feed([rr], time.monotonic())
        if sample is not None:
            yield sample
        i += 1
        await asyncio.sleep(1)


def compute_pre_post(rows):
    music = [r for r in rows if r["music_t_s"] != ""]
    pre = [r["rmssd_ms"] for r in music if r["music_t_s"] <= PRE_WINDOW_SECONDS]
    last = max((r["music_t_s"] for r in music), default=0)
    post = [r["rmssd_ms"] for r in music if r["music_t_s"] >= max(0, last - POST_WINDOW_SECONDS)]
    if not pre or not post:
        return None
    a, b = sum(pre) / len(pre), sum(post) / len(post)
    return a, b, b - a, len(pre), len(post), last


def append_summary(rows, session_id, source, status, log_dir):
    result = compute_pre_post(rows)
    if result is None:
        return
    a, b, delta, na, nb, duration = result
    path = log_dir / "summary_rmssd_ms.csv"  # Do not mix units with old z-score summaries.
    fields = ["session_id", "source", "status", "music_duration_s", "baseline_ms",
              "pre_mean_rmssd_ms", "post_mean_rmssd_ms", "delta_rmssd_ms", "pre_n", "post_n",
              "windows_overlap"]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(fields)
        writer.writerow([session_id, source, status, duration, rows[-1]["baseline_ms"],
                         a, b, delta, na, nb, duration < PRE_WINDOW_SECONDS + POST_WINDOW_SECONDS])
    print(f"Pre RMSSD {a:.1f} ms; post {b:.1f} ms; change {delta:+.1f} ms.")
    print("These are descriptive windows; short runs can have overlapping windows.")


async def run_session(args):
    from audio_engine import AudioEngine  # Nonhardware helpers import without VLC.
    source = "simulation" if args.simulate else "polar"
    log_dir = Path(os.environ.get("T4MH_MUSIC_LOG_DIR", str(ROOT / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + source
    path = log_dir / f"session_{session_id}_samples.csv"
    media = Path(args.media).expanduser()
    if not media.is_absolute():
        media = ROOT / media
    engine = AudioEngine(media_path=str(media))
    stream = simulated_rmssd_stream() if args.simulate else rmssd_stream(BASELINE_SECONDS)
    rows = []
    params = AudioParams()
    music_start = None
    first_sample = None
    previous = None
    pending = None
    started = time.monotonic()
    last_received = started
    status = "complete"
    print(f"Source: {source}. Music OFF during RR warm-up + {BASELINE_SECONDS}s valid baseline.")
    print(f"Then {PRE_WINDOW_SECONDS}s static music, then adaptive music. Log: {path}")
    try:
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            f.flush()
            pending = asyncio.create_task(stream.__anext__())
            while True:
                now = time.monotonic()
                if engine.failed:
                    raise RuntimeError("VLC playback error")
                if engine.finished:
                    break
                if music_start is not None and now - music_start >= args.max_music_seconds:
                    print("Music time limit reached.")
                    break
                if music_start is None and now - started > 180:
                    raise RuntimeError("Baseline not ready after 180s; check strap/contact.")
                if music_start is not None and now - last_received > STALE_SECONDS:
                    raise RuntimeError("No usable HRV for 5s; stopping music rather than using stale data.")
                done, _ = await asyncio.wait([pending], timeout=0.25)
                if not done:
                    continue
                try:
                    sample = pending.result()
                except StopAsyncIteration:
                    raise RuntimeError("HRV stream ended unexpectedly")
                pending = None
                last_received = time.monotonic()
                if first_sample is None:
                    first_sample = sample.t_monotonic
                dt = 0 if previous is None else sample.t_monotonic - previous
                previous = sample.t_monotonic
                if sample.baseline_ms is not None and music_start is None:
                    engine.start()
                    deadline = time.monotonic() + 5
                    while not engine.player.is_playing():
                        if engine.failed or engine.finished or time.monotonic() > deadline:
                            raise RuntimeError("VLC did not start playback within 5s")
                        await asyncio.sleep(0.1)
                    music_start = time.monotonic()
                    engine.set_params(params)
                music_t = "" if music_start is None else max(0, sample.t_monotonic - music_start)
                phase = "baseline" if music_start is None else ("static" if music_t <= PRE_WINDOW_SECONDS else "adaptive")
                params = update_audio_params(params, sample.rmssd_ms, sample.baseline_ms,
                                             dt_s=dt, is_baseline=phase != "adaptive")
                if music_start is not None:
                    engine.set_params(params)
                row = dict(zip(FIELDS, [sample.t_monotonic - first_sample, music_t, phase, source,
                    sample.rmssd_ms, sample.baseline_ms if sample.baseline_ms is not None else "",
                    sample.rmssd_ms - sample.baseline_ms if sample.baseline_ms is not None else "",
                    sample.rejected_rr, params.tempo, params.pitch, params.volume]))
                writer.writerow(row)
                f.flush()  # Keep collected samples if interrupted or streaming fails.
                rows.append(row)
                print(f"{phase}: RMSSD={sample.rmssd_ms:.1f} ms; baseline={row['baseline_ms']}; "
                      f"tempo={params.tempo:.3f}; volume={params.volume:.1f}; rejected={sample.rejected_rr}")
                pending = asyncio.create_task(stream.__anext__())
    except asyncio.CancelledError:
        status = "interrupted"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await stream.aclose()
        finally:
            engine.stop()
            append_summary(rows, session_id, source, status, log_dir)
            print(f"Saved {path} ({status}).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="Explicit synthetic-data rehearsal")
    parser.add_argument("--media", default=str(ROOT.parent / "media" / "three_little_birds.flac"))
    parser.add_argument("--max-music-seconds", type=float, default=600,
                        help="Safety stop in seconds after music starts (default 600)")
    args = parser.parse_args()
    if not math.isfinite(args.max_music_seconds) or args.max_music_seconds <= 0:
        parser.error("--max-music-seconds must be positive and finite")
    try:
        asyncio.run(run_session(args))
    except KeyboardInterrupt:
        print("Stopped by user.")
    except Exception as exc:
        parser.exit(1, f"Session stopped: {exc}\n")


if __name__ == "__main__":
    main()
