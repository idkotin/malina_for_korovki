import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError
from host_monitor.config import load_config, WeightCfg
from host_monitor.weight_reader import ScaleCalibration, save_calibration


class AdcGainTests(unittest.TestCase):
    def test_gain_normalization_and_calibration_guard(self):
        from host_monitor.main import _build_weight_reader
        with tempfile.TemporaryDirectory() as directory:
            cfg = load_config('config.with-tablet.yaml')
            cfg.weight.calibration_path = str(Path(directory) / 'cal.json')
            save_calibration(cfg.weight.calibration_path, ScaleCalibration(1700, 2, True))
            old_id = _build_weight_reader(cfg).calibration_id
            cfg.weight.adc2_gain = 128
            reader = _build_weight_reader(cfg)
            self.assertFalse(reader.calibrated)
            self.assertNotEqual(old_id, reader.calibration_id)
            reader.read_raw_counts = lambda: 128 * 1800
            self.assertEqual(reader.read_raw(), 1800)
            self.assertIsNone(reader.read_weight().weight)
            save_calibration(cfg.weight.calibration_path, ScaleCalibration(1700, 2, True, 128))
            reader.reload_calibration()
            self.assertTrue(reader.calibrated)
            self.assertEqual(reader.read_weight().raw, 200)

    def test_invalid_gain_rejected(self):
        with self.assertRaises(ValidationError):
            WeightCfg(adc2_gain=3)


if __name__ == '__main__':
    unittest.main()
