import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace

from host_monitor.adc_transport import read_conversion
from host_monitor.config import load_config, PanelCfg
from host_monitor.weight_reader import ScaleCalibration, save_calibration
from host_monitor.scale_panel import PanelState


class TransportTests(unittest.TestCase):
    def fake(self, frames):
        class IO:
            def __init__(self): self.levels = []
            def digital_write(self, pin, value): self.levels.append(value)
            def spi_writebyte(self, value): pass
            def spi_readbytes(self, n): return frames.pop(0)
        io = IO()
        return SimpleNamespace(config=io, ADS1263_CMD={'CMD_RDATA1': 1, 'CMD_RDATA2': 2}), SimpleNamespace(cs_pin=22)

    def test_signed_fresh_and_cs_release(self):
        module, dev = self.fake([[0,0,0,0,0,0], [0x80,255,255,254,0,(255+255+254+155)&255]])
        self.assertEqual(read_conversion(module,dev,2), -2)
        self.assertEqual(module.config.levels, [0,1,0,1])

    def test_checksum_alarm_and_timeout_fail_closed(self):
        for packet, adc in [([128,0,0,0,0,0],2), ([0x48,0,0,0,0,155],1),
                            ([0x81,0,0,0,0,155],2), ([128,0,0,0,1,155],2),
                            ([128,127,255,255,0,(127+255+255+155)&255],2)]:
            module,dev = self.fake([packet])
            with self.assertRaises(IOError): read_conversion(module,dev,adc)
            self.assertEqual(module.config.levels[-1],1)
        module,dev = self.fake([])
        with self.assertRaises(TimeoutError): read_conversion(module,dev,1,timeout=0)


class ProfilesTests(unittest.TestCase):
    def test_disconnected_signal_does_not_reset_chip_but_transport_error_does(self):
        from host_monitor.adc_transport import ADCSignalError
        with tempfile.TemporaryDirectory() as folder:
            _,r=self.make(folder)
            r._cfg=replace(r._cfg,adc_burst=True)
            r._adc_ready=True
            r.read_raw=lambda: (_ for _ in ()).throw(ADCSignalError('saturated'))
            self.assertIsNone(r.read_weight().weight)
            self.assertTrue(r._adc_ready)
            r.read_raw=lambda: (_ for _ in ()).throw(IOError('checksum'))
            self.assertIsNone(r.read_weight().weight)
            self.assertFalse(r._adc_ready)

    def make(self, directory):
        from host_monitor.main import _build_weight_reader
        cfg=load_config('config.with-tablet.yaml')
        cfg.weight.calibration_path=str(Path(directory)/'cal.json')
        cfg.weight.adc_profiles=True
        cfg.weight.adc2_gain=128
        save_calibration(cfg.weight.calibration_path, ScaleCalibration(1800,2,True,128))
        reader=_build_weight_reader(cfg)
        reader._init_ads1263=lambda: None
        return cfg,reader

    def test_round_trip_separate_calibration_and_one_point(self):
        from host_monitor.main import _build_weight_reader
        with tempfile.TemporaryDirectory() as folder:
            cfg,r=self.make(folder)
            original=Path(cfg.weight.calibration_path).read_bytes()
            original_id=r.calibration_id
            r.switch_adc(1)
            self.assertFalse(r.calibrated)
            r.read_raw=lambda: 2000
            self.assertIsNone(r.read_weight().weight)
            r.panel_calibrate('anchor', 1000)
            self.assertEqual(r._cal.offset,1500)
            self.assertEqual(r._cal.scale,2)
            self.assertTrue(r._cal.provisional)
            self.assertTrue(r.calibrated)
            self.assertEqual(r.read_weight().weight,1000)
            rebooted=_build_weight_reader(cfg)
            self.assertEqual(rebooted.adc_profile,1)
            self.assertTrue(rebooted.calibrated)
            r.switch_adc(2)
            self.assertEqual(r.calibration_id,original_id)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(),original)
            with self.assertRaises(ValueError): r.panel_calibrate('anchor',0)

    def test_zero_anchor_then_full_span(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg,r=self.make(folder)
            r.switch_adc(1)
            r.read_raw=lambda: 2000
            r.panel_calibrate('anchor',0)
            r.read_raw=lambda: 4000
            r.panel_calibrate('span',4015)
            self.assertAlmostEqual(r._cal.scale,2.0075)
            self.assertFalse(r._cal.provisional)
            self.assertEqual(r._cal.frontend,'adc1')

    def test_pending_zeros_survive_switch_restart_and_are_independent(self):
        from host_monitor.main import _build_weight_reader
        with tempfile.TemporaryDirectory() as folder:
            cfg,r=self.make(folder)
            original=Path(cfg.weight.calibration_path).read_bytes()
            r.read_raw=lambda: 1800
            r.panel_calibrate('zero',0)
            r.switch_adc(1)
            r.read_raw=lambda: 2000
            r.panel_calibrate('anchor',0)
            r.switch_adc(2)
            r=_build_weight_reader(cfg)
            r._init_ads1263=lambda: None
            self.assertEqual(r._pending_zero,1800)
            r.switch_adc(1)
            self.assertEqual(r._pending_zero,2000)
            r.read_raw=lambda: 2800
            r.panel_calibrate('span',2000)
            self.assertEqual(r._cal.scale,2.5)
            self.assertFalse(hasattr(r,'_pending_zero'))
            r.reload_calibration()
            self.assertFalse(hasattr(r,'_pending_zero'))
            r.switch_adc(2)
            self.assertEqual(r._pending_zero,1800)
            self.assertEqual(Path(cfg.weight.calibration_path).read_bytes(),original)
            r.switch_adc(1)
            r.read_raw=lambda: 2900
            r.panel_calibrate('anchor',2000)
            self.assertEqual(r._cal.scale,2.5)
            self.assertEqual(r._cal.offset,2100)
            self.assertFalse(r._cal.provisional)
            r.read_raw=lambda: 2100
            r.panel_calibrate('anchor',0)
            self.assertEqual(r._cal.scale,2.5)

    def test_failed_new_zero_invalidates_saved_pending_but_keeps_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            _,r=self.make(folder)
            r.switch_adc(1)
            r.read_raw=lambda: 2000
            r.panel_calibrate('anchor',0)
            original=Path(r._cfg.calibration_path).read_bytes()
            r.read_raw=lambda: (_ for _ in ()).throw(IOError('disconnected'))
            with self.assertRaises(IOError): r.panel_calibrate('zero',0)
            r.reload_calibration()
            self.assertFalse(hasattr(r,'_pending_zero'))
            self.assertEqual(Path(r._cfg.calibration_path).read_bytes(),original)

    def test_stale_or_malformed_pending_zero_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            _,r=self.make(folder)
            path=Path(r._cfg.calibration_path+'.pending-zero.json')
            for saved in [[], {'active':True,'calibration_id':'old','raw':2,'noise':0},
                          {'active':True,'calibration_id':r.calibration_id,'raw':'NaN','noise':0}]:
                path.write_text(json.dumps(saved))
                r.reload_calibration()
                with self.assertRaisesRegex(ValueError,'Capture empty'):
                    r.panel_calibrate('span',2000)

    def test_failed_switch_and_noisy_anchor_preserve_files(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg,r=self.make(folder)
            r._init_ads1263=lambda: (_ for _ in ()).throw(IOError('register mismatch'))
            with self.assertRaises(IOError): r.switch_adc(1)
            self.assertEqual(r.adc_profile,2)
            self.assertFalse(Path(r._profile_path).exists())
            r._init_ads1263=lambda: None
            r.switch_adc(1)
            samples=iter([2000+i*50 for i in range(60)])
            r.read_raw=lambda: next(samples)
            with self.assertRaises(ValueError): r.panel_calibrate('anchor',0)
            self.assertFalse(Path(r._cfg.calibration_path).exists())

    def test_panel_profile_and_anchor_require_long_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            commands=[]
            sampler=SimpleNamespace(status=lambda: {'adc_profile':2},command=lambda *x:commands.append(x))
            cfg=PanelCfg(state_path=str(Path(folder)/'panel.json'))
            p=PanelState(cfg,sampler,'host')
            p.mode='adc'; p.value=2
            self.assertEqual(p.text(),'AdC 2')
            p.press('minus'); p.press('net')
            self.assertEqual(commands,[])
            p.press('net',long=True)
            self.assertEqual(commands,[('adc',1)])
            p.mode='anchor'; p.value=0
            p.press('minus'); self.assertEqual(p.value,0)
            p.press('plus',long=True); self.assertEqual(p.value,50)
            p.press('net',long=True)
            self.assertEqual(commands[-1],('anchor',50))


if __name__ == '__main__': unittest.main()

