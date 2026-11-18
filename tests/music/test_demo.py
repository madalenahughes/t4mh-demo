"""Nonhardware regression checks: python3 -m unittest -v test_demo.py"""
import asyncio
import csv
import math
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "music"))
sys.modules.setdefault("bleak", types.SimpleNamespace(BleakError=Exception, BleakClient=object, BleakScanner=object))

from controller import AudioParams, update_audio_params
from polar_hrv import HRVSample, RMSSDTracker, compute_rmssd, parse_rr_ms
import polar_run


class SignalTests(unittest.TestCase):
    def test_rmssd_units_and_no_gap_bridging(self):
        self.assertEqual(compute_rmssd([800, 810, 800]), 10)
        self.assertEqual(compute_rmssd([800, 810, None, 1200, 1210]), 10)
        self.assertIsNone(compute_rmssd([800, None, 1200]))

    def test_hrm_flags_and_truncation(self):
        rr = (1024).to_bytes(2, 'little')
        for flags, prefix in [(0x10, bytes([60])), (0x19, bytes([60, 0, 99, 0]))]:
            self.assertEqual(parse_rr_ms(bytes([flags]) + prefix + rr), [1000])
        self.assertEqual(parse_rr_ms(bytes([0x10, 60, 1])), [])
        self.assertEqual(parse_rr_ms(bytes([0x14, 60]) + rr), [])
        self.assertEqual(parse_rr_ms(bytes([0, 60])), [])

    def test_baseline_starts_after_warmup_and_stays_fixed(self):
        tracker = RMSSDTracker(5, 5)
        for i in range(9):
            sample = tracker.feed([800 + 10 * (i % 2)], 100 + i)
            self.assertIsNone(sample.baseline_ms if sample else None)
        sample = tracker.feed([810], 109)
        self.assertEqual(sample.baseline_ms, 10)
        for i in range(10, 30):
            sample = tracker.feed([800 + 30 * (i % 2)], 100 + i)
        self.assertEqual(sample.baseline_ms, 10)
        self.assertGreater(sample.rmssd_ms, sample.baseline_ms)

    def test_artifacts_and_reacquisition(self):
        tracker = RMSSDTracker(5, 20)
        for i in range(30):
            tracker.feed([800 + 10 * (i % 2)], i)
        sample = tracker.feed([1600], 30)
        self.assertEqual(sample.rmssd_ms, 10)
        self.assertEqual(sample.rejected_rr, 1)
        self.assertIsNone(tracker.feed([float('nan'), 100, 2500], 31))
        for i in range(32, 55):
            sample = tracker.feed([810], i)
        self.assertEqual(sample.rmssd_ms, 0)

    def test_gap_does_not_finish_baseline(self):
        tracker = RMSSDTracker(30, 5)
        for i in range(10):
            tracker.feed([800], i)
        self.assertIsNone(tracker.feed([800], 100))
        self.assertEqual(tracker.baseline_elapsed, 0)

    def test_controller_direction_deadband_bounds_and_time(self):
        neutral = AudioParams()
        higher = update_audio_params(neutral, 60, 40)
        lower = update_audio_params(neutral, 20, 40)
        self.assertLess(higher.tempo, 1)
        self.assertLess(higher.pitch, -2)
        self.assertLess(higher.volume, 60)
        self.assertGreater(lower.tempo, 1)
        self.assertGreater(lower.pitch, -2)
        self.assertGreater(lower.volume, 60)
        self.assertEqual(update_audio_params(neutral, 41, 40), neutral)
        a = neutral
        for _ in range(10):
            a = update_audio_params(a, 60, 40, dt_s=.1)
        self.assertAlmostEqual(a.tempo, higher.tempo)
        self.assertAlmostEqual(a.volume, higher.volume)
        self.assertEqual(update_audio_params(neutral, 60, 40, dt_s=100),
                         update_audio_params(neutral, 60, 40, dt_s=2))
        for _ in range(10000):
            a = update_audio_params(a, 100, 40)
        self.assertEqual(a, AudioParams(.85, -6, 52))
        recovery = update_audio_params(a, 10, 40)
        self.assertGreater(recovery.tempo, a.tempo)
        self.assertEqual(update_audio_params(a, 60, 40, is_baseline=True), neutral)
        self.assertTrue(math.isfinite(update_audio_params(neutral, float('nan'), 40).tempo))


class FakeEngine:
    instances = []
    def __init__(self, media_path):
        self.finished = self.failed = self.started = self.stopped = False
        self.player = self
        self.applied = []
        self.instances.append(self)
    def start(self): self.started = True
    def is_playing(self): return self.started
    def set_params(self, p): self.applied.append(p)
    def stop(self): self.stopped = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failure=False):
        async def stream(*_):
            for base in [None, 40, 40, 40]:
                await asyncio.sleep(.01)
                yield HRVSample(time.monotonic(), 60, base, 2)
            if failure:
                raise RuntimeError('injected BLE failure')
            FakeEngine.instances[-1].finished = True
            await asyncio.sleep(10)  # End-of-track must work without another sample.
        with tempfile.TemporaryDirectory() as td:
            fake_module = types.SimpleNamespace(AudioEngine=FakeEngine)
            args = types.SimpleNamespace(simulate=False, media='unused.wav', max_music_seconds=10)
            with patch.dict(sys.modules, audio_engine=fake_module), patch.object(polar_run, 'ROOT', Path(td)), \
                 patch.object(polar_run, 'rmssd_stream', stream), patch.object(polar_run, 'PRE_WINDOW_SECONDS', .001):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, 'injected'):
                        await polar_run.run_session(args)
                else:
                    await asyncio.wait_for(polar_run.run_session(args), 2)
            files = list((Path(td) / 'logs').glob('*samples.csv'))
            with files[0].open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]['phase'], 'baseline')
            self.assertEqual(rows[1]['phase'], 'static')
            self.assertEqual(rows[-1]['phase'], 'adaptive')
            self.assertEqual(float(rows[-1]['rmssd_ms']), 60)
            self.assertLess(float(rows[-1]['tempo']), 1)
            self.assertNotIn('rmssd_z', rows[-1])
            self.assertTrue(FakeEngine.instances[-1].stopped)
            with (Path(td) / 'logs' / 'summary_rmssd_ms.csv').open() as f:
                summary = list(csv.DictReader(f))[0]
            self.assertEqual(summary['status'], 'error' if failure else 'complete')

    async def test_track_end_without_next_sensor_sample(self):
        await self.exercise()

    async def test_error_preserves_csv_and_stops_audio(self):
        await self.exercise(True)

    async def test_stale_data_stops_and_logs(self):
        async def stream(*_):
            yield HRVSample(time.monotonic(), 40, 40, 0)
            await asyncio.sleep(10)
        with tempfile.TemporaryDirectory() as td:
            args = types.SimpleNamespace(simulate=False, media='unused.wav', max_music_seconds=10)
            with patch.dict(sys.modules, audio_engine=types.SimpleNamespace(AudioEngine=FakeEngine)), \
                 patch.object(polar_run, 'ROOT', Path(td)), patch.object(polar_run, 'rmssd_stream', stream), \
                 patch.object(polar_run, 'STALE_SECONDS', .02):
                with self.assertRaisesRegex(RuntimeError, 'No usable HRV'):
                    await asyncio.wait_for(polar_run.run_session(args), 2)
            self.assertTrue(FakeEngine.instances[-1].stopped)
            with (Path(td) / 'logs' / 'summary_rmssd_ms.csv').open() as f:
                self.assertEqual(list(csv.DictReader(f))[0]['status'], 'error')

    async def test_cancellation_preserves_log(self):
        async def stream(*_):
            yield HRVSample(time.monotonic(), 40, 40, 0)
            await asyncio.sleep(10)
        with tempfile.TemporaryDirectory() as td:
            args = types.SimpleNamespace(simulate=False, media='unused.wav', max_music_seconds=10)
            with patch.dict(sys.modules, audio_engine=types.SimpleNamespace(AudioEngine=FakeEngine)), \
                 patch.object(polar_run, 'ROOT', Path(td)), patch.object(polar_run, 'rmssd_stream', stream):
                task = asyncio.create_task(polar_run.run_session(args))
                await asyncio.sleep(.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(FakeEngine.instances[-1].stopped)
            with (Path(td) / 'logs' / 'summary_rmssd_ms.csv').open() as f:
                self.assertEqual(list(csv.DictReader(f))[0]['status'], 'interrupted')

    async def test_simulation_uses_same_sample_type(self):
        with patch.object(polar_run, 'BASELINE_SECONDS', .001):
            stream = polar_run.simulated_rmssd_stream()
            # Speed sleep only, preserving tracker sample schema.
            original = asyncio.sleep
            async def fast(_): await original(0)
            with patch('polar_run.asyncio.sleep', fast):
                sample = await stream.__anext__()
            self.assertIsInstance(sample, HRVSample)
            self.assertGreaterEqual(sample.rmssd_ms, 0)
            await stream.aclose()


class AudioTests(unittest.TestCase):
    def test_vlc_rate_events_and_failures(self):
        from unittest.mock import MagicMock
        player = MagicMock()
        player.play.return_value = 0
        player.set_rate.return_value = 0
        player.audio_set_volume.return_value = 0
        vlc = MagicMock()
        vlc.Instance.return_value.media_player_new.return_value = player
        with patch.dict(sys.modules, vlc=vlc):
            import importlib.util
            spec = importlib.util.spec_from_file_location('tested_audio', Path(__file__).resolve().parents[2] / 'music' / 'audio_engine.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.NamedTemporaryFile() as media:
                engine = module.AudioEngine(media.name)
            engine.start()
            engine.set_params(AudioParams(.9, -3, 55))
            player.set_rate.assert_called_with(.9)
            engine._on_end(None)
            self.assertTrue(engine.finished)
            engine._on_error(None)
            self.assertTrue(engine.failed)
            player.set_rate.return_value = -1
            with self.assertRaises(RuntimeError):
                engine.set_params(AudioParams())


if __name__ == '__main__':
    unittest.main()
