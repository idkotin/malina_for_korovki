import tempfile
import unittest
from pathlib import Path
from host_monitor.main import _build_weight_reader
from host_monitor.config import load_config


FIELD_ZERO = [2780.833333,2755,2695.666667,2733.833333,2754.333333,
              2828,2672.833333,2823.833333,2863,2630.666667,
              2759.666667,2790.333333,2745,2639.666667,2823,
              2792.833333,2842.666667,2672.833333,2654.166667,2766.833333]


class CalibrationNoiseTests(unittest.TestCase):
    def test_field_noise_and_span_signal_requirement(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = load_config('config.with-tablet.yaml')
            cfg.weight.calibration_path = str(Path(folder) / 'cal.json')
            reader = _build_weight_reader(cfg)
            def capture(action, samples, value=0):
                iterator = iter(samples)
                reader.read_raw = lambda: next(iterator)
                reader.panel_calibrate(action, value)
            capture('zero', FIELD_ZERO)
            self.assertAlmostEqual(reader._pending_zero, 2757.3333335)
            self.assertFalse(reader.calibrated)
            with self.assertRaisesRegex(ValueError, 'relative to noise'):
                capture('span', [x+500 for x in FIELD_ZERO], 500)
            self.assertFalse(Path(cfg.weight.calibration_path).exists())
            capture('span', [x+5000 for x in FIELD_ZERO], 1000)
            self.assertTrue(reader.calibrated)
            self.assertAlmostEqual(reader._cal.scale, .2)
            saved = Path(cfg.weight.calibration_path).read_bytes()
            capture('zero', FIELD_ZERO)
            with self.assertRaisesRegex(ValueError, 'Unstable'):
                capture('zero', [2700+i*10 for i in range(20)])
            with self.assertRaisesRegex(ValueError, 'Capture empty'):
                capture('span', FIELD_ZERO, 1000)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(), saved)
            for samples in ([2700]*19+[4000], [float('nan')]*20):
                with self.assertRaises(ValueError): capture('zero', samples)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(), saved)


if __name__ == '__main__': unittest.main()
