"""Hardware-free regression checks for baseline metadata and legacy callers."""
import asyncio
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import statistics
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[1] / 'monitor'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def samples(self, include, differences, duration, count):
        clock = types.SimpleNamespace(now=0.0)
        stopped = []
        class BleakError(Exception): pass
        class Client:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self):
                clock.now += 500  # Connection setup must not count as baseline.
                return self
            async def __aexit__(self, *args): pass
            async def start_notify(self, uuid, handler):
                for difference in differences:
                    clock.now += 1
                    raw = [800, 800 + difference, 800]
                    packet = bytes([0x10, 60]) + b''.join(x.to_bytes(2, 'little') for x in raw)
                    handler(None, bytearray(packet))
            async def stop_notify(self, uuid): stopped.append(uuid)
        fake = types.SimpleNamespace(BleakError=BleakError, BleakClient=Client, BleakScanner=object)
        with patch.dict(sys.modules, bleak=fake):
            module = load_module('tested_hrv', SOURCE / 'polar_hrv.py')
        module.find_polar_device = AsyncMock(return_value=types.SimpleNamespace(address='test'))
        module.time = types.SimpleNamespace(monotonic=lambda: clock.now)
        with contextlib.redirect_stdout(io.StringIO()):
            gen = module.rmssd_z_stream(baseline_duration_s=duration, rr_window_size=3,
                                        include_baseline=include)
            try:
                values = [await asyncio.wait_for(gen.__anext__(), .5) for _ in range(count)]
            finally:
                await gen.aclose()
        self.assertTrue(stopped)
        return values

    async def test_default_still_returns_two_values(self):
        result = (await self.samples(False, [32, 32, 32, 64], 2, 1))[0]
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0], 62.5)
        self.assertAlmostEqual(result[1], 3.125)  # Existing zero-SD 10 ms fallback retained.

    async def test_metadata_delta_sign_and_fixed_baseline(self):
        values = await self.samples(True, [32, 32, 32, 64, 16], 2, 2)
        high, low = values
        self.assertEqual(high, (62.5, 3.125, 31.25, 31.25))
        self.assertEqual(low, (15.625, -1.5625, 31.25, -15.625))

    async def test_z_and_delta_share_same_nonzero_std_baseline(self):
        current, z, baseline, delta = (await self.samples(True, [16, 32, 48, 64], 2, 1))[0]
        base_values = [15.625, 31.25, 46.875]
        self.assertAlmostEqual(baseline, statistics.mean(base_values))
        self.assertAlmostEqual(delta, current - baseline)
        self.assertAlmostEqual(z, delta / statistics.pstdev(base_values))

    async def test_baseline_excludes_connection_setup_time(self):
        result = (await self.samples(True, [16, 16, 16, 32, 32, 32, 64], 5, 1))[0]
        self.assertAlmostEqual(result[2], 23.4375)
        self.assertAlmostEqual(result[3], 39.0625)
        self.assertAlmostEqual(result[1], 5)


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_csv_udp_and_console_keep_raw_change_before_z(self):
        options = []
        async def stream(**kwargs):
            options.append(kwargs)
            yield (40, 2, 30, 10)
            yield (20, -2, 30, -10)
        sock = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(sys.modules, polar_hrv=types.SimpleNamespace(rmssd_z_stream=stream)), \
                 patch('socket.socket', return_value=sock), patch.dict(__import__('os').environ, {'T4MH_DATA_DIR': td}):
                module = load_module('tested_recorder', SOURCE / 'polar_record.py')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                path = await module.record()
            self.assertEqual(options, [{'include_baseline': True}])
            with open(path, newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0]), ['unix_time_s', 'elapsed_s', 'rmssd_ms',
                             'baseline_rmssd_ms', 'delta_rmssd_ms', 'rmssd_z'])
            self.assertEqual(float(rows[0]['delta_rmssd_ms']), 10)
            self.assertEqual(float(rows[1]['delta_rmssd_ms']), -10)
            self.assertEqual(float(rows[0]['baseline_rmssd_ms']), 30)
            messages = [json.loads(call.args[0]) for call in sock.sendto.call_args_list]
            self.assertEqual(messages[0], {'sensor':'polar', 'rmssd_ms':40,
                             'baseline_rmssd_ms':30, 'delta_rmssd_ms':10, 'rmssd_z':2})
            self.assertEqual(messages[1]['delta_rmssd_ms'], -10)
            self.assertTrue(all(call.args[1] == ('127.0.0.1', 15000)
                                for call in sock.sendto.call_args_list))
            text = out.getvalue()
            self.assertIn('change= +10.00 ms', text)
            self.assertLess(text.index('change='), text.index('z='))
            module.integration_sock.close()

    async def test_stream_failure_leaves_flushed_csv(self):
        async def stream(**kwargs):
            yield (40, 2, 30, 10)
            raise RuntimeError('injected sensor failure')
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(sys.modules, polar_hrv=types.SimpleNamespace(rmssd_z_stream=stream)), \
                 patch('socket.socket', return_value=MagicMock()), patch.dict(__import__('os').environ, {'T4MH_DATA_DIR': td}):
                module = load_module('tested_failed_recorder', SOURCE / 'polar_record.py')
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'injected'):
                await module.record()
            with next(Path(td).glob('polar_*.csv')).open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(float(rows[0]['delta_rmssd_ms']), 10)
            module.integration_sock.close()


class DashboardTests(unittest.TestCase):
    def test_fields_order_sign_missing_values_and_fahrenheit_preserved(self):
        source = (SOURCE / 'integration.py').read_text().split('\nlast_draw = 0')[0]
        ns = {'__name__':'tested_monitor'}
        with patch('socket.socket', return_value=MagicMock()):
            exec(compile(source, 'integration.py', 'exec'), ns)
        ns['latest'] = {'polar':{'rmssd_ms':40,'baseline_rmssd_ms':30,
                                'delta_rmssd_ms':10,'rmssd_z':2},
                        'emotibit':{'t1':37,'th':0}}
        with patch('os.system'), contextlib.redirect_stdout(io.StringIO()) as out:
            ns['draw']()
        text = out.getvalue()
        self.assertLess(text.index('Baseline RMSSD'), text.index('Change from baseline'))
        self.assertLess(text.index('Change from baseline'), text.index('RMSSD z-score'))
        self.assertIn('+10.0 ms', text)
        self.assertIn('98.600 °F', text)
        self.assertIn('FBA Current', text)
        ns['latest']['polar']['delta_rmssd_ms'] = -5
        self.assertEqual(ns['fmt_signed']('polar','delta_rmssd_ms'), '-5.0')
        ns['latest']['polar'] = {'rmssd_ms':40,'rmssd_z':2}
        self.assertEqual(ns['fmt_signed']('polar','delta_rmssd_ms'), 'Waiting...')
        self.assertEqual(ns['fmt']('polar','baseline_rmssd_ms'), 'Waiting...')


if __name__ == '__main__':
    unittest.main()
