"""Interactive two-point calibration; stop host-monitor before running."""
import argparse
import subprocess
import sys
from host_monitor.config import load_config
from host_monitor.main import _build_weight_reader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/host-monitor/config.yaml')
    args = parser.parse_args()
    if sys.platform == 'linux' and subprocess.run(['systemctl', 'is-active', '--quiet', 'host-monitor']).returncode == 0:
        parser.error('Stop host-monitor before calibration: sudo systemctl stop host-monitor')
    cfg = load_config(args.config)
    if not cfg.weight.require_calibration:
        parser.error('Use the standalone profile with require_calibration: true')
    reader = _build_weight_reader(cfg)
    input('Empty the mixer completely, keep stationary; press Enter to capture ZERO: ')
    reader.panel_calibrate('zero', 0)
    value = float(input('Apply a known stationary load, enter its mass in kg: '))
    if input(f'Confirm known load {value:g} kg (type YES): ') != 'YES':
        print('Cancelled; previous calibration unchanged')
        return
    reader.panel_calibrate('span', value)
    print('Calibration saved:', cfg.weight.calibration_path, 'revision:', reader.calibration_id)
    print('Verify several loads before use. Restart host-monitor to resume.')


if __name__ == '__main__':
    main()
