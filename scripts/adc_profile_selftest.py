"""Exclusive internal TDAC test. No external pin drive or calibration writes.

Run ONLY with host-monitor stopped and an independent restart timer installed.
The internal source checks transport/scale/timing, NOT load-cell accuracy.
"""
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_monitor.config import load_config
from host_monitor.main import _build_weight_reader


def main():
    if subprocess.run(['systemctl','is-active','--quiet','host-monitor']).returncode == 0:
        raise SystemExit('Stop host-monitor before opening ADC')
    cfg=load_config('/etc/host-monitor/config.yaml')
    cfg.weight.adc_profiles=True
    report={}
    for name, profile, burst in [('adc2_legacy',2,False),('adc2_burst',2,True),('adc1_burst',1,True)]:
        r=_build_weight_reader(cfg)
        r._cfg=replace(r._profile_cfg(profile),adc_burst=burst)
        r.prepare()
        dev, mod=r._adc_dev,r._adc_mod
        regs=mod.ADS1263_REG
        # TDAC output routing to external AIN6/AIN7 stays disabled (bit7=0).
        dev.ADS1263_WriteReg(regs['REG_TDACN'],0)
        dev.ADS1263_WriteReg(regs['REG_TDACP'],0)
        if profile==2:
            dev.ADS1263_SetDiffChannal_ADC2=lambda channel: dev.ADS1263_WriteReg(regs['REG_ADC2MUX'],0xee)
        else:
            dev.ADS1263_WriteReg(regs['REG_INPMUX'],0xee)
        report[name]={}
        for code in (0,1):
            dev.ADS1263_WriteReg(regs['REG_TDACP'],code)
            r.read_raw()  # discard first batch after test-source switch
            values=[]; elapsed=[]
            for _ in range(40):
                started=time.monotonic()
                values.append(r.read_raw())
                elapsed.append(time.monotonic()-started)
            report[name][str(code)]={'median':statistics.median(values),
                'sd':statistics.stdev(values),'read_seconds':statistics.median(elapsed)}
        print(name,json.dumps(report[name]),flush=True)
    Path('/tmp/adc-profile-selftest.json').write_text(json.dumps(report,indent=2))
    print('INTERNAL TEST COMPLETE; not a load calibration',flush=True)


if __name__=='__main__': main()
