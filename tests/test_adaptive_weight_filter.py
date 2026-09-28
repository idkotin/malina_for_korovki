import math
import unittest
from unittest.mock import patch
from host_monitor.adaptive_weight_filter import AdaptiveWeightFilter
from host_monitor.config import load_config


class AdaptiveTests(unittest.TestCase):
    def settled(self, value=1000):
        f = AdaptiveWeightFilter()
        for i in range(40):
            f.update(value, i * .5)
        return f

    def test_loading_and_unloading_settle_without_gps(self):
        for step in (20, 60, 100, 1000, -60, -1000):
            f = self.settled()
            out = [f.update(1000 + step, 20 + i * .5) for i in range(20)]
            self.assertLessEqual(abs(out[5] - 1000 - step), max(5, abs(step)*.05))
            self.assertTrue(all(min(1000, 1000+step) <= x <= max(1000, 1000+step) for x in out))

    def test_single_spike_and_linear_ramp(self):
        f = self.settled()
        self.assertEqual(f.update(2000, 20), 1000)
        self.assertEqual(f.update(1000, 20.5), 1000)
        for sign in (-1, 1):
            f = self.settled()
            for i in range(80):
                value = 1000 + sign * i * 10
                out = f.update(value, 20+i*.5)
            self.assertLess(abs(out-value), 25)  # 20 kg/s, <1.25 s lag

    def test_vibration_then_quiet_recovery(self):
        f = self.settled()
        values = [f.update(1000 + 150 * (-1)**i, 20+i*.5) for i in range(80)]
        self.assertLess(max(values[20:])-min(values[20:]), 40)
        for i in range(30):
            value = f.update(1000, 60+i*.5)
        self.assertAlmostEqual(value, 1000, places=3)
        self.assertLess(f.noisy_until, f.time)

    def test_missing_invalid_and_nonmonotonic_time_reset(self):
        f = self.settled()
        self.assertEqual(f.update(3000, 25), 3000)
        self.assertEqual(f.update(500, 24), 500)
        with self.assertRaises(ValueError):
            f.update(math.nan, 26)
        self.assertEqual(f.update(200, 27), 200)

    def test_reader_rounding_raw_identity_and_invalid_recovery(self):
        from host_monitor.main import _build_weight_reader
        cfg = load_config('config.with-tablet.yaml')
        cfg.weight.calibration_path = '__nonexistent_adaptive_test__'
        cfg.weight.adaptive_filter = True
        reader = _build_weight_reader(cfg)
        calibration_id = reader.calibration_id
        for i, raw in enumerate([1000]*20 + [1100]*12 + [float('nan'), 500]):
            reader.read_raw = lambda: raw
            with patch('host_monitor.weight_reader.time.monotonic', return_value=i*.5):
                result = reader.read_weight()
            if math.isfinite(raw):
                self.assertEqual(result.raw, raw)
                self.assertEqual(result.weight % 5, 0)
            else:
                self.assertIsNone(result.weight)
        self.assertEqual(result.weight, 500)
        self.assertEqual(reader.calibration_id, calibration_id)


if __name__ == '__main__':
    unittest.main()
