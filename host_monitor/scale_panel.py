"""Five static common-anode digits on two TLC5947s; no hardware imports in tests."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

from host_monitor.local_scale import atomic_json, measurement

SEGMENTS = dict(zip('0123456789', ('abcdef', 'bc', 'abdeg', 'abcdg', 'bcfg',
                                  'acdfg', 'acdefg', 'abc', 'abcdefg', 'abcdfg')))
SEGMENTS.update({' ': '', '-': 'g', 'E': 'adefg', 'r': 'eg', 'P': 'abefg',
                 'I': 'bc', 'n': 'ceg', 'o': 'cdeg', 'F': 'aefg', 'A': 'abcefg', 'd': 'bcdeg', 'C': 'adef'})
DIGIT_CHANNELS = (0, 7, 14, 24, 31)  # board nearest Pi: three left digits


def display_number(weight: float, step: int = 5) -> str:
    if not math.isfinite(weight):
        return 'Err  '
    value = int(math.copysign(math.floor(abs(weight) / step + .5) * step, weight))
    return str(value).rjust(5) if -9999 <= value <= 99999 else '-----'


def frame(text: str, brightness: int) -> list[int]:
    values = [0] * 48
    for base, char in zip(DIGIT_CHANNELS, text.rjust(5)[-5:]):
        for segment in SEGMENTS.get(char, ''):
            values[base + ord(segment) - ord('a')] = max(0, min(4095, brightness))
    return values


def serial_bits(values):
    # Farthest device first, OUT23 first, 12 bits MSB-first per output.
    for value in reversed(values):
        for shift in range(11, -1, -1):
            yield (value >> shift) & 1


class TLC5947:
    def __init__(self, cfg):
        from gpiozero import DigitalOutputDevice
        self.blank = DigitalOutputDevice(cfg.blank_pin, initial_value=True)
        self.clock = DigitalOutputDevice(cfg.clock_pin, initial_value=False)
        self.data = DigitalOutputDevice(cfg.data_pin, initial_value=False)
        self.latch = DigitalOutputDevice(cfg.latch_pin, initial_value=False)
        self.previous = None

    def write(self, values):
        if values == self.previous:
            return
        for bit in serial_bits(values):
            self.data.value = bit
            self.clock.on()
            self.clock.off()
        self.latch.on()
        self.latch.off()
        self.blank.off()
        self.previous = list(values)

    def close(self):
        self.blank.on()
        for pin in (self.clock, self.data, self.latch, self.blank):
            pin.close()


class PanelState:
    """Pure state machine. Calibration commands execute only in sampler thread."""
    def __init__(self, cfg, sampler, device_id, clock=time.monotonic):
        self.cfg, self.sampler, self.device_id, self.clock = cfg, sampler, device_id, clock
        self.state = dict(brightness=1024, tare=0.0, calibration_id='', screen=True)
        try:
            saved = json.loads(Path(cfg.state_path).read_text(encoding='utf-8'))
            if not math.isfinite(float(saved['tare'])):
                raise ValueError('invalid tare')
            brightness = max(128, min(4095, int(saved['brightness'])))
            if not isinstance(saved.get('screen'), bool) or not isinstance(saved.get('calibration_id'), str):
                raise ValueError('invalid panel state')
            self.state.update(saved)
            self.state['brightness'] = brightness
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            logging.getLogger(__name__).error('Invalid panel state; using defaults')
        self.mode = 'weight'
        self.digits = [0] * 4
        self.cursor = 0
        self.value = 5
        self.last_action = clock()
        self.lock_until = 0.0
        self.failures = 0
        self.error_until = 0.0

    def save(self):
        atomic_json(self.cfg.state_path, self.state)

    def press(self, key: str, long=False):
        now = self.clock()
        self.last_action = now
        if self.mode != 'weight':
            if key == 'power':
                self.mode = 'weight'
            elif self.mode == 'pin':
                if now < self.lock_until:
                    return
                if key in ('plus', 'minus'):
                    self.digits[self.cursor] = (self.digits[self.cursor] + (1 if key == 'plus' else -1)) % 10
                elif key == 'net':
                    self.cursor += 1
                    if self.cursor == 4:
                        pin = ''.join(map(str, self.digits))
                        self.mode = next((name for name, code in self.cfg.admin_pins.items() if code == pin), 'weight')
                        if self.mode == 'weight':
                            self.failures += 1
                            self.error_until = now + 2
                            if self.failures >= 5:
                                self.lock_until = now + 60
                                self.failures = 0
                        else:
                            self.failures = 0
                            self.value = 5
                            if self.mode == 'adc':
                                self.value = self.sampler.status().get('adc_profile', 2)
                            elif self.mode == 'anchor':
                                self.value = 0
            elif self.mode == 'adc' and key in ('plus', 'minus'):
                self.value = 1 if self.value == 2 else 2
            elif self.mode in ('span', 'anchor') and key in ('plus', 'minus'):
                self.value = max(0 if self.mode == 'anchor' else 5, min(99995, self.value + (50 if long else 5) * (1 if key == 'plus' else -1)))
            elif key == 'net' and long:
                try:
                    self.sampler.command(self.mode, self.value)
                except Exception:
                    self.error_until = now + 2
                self.mode = 'weight'
            return
        if key == 'admin' and now >= self.lock_until:
            self.mode, self.digits, self.cursor = 'pin', [0] * 4, 0
            self.state['screen'] = True
        elif key == 'power':
            self.state['screen'] = not self.state['screen']
            self.save()
        elif key in ('plus', 'minus'):
            self.state['brightness'] = max(128, min(4095, self.state['brightness'] + (256 if key == 'plus' else -256)))
            self.save()
        elif key == 'net':
            packet = measurement(self.sampler, self.device_id)
            if not long and not packet['valid']:
                self.error_until = now + 2
                return
            self.state['tare'] = 0.0 if long else packet['weightKg']
            self.state['calibration_id'] = packet['calibrationId']
            self.save()

    def text(self):
        now = self.clock()
        if self.mode != 'weight' and now - self.last_action > 60:
            self.mode = 'weight'
        if not self.state['screen']:
            return '     '
        if now < self.error_until or (self.mode == 'weight' and self.sampler.status().get('command_error')):
            return 'Err  '
        if self.mode == 'weight' and self.sampler.status().get('command_busy'):
            return '-----'
        if self.mode == 'pin':
            digits = list(map(str, self.digits))
            if int(now * 2) % 2:
                digits[self.cursor] = ' '
            return 'P' + ''.join(digits)
        if self.mode == 'zero':
            return '    0'
        if self.mode in ('span', 'anchor'):
            return display_number(self.value)
        if self.mode == 'adc':
            return 'AdC ' + str(self.value)
        packet = measurement(self.sampler, self.device_id)
        if self.state['calibration_id'] != packet['calibrationId']:
            self.state['tare'] = 0.0
            self.state['calibration_id'] = packet['calibrationId']
            self.save()
        if not packet['valid']:
            return '-----'
        return display_number(packet['weightKg'] - self.state['tare'])


class ScalePanel:
    def __init__(self, cfg, sampler, device_id):
        from gpiozero import Button
        self.driver = TLC5947(cfg)
        self.buttons = {name: Button(pin, pull_up=True, bounce_time=.05) for name, pin in
                        [('minus', cfg.minus_pin), ('plus', cfg.plus_pin), ('power', cfg.power_pin), ('net', cfg.net_pin)]}
        self.state = PanelState(cfg, sampler, device_id)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='scale-panel')

    def start(self):
        self.thread.start()

    def run(self):
        down, consumed, repeated = {}, set(), {}
        try:
            while not self.stop_event.wait(.03):
                now = time.monotonic()
                pressed = {key for key, button in self.buttons.items() if button.is_pressed}
                for key in pressed:
                    down.setdefault(key, now)
                chord = {'plus', 'minus'} <= pressed
                if chord and not {'plus', 'minus'} & consumed and now - max(down['plus'], down['minus']) >= 3:
                    self.state.press('admin')
                    consumed.update(('plus', 'minus'))
                for key in pressed:
                    if key in ('plus', 'minus') and not chord and self.state.mode in ('span', 'anchor') and key in consumed and now - repeated.get(key, 0) >= .15:
                        self.state.press(key, long=True)
                        repeated[key] = now
                    if key not in consumed and not (chord and key in ('plus', 'minus')) and now - down[key] >= 2:
                        self.state.press(key, long=True)
                        consumed.add(key)
                        repeated[key] = now
                for key in set(down) - pressed:
                    if key not in consumed:
                        self.state.press(key)
                    down.pop(key)
                    consumed.discard(key)
                self.driver.write(frame(self.state.text(), self.state.state['brightness']))
        except Exception:
            logging.getLogger(__name__).exception('Panel stopped; weight sampler and LAN remain active')
        finally:
            self.driver.close()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        for button in self.buttons.values():
            button.close()
