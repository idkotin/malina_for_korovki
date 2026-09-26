"""Apply only the reviewed filter parameters to the existing live configuration.

Run with the service stopped: sudo .venv/bin/python apply-weight-filter.py
"""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import tempfile
import yaml
from host_monitor.config import AppCfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/host-monitor/config.yaml')
    args = parser.parse_args()
    path = Path(args.config)
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not config.get('local_scale', {}).get('enabled'):
        raise SystemExit('Standalone local_scale must be enabled; refusing legacy configuration')
    config['weight'].update(median_window=5, smoothing_alpha=.2,
                            fast_smoothing_alpha=.65, fast_change_threshold_kg=60,
                            zero_deadband_kg=0)
    AppCfg.model_validate(config)
    backup = path.with_name(path.name + '.before-filter-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(path, name)
        if hasattr(os, 'chown'):
            info = path.stat()
            os.chown(name, info.st_uid, info.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)
    print('Filter updated. Calibration, panel, network and queue unchanged. Backup:', backup)


if __name__ == '__main__': main()
