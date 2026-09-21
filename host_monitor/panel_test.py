"""Run with host-monitor stopped, after the electrical checks in WITH_TABLET.md."""
import argparse
import time
from host_monitor.config import load_config
from host_monitor.scale_panel import TLC5947, frame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/host-monitor/config.yaml')
    args = parser.parse_args()
    driver = TLC5947(load_config(args.config).panel)
    try:
        # Walk every connected segment at low duty first.
        for digit in range(5):
            for segment in 'abcdefg':
                values = [0] * 48
                values[(0,7,14,24,31)[digit] + ord(segment)-ord('a')] = 256
                driver.write(values)
                print(f'digit {digit+1}, segment {segment}', flush=True)
                time.sleep(.5)
        for text in ('12345', '-1235', '    0', '-----', '88888'):
            driver.write(frame(text, 256))
            print(text, flush=True)
            time.sleep(2)
    finally:
        driver.close()


if __name__ == '__main__':
    main()
