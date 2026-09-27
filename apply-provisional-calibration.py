"""Explicit field workaround requested on 2026-09-27; not a precision calibration.

Stop host-monitor before running. Uses recorded empty and stationary 4015 kg
measurements; does not access the ADC or change filtering/network configuration.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
from host_monitor.config import load_config
from host_monitor.local_scale import atomic_json
from host_monitor.weight_reader import load_calibration


def apply(config_path):
    cfg = load_config(config_path)
    if not (cfg.weight.enabled and not cfg.weight.simulate and cfg.weight.require_calibration
            and cfg.weight.frontend == 'adc2' and cfg.weight.reference_mode == 'internal'
            and cfg.weight.channel_pos == 0 and cfg.weight.channel_neg == 1):
        raise ValueError('Configuration does not match the measured standalone ADC2 setup')
    target = Path(cfg.weight.calibration_path).resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = target.with_name(target.name + '.before-provisional-' + stamp)
    if target.exists():
        shutil.copy2(target, backup)
        print('Backup:', backup)
    zero = 2745.8333333333335
    loaded = 4732.5
    kg = 4015.0
    value = dict(offset=zero, scale=kg/(loaded-zero), confirmed=True,
                 provisional=True, precision_verified=False,
                 note='User-authorized temporary calibration from field logs; normal span noise check was not satisfied.',
                 recorded_zero=zero, recorded_loaded=loaded, known_load_kg=kg,
                 loaded_raw_range=334.166667, created_at=datetime.now(timezone.utc).isoformat())
    atomic_json(str(target), value)
    result = load_calibration(str(target))
    assert result.confirmed and abs((loaded-result.offset)*result.scale-kg) < 1e-6
    print('PROVISIONAL calibration saved:', target)
    print('Offset:', result.offset, 'kg/count:', result.scale)
    print('5 kg display step is NOT verified accuracy. Recalibrate after measurement-chain diagnosis.')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/host-monitor/config.yaml')
    args = parser.parse_args()
    apply(args.config)
