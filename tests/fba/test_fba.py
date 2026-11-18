"""Nonhardware tests for the proposed FBA calculation and output path."""
import contextlib
import csv
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('MPLBACKEND', 'Agg')
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "monitor"))
import processor
from processor import EEGProcessor, frontal_band_asymmetry
import numpy as np


def powers(left, right):
    return {'AF7': {'beta_power': left}, 'AF8': {'beta_power': right}}


class CalculationTests(unittest.TestCase):
    def test_equal_powers_are_zero(self):
        self.assertEqual(frontal_band_asymmetry(powers(4, 4), 'beta_power'), 0)

    def test_right_minus_left_and_log_power_ratio(self):
        self.assertAlmostEqual(frontal_band_asymmetry(powers(4, 16), 'beta_power'), math.log(4))
        self.assertAlmostEqual(frontal_band_asymmetry(powers(16, 4), 'beta_power'), -math.log(4))

    def test_common_scale_does_not_change_asymmetry(self):
        self.assertAlmostEqual(frontal_band_asymmetry(powers(4000, 16000), 'beta_power'), math.log(4))

    def test_invalid_or_missing_powers_are_unavailable(self):
        for value in [None, 0, -1, float('nan'), float('inf')]:
            for data in [powers(value, 1), powers(1, value)]:
                with self.subTest(data=data):
                    self.assertIsNone(frontal_band_asymmetry(data, 'beta_power'))
        self.assertIsNone(frontal_band_asymmetry({}, 'beta_power'))
        self.assertIsNone(frontal_band_asymmetry({'AF7': {'beta_power': 1}}, 'beta_power'))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sock = MagicMock()
        with patch('processor.socket.socket', return_value=self.sock), \
             patch.dict(os.environ, {'T4MH_DATA_DIR': self.temp.name}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.processor = EEGProcessor()
        self.processor.first_plot = False
        self.addCleanup(self.processor.log_file.close)
        self.addCleanup(self.processor.integration_sock.close)
        self.t = np.arange(512) / 256.0

    def signal(self, beta):
        return 3 * np.sin(2 * np.pi * 10 * self.t) + beta * np.sin(2 * np.pi * 20 * self.t)

    def process(self, left_beta, right_beta):
        eeg = {'TP9': self.signal(3), 'AF7': self.signal(left_beta),
               'AF8': self.signal(right_beta), 'TP10': self.signal(3)}
        with patch('processor.os.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.processor.process(eeg)
        message = json.loads(self.sock.sendto.call_args.args[0].decode('utf-8'))
        return message, output.getvalue()

    def test_synthetic_eeg_uses_beta_not_alpha(self):
        message, output = self.process(3, 6)
        self.assertAlmostEqual(message['fba'], math.log(4), delta=0.002)
        self.assertAlmostEqual(message['faa'], 0, delta=0.002)
        self.assertEqual(message['fba_smoothed'], message['fba'])
        self.assertIn('FBA (>13–30 Hz)', output)
        self.assertEqual(self.sock.sendto.call_args.args[1], ('127.0.0.1', 15000))

    def test_smoothing_csv_schema_and_faa_preserved(self):
        first, _ = self.process(3, 6)
        second, _ = self.process(6, 3)
        self.assertAlmostEqual(second['fba_smoothed'], .2 * second['fba'] + .8 * first['fba'])
        self.assertAlmostEqual(second['faa'], 0, delta=0.002)
        with open(self.processor.log_path, newline='') as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][-4:], ['faa', 'faa_smoothed', 'fba', 'fba_smoothed'])
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
        self.assertAlmostEqual(float(rows[-1][-2]), second['fba'])
        self.assertAlmostEqual(float(rows[-1][-1]), second['fba_smoothed'])

    def test_missing_pair_clears_fba_smoothing(self):
        self.process(3, 6)
        with patch('processor.os.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()) as output:
            self.processor.process({'TP9': self.signal(3)})
        self.assertIsNone(self.processor.fba_smoothed)
        self.assertIn('Waiting for valid AF7/AF8 beta power', output.getvalue())
        message, _ = self.process(6, 3)
        self.assertEqual(message['fba_smoothed'], message['fba'])

    def test_combined_dashboard_displays_message_fields(self):
        message, _ = self.process(3, 6)
        source = Path(processor.__file__).with_name('integration.py').read_text()
        prefix = source.split('\nlast_draw = 0')[0]
        ns = {'__name__': 'tested_integration'}
        with patch('socket.socket', return_value=MagicMock()):
            exec(compile(prefix, 'integration.py', 'exec'), ns)
        ns['latest']['muse'] = message
        ns['last_seen']['muse'] = 0
        with patch('os.system'), contextlib.redirect_stdout(io.StringIO()) as out:
            ns['draw']()
        text = out.getvalue()
        self.assertIn('FBA Current', text)
        self.assertIn('FBA Smoothed', text)
        self.assertIn(f"{message['fba']:.3f}", text)
        self.assertIn('FAA Current', text)
        ns['latest']['muse'] = {'faa': 0.1}
        self.assertEqual(ns['fmt']('muse', 'fba'), 'Waiting...')


if __name__ == '__main__':
    unittest.main()
