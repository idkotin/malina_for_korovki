import json
import tempfile
import threading
import time
import unittest
import urllib.request
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from host_monitor.config import PanelCfg
from host_monitor.local_scale import LocalWeightServer, measurement
from host_monitor.models import Weight
from host_monitor.scale_panel import PanelState, display_number, frame, serial_bits
from host_monitor.workers import WeightSampler
from host_monitor.weight_reader import WeightReader, WeightCfg as ReaderCfg, load_calibration
from host_monitor.config import load_config, AppCfg


class Reader:
    calibrated = True
    calibration_id = 'cal-one'
    def read_weight(self):
        return Weight(weight=2500, raw=2501)


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.sampler = WeightSampler(Reader())
        self.sampler.start()
        for _ in range(100):
            if self.sampler.snapshot()['timestamp_ms']:
                break
            time.sleep(.005)

    def tearDown(self):
        self.sampler.stop()

    def test_http_identity_staleness_and_restart(self):
        server = LocalWeightServer(self.sampler, 'host', '127.0.0.1', 0)
        server.start()
        try:
            self.sampler.stop()
            url = f'http://127.0.0.1:{server.server.server_port}/v1/weight'
            first = json.load(urllib.request.urlopen(url))
            second = json.load(urllib.request.urlopen(url))
            self.assertEqual(first['packetId'], second['packetId'])
            self.assertEqual(first['timestampMs'], second['timestampMs'])
            self.assertTrue(first['valid'])
            with self.sampler._lock:
                self.sampler._updated_monotonic -= 4
            stale = json.load(urllib.request.urlopen(url))
            self.assertFalse(stale['valid'])
            self.assertIsNone(stale['weightKg'])
            self.assertEqual(first['timestampMs'], stale['timestampMs'])
            self.assertNotEqual(first['packetId'], WeightSampler(Reader()).snapshot()['packet_id'])
        finally:
            server.stop()

    def test_tare_persists_without_changing_api_and_screen_off(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = PanelCfg(state_path=str(Path(folder) / 'state.json'))
            panel = PanelState(cfg, self.sampler, 'host')
            panel.press('net')
            self.assertEqual(panel.text(), '    0')
            self.assertEqual(measurement(self.sampler, 'host')['weightKg'], 2500)
            net = measurement(self.sampler, 'host', tare_provider=panel.tare_for)
            self.assertEqual(net['tareKg'], 2500)
            self.assertEqual(net['weightKg'], 2500)
            self.assertEqual(panel.tare_for('different-calibration'), 0)
            restored = PanelState(cfg, self.sampler, 'host')
            self.assertEqual(restored.text(), '    0')
            self.assertEqual(restored.tare_for('cal-one'), 2500)
            restored.press('power')
            self.assertEqual(restored.text(), '     ')
            self.assertTrue(measurement(self.sampler, 'host')['valid'])
            restored.press('power')
            restored.press('net', long=True)
            self.assertEqual(restored.text(), ' 2500')
            self.assertEqual(measurement(self.sampler, 'host', tare_provider=restored.tare_for)['tareKg'], 0)

    def test_admin_pin_selection_and_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = [0]
            cfg = PanelCfg(state_path=str(Path(folder) / 'state.json'), admin_pins={'zero': '0001', 'span': '0002'})
            panel = PanelState(cfg, self.sampler, 'host', lambda: clock[0])
            panel.press('admin')
            for _ in range(3): panel.press('net')
            panel.press('plus'); panel.press('net')
            self.assertEqual(panel.mode, 'zero')
            clock[0] = 61
            panel.text()
            self.assertEqual(panel.mode, 'weight')

    def test_rounding_overflow_mapping_and_shift_order(self):
        self.assertEqual(display_number(-2.5), '   -5')
        self.assertEqual(display_number(99998), '-----')
        self.assertEqual(display_number(0), '    0')
        values = frame('88888', 100)
        self.assertEqual(sum(v > 0 for v in values), 35)
        self.assertTrue(all(values[i] == 0 for i in [21,22,23,38,47]))
        values = [0] * 48; values[47] = 2048; values[0] = 1
        bits = list(serial_bits(values))
        self.assertEqual(len(bits), 576)
        self.assertEqual(bits[0], 1); self.assertEqual(bits[-1], 1)

    def test_unconfirmed_calibration_never_valid(self):
        self.sampler.stop()
        self.sampler._calibrated = False
        self.assertFalse(measurement(self.sampler, 'host')['valid'])

    def test_conflicting_gpio_rejected(self):
        with self.assertRaises(ValueError): PanelCfg(data_pin=17)
        with self.assertRaises(ValueError): PanelCfg(data_pin=6)

    def test_calibration_is_atomic_and_does_not_change_on_rejected_span(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = load_config('config.with-tablet.yaml')
            values = cfg.weight.model_dump()
            values['calibration_path'] = str(Path(folder) / 'cal.json')
            reader = WeightReader(ReaderCfg(**{f.name: values[f.name] for f in fields(ReaderCfg)}))
            self.assertFalse(reader.calibrated)
            reader.read_raw = lambda: 1000.0
            reader.panel_calibrate('zero', 0)
            with self.assertRaises(ValueError): reader.panel_calibrate('span', 100)
            self.assertFalse(Path(values['calibration_path']).exists())
            reader.read_raw = lambda: 1100.0
            reader.panel_calibrate('span', 500)
            self.assertTrue(reader.calibrated)
            cal = load_calibration(values['calibration_path'])
            self.assertEqual(cal.offset, 1000)
            self.assertEqual(cal.scale, 5)
            restored = WeightReader(ReaderCfg(**{f.name: values[f.name] for f in fields(ReaderCfg)}))
            self.assertEqual(restored.calibration_id, reader.calibration_id)
            self.assertTrue(restored.calibrated)

    def test_new_config_forbids_factory_terminal_reboot_guard(self):
        cfg = load_config('config.with-tablet.yaml').model_dump()
        cfg['auto_reboot']['enabled'] = True
        with self.assertRaises(ValueError): AppCfg.model_validate(cfg)


if __name__ == '__main__':
    unittest.main()
