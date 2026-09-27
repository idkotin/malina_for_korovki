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
                iterator = iter(samples * 3 if len(samples) == 20 else samples)
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
                capture('zero', [2700+i*3 for i in range(60)])
            with self.assertRaisesRegex(ValueError, 'Capture empty'):
                capture('span', FIELD_ZERO, 1000)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(), saved)
            for samples in ([2700]*19+[4000], [float('nan')]*20):
                with self.assertRaises(ValueError): capture('zero', samples)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(), saved)

    def test_long_field_capture_and_real_movement(self):
        series = [2770.667,2576,2579.833,2770.833,2766,2499,2636.667,2685.333,2822.833,2724.5,
                  2765.667,2715.667,2628.167,2717.167,2706.167,2759.167,2573.667,2683.5,2687.833,2762.833,
                  2665.833,2774.833,2574,2762.333,2811.833,2677.333,2635,2887.833,2661,2777.5,
                  2811.333,2811.167,2810.833,2719.833,2776.833,2860.333,2841.833,2741.167,2552.167,2780.5,
                  2708.833,2759.5,2826.667,2863.833,2721.167,2685,2759.333,2797.333,2675,2791.167,
                  2668,2664.333,2716.5,2552.333,2773.667,2691.667,2632.833,2842.5,2635.167,2674.667]
        with tempfile.TemporaryDirectory() as folder:
            cfg = load_config('config.with-tablet.yaml')
            cfg.weight.calibration_path = str(Path(folder) / 'cal.json')
            reader = _build_weight_reader(cfg)
            iterator = iter(series)
            reader.read_raw = lambda: next(iterator)
            reader.panel_calibrate('zero', 0)
            self.assertTrue(hasattr(reader, '_pending_zero'))
            for movement in ([2700+i*3 for i in range(60)],
                             [2700+(-1)**i*20+i*4 for i in range(60)],
                             [2700]*30+[2900]*30):
                iterator = iter(movement)
                with self.assertRaisesRegex(ValueError, 'Unstable'):
                    reader.panel_calibrate('zero', 0)
                self.assertFalse(hasattr(reader, '_pending_zero'))


if __name__ == '__main__': unittest.main()
