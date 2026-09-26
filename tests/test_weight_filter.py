import unittest
from dataclasses import fields
from unittest.mock import patch
from host_monitor.config import load_config
from host_monitor.weight_reader import WeightReader, WeightCfg
from host_monitor.weight_display import WeightDisplay, round_weight
from host_monitor.local_scale import measurement


class FilterTests(unittest.TestCase):
    def reader(self):
        values = load_config('config.with-tablet.yaml').weight.model_dump()
        values['calibration_path'] = '__missing_test_calibration__'
        return WeightReader(WeightCfg(**{f.name: values[f.name] for f in fields(WeightCfg)}))

    def test_rounding_and_boundary_hysteresis(self):
        self.assertEqual([round_weight(x) for x in [-7.5, -2.5, 2.5, 7.5]], [-10, -5, 5, 10])
        display = WeightDisplay()
        self.assertEqual([display.update(x) for x in [100, 102.6, 102.4, 103.6, 102.4, 101.4]],
                         [100, 100, 100, 105, 105, 100])

    def test_spikes_steps_raw_and_recovery(self):
        reader = self.reader()
        def read(x):
            reader.read_raw = lambda: x
            return reader.read_weight()
        for _ in range(15): read(1000)
        self.assertEqual(read(1700).weight, 1000)
        self.assertEqual(read(1000).weight, 1000)
        result = [read(1100).weight for _ in range(20)]
        self.assertLessEqual(next(i for i,x in enumerate(result) if x >= 1090), 10)
        self.assertEqual(result[-1], 1100)
        self.assertTrue(all(x % 5 == 0 for x in result))
        self.assertEqual(read(1101.23).raw, 1101.23)
        self.assertIsNone(read(float('nan')).weight)
        self.assertEqual(read(2001).weight, 2000)
        reader.read_raw = lambda: (_ for _ in ()).throw(IOError('disconnected'))
        self.assertIsNone(reader.read_weight().weight)
        self.assertEqual(read(500).weight, 500)
        with patch('host_monitor.weight_reader.time.monotonic', return_value=reader._last_read_time + 4):
            self.assertEqual(read(2500).weight, 2500)

    def test_packet_uses_supplied_snapshot(self):
        from host_monitor.models import Weight
        snap = dict(weight=Weight(weight=2505,raw=2506.7), age_s=.1, calibrated=True,
                    packet_id='a:1',timestamp_ms=123,calibration_id='cal')
        packet = measurement(None, 'host', snap)
        self.assertEqual(packet['weightKg'],2505)
        self.assertEqual(packet['packetId'],'a:1')


if __name__ == '__main__': unittest.main()
