from __future__ import annotations

from collections import deque
import json
import hashlib
import logging
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

from host_monitor.models import Weight
from host_monitor.weight_display import WeightDisplay
from host_monitor.adaptive_weight_filter import AdaptiveWeightFilter


log = logging.getLogger("host_monitor.weight")
ADC_FULL_SCALE = float(0x7FFFFFFF)


@dataclass(frozen=True)
class WeightCfg:
    enabled: bool
    driver: str
    calibration_path: str
    simulate: bool
    waveshare_path: str
    frontend: str
    reference_mode: str
    ref_pos: int
    ref_neg: int
    channel_pos: int
    channel_neg: int
    sample_count: int
    adc_rate: str
    adc2_rate: str
    adc2_gain: int = 1
    trim_fraction: float = 0.1
    smoothing_alpha: float = 0.12
    fast_smoothing_alpha: float = 0.45
    fast_change_threshold_kg: float = 30.0
    zero_deadband_kg: float = 10.0
    median_window: int = 5
    adaptive_filter: bool = False
    adc_burst: bool = False
    adc_profiles: bool = False
    min_ref_abs: float = 1e-9
    invalid_below_kg: float | None = -1000.0
    invalid_above_kg: float | None = None
    require_calibration: bool = False


@dataclass
class ScaleCalibration:
    offset: float = 0.0
    scale: float = 1.0  # kg per raw_unit
    confirmed: bool = False
    adc2_gain: int = 1
    frontend: str = 'adc2'
    provisional: bool = False


def load_calibration(path: str) -> ScaleCalibration:
    p = Path(path)
    if not p.exists():
        return ScaleCalibration()
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        offset, scale = float(obj['offset']), float(obj['scale'])
        if not math.isfinite(offset) or not math.isfinite(scale) or scale == 0:
            raise ValueError('Invalid calibration')
        return ScaleCalibration(offset=offset, scale=scale, confirmed=obj.get('confirmed') is True,
                                adc2_gain=int(obj.get('adc2_gain', 1)), frontend=obj.get('frontend', 'adc2'),
                                provisional=obj.get('provisional') is True)
    except Exception:
        return ScaleCalibration()


def save_calibration(path: str, cal: ScaleCalibration) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    from host_monitor.local_scale import atomic_json
    atomic_json(str(p), {'offset': cal.offset, 'scale': cal.scale, 'confirmed': cal.confirmed,
                         'adc2_gain': cal.adc2_gain, 'frontend': cal.frontend,
                         'provisional': cal.provisional, 'precision_verified': False})


class WeightReader:
    """
    Weight pipeline for ADS1263.

    Recommended mode:
    - adc2 frontend
    - internal reference
    - passive parallel tap to an existing terminal:
      SIG+ -> IN0, SIG- -> IN1, terminal E- -> AVSS/GND
    """

    def __init__(self, cfg: WeightCfg):
        self._base_cfg = cfg
        self._profile_path = cfg.calibration_path + '.mode.json'
        if cfg.adc_profiles:
            if cfg.frontend != 'adc2' or cfg.reference_mode != 'internal':
                raise ValueError('ADC profiles require an ADC2/internal base configuration')
            try:
                profile = json.loads(Path(self._profile_path).read_text())['adc']
            except FileNotFoundError:
                profile = 2
            if profile not in (1, 2):
                raise ValueError('Invalid saved ADC profile')
            cfg = self._profile_cfg(profile)
        self._cfg = cfg
        self._cal = load_calibration(cfg.calibration_path)
        self._t = 0
        self._adc_mod: ModuleType | None = None
        self._adc_dev = None
        self._adc_ready = False
        self._ads_measurement_channel: int | None = None
        self._ads_measurement_sign = 1.0
        self._filtered_weight: float | None = None
        self._recent_weights: deque[float] = deque(maxlen=max(1, int(cfg.median_window)))
        self._invalid_weight_reads = 0
        self._reseed_after_invalid = False
        self._reseed_valid_reads = 0
        self._display = WeightDisplay()
        self._last_read_time = None
        self._burst_active = False
        self._read_failure_count = 0
        self._adaptive_filter = AdaptiveWeightFilter()
        self._restore_pending_zero()
        log.info('Weight filter: %s', 'adaptive-v1' if cfg.adaptive_filter else 'legacy')

    def _reset_filter(self):
        self._adaptive_filter.reset()
        self._filtered_weight = None
        self._recent_weights.clear()
        self._display.reset()
        self._reseed_after_invalid = False
        self._reseed_valid_reads = 0

    def reload_calibration(self) -> None:
        self._cal = load_calibration(self._cfg.calibration_path)
        self._reset_filter()
        self._restore_pending_zero()

    def _restore_pending_zero(self):
        self.__dict__.pop('_pending_zero', None)
        self.__dict__.pop('_pending_zero_noise', None)
        if not self._cfg.adc_profiles:
            return
        try:
            saved = json.loads(Path(self._cfg.calibration_path + '.pending-zero.json').read_text())
            if not isinstance(saved, dict):
                raise ValueError('Invalid saved zero document')
            if saved.get('active') is not True or saved.get('calibration_id') != self.calibration_id:
                return
            raw, noise = float(saved['raw']), float(saved['noise'])
            if not math.isfinite(raw) or not math.isfinite(noise) or noise < 0:
                raise ValueError('Invalid saved zero')
            self._pending_zero, self._pending_zero_noise = raw, noise
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, KeyError):
            log.warning('Saved calibration zero is invalid; capture zero again')

    def _clear_pending_zero(self):
        self.__dict__.pop('_pending_zero', None)
        self.__dict__.pop('_pending_zero_noise', None)
        if self._cfg.adc_profiles:
            from host_monitor.local_scale import atomic_json
            atomic_json(self._cfg.calibration_path + '.pending-zero.json', {'active': False})

    def _remember_pending_zero(self, raw, noise):
        if self._cfg.adc_profiles:
            from host_monitor.local_scale import atomic_json
            atomic_json(self._cfg.calibration_path + '.pending-zero.json',
                        {'active': True, 'calibration_id': self.calibration_id,
                         'raw': raw, 'noise': noise, 'captured_at_ms': int(time.time()*1000)})
        self._pending_zero, self._pending_zero_noise = raw, noise

    @property
    def calibrated(self):
        gain_matches = self._cfg.frontend.lower() != 'adc2' or self._cal.adc2_gain == self._cfg.adc2_gain
        profile_matches = not self._cfg.adc_profiles or self._cal.frontend == self._cfg.frontend
        return gain_matches and profile_matches and (not self._cfg.require_calibration or self._cal.confirmed)

    @property
    def adc_profile(self):
        return 2 if self._cfg.frontend == 'adc2' else 1

    def _profile_cfg(self, profile):
        if profile == 2:
            return self._base_cfg
        return replace(self._base_cfg, frontend='adc1', adc_rate='ADS1263_100SPS',
                       calibration_path=self._base_cfg.calibration_path + '.adc1.json',
                       adc_burst=True, require_calibration=True)

    def switch_adc(self, profile):
        if not self._base_cfg.adc_profiles or profile not in (1, 2):
            raise ValueError('ADC profile unavailable')
        old_cfg = self._cfg
        self._cfg = self._profile_cfg(profile)
        try:
            self._adc_ready = False
            self._init_ads1263()
            from host_monitor.local_scale import atomic_json
            atomic_json(self._profile_path, {'adc': profile})
        except Exception:
            self._cfg = old_cfg
            self._adc_ready = False
            raise
        finally:
            self.__dict__.pop('_pending_zero', None)
            self.__dict__.pop('_pending_zero_noise', None)
            self.reload_calibration()
        log.info('ADC profile changed to %s; calibrated=%s', profile, self.calibrated)

    @property
    def calibration_id(self):
        identity = (self._cal.offset, self._cal.scale, self._cfg.frontend,
                                    self._cfg.reference_mode, self._cfg.channel_pos,
                                    self._cfg.channel_neg, self._cfg.ref_pos,
                                    self._cfg.ref_neg)
        if self._cfg.frontend.lower() == 'adc2' and self._cfg.adc2_gain != 1:
            identity += (self._cfg.adc2_gain,)
        if self._cfg.adc_profiles and self.adc_profile == 1:
            identity += ('adc1-internal-gain32-input-counts-v1',)
        return hashlib.sha256(repr(identity).encode()).hexdigest()[:24]

    def panel_calibrate(self, action, value):
        if action == 'adc':
            self.switch_adc(int(value))
            return
        if not self._cfg.enabled or self._cfg.simulate:
            raise ValueError('Calibration needs enabled real sensors')
        if action not in ('zero', 'span', 'anchor'):
            raise ValueError('Unknown calibration command')
        if action == 'anchor':
            if not self._cfg.adc_profiles or self.adc_profile != 1:
                raise ValueError('One-point calibration is only available for ADC1')
            if not math.isfinite(value) or not 0 <= value <= 99995:
                raise ValueError('Invalid known weight')
            inherited = not (self._cal.confirmed and self._cal.frontend == 'adc1')
            source = load_calibration(self._base_cfg.calibration_path) if inherited else self._cal
            if not source.confirmed or (inherited and source.adc2_gain != self._base_cfg.adc2_gain):
                raise ValueError('Confirmed source calibration required')
        if action in ('zero', 'anchor'):
            # A failed retry must not leave an older zero eligible for span.
            self._clear_pending_zero()
        if action == 'span' and not hasattr(self, '_pending_zero'):
            raise ValueError('Capture empty machine zero first')
        samples = [self.read_raw() for _ in range(60)]
        if not all(math.isfinite(x) for x in samples):
            raise ValueError('Non-finite calibration signal')
        raw = statistics.median(samples)
        spread = max(samples) - min(samples)
        # ADC2 returns counts; ADC1 returns a normalized ratio. Do not use
        # the field ADC2 noise allowance for a different measurement scale.
        standalone_adc2 = self._cfg.require_calibration and (self._cfg.frontend.lower() == 'adc2' or self._cfg.adc_profiles)
        limit = max(500 if standalone_adc2 else 10, abs(raw) * .002)
        # Estimate a trend from the entire capture, not two short end windows.
        # The noise estimate uses residuals so an actual ramp cannot increase
        # its own allowed drift. This is a screening rule, not an accuracy claim.
        center = (len(samples) - 1) / 2
        mean = statistics.mean(samples)
        sxx = sum((i - center) ** 2 for i in range(len(samples)))
        slope = sum((i - center) * (x - mean) for i, x in enumerate(samples)) / sxx
        residuals = [x - (mean + slope * (i - center)) for i, x in enumerate(samples)]
        residual_center = statistics.median(residuals)
        sigma = 1.4826 * statistics.median(abs(x - residual_center) for x in residuals)
        drift = abs(slope) * (len(samples) - 1)
        drift_limit = max(20 if standalone_adc2 else 0,
                          4 * sigma * (len(samples) - 1) / math.sqrt(sxx))
        log.info('Calibration %s median=%.6f range=%.6f limit=%.6f drift=%.6f drift_limit=%.6f',
                 action, raw, spread, limit, drift, drift_limit)
        if spread > limit or drift > drift_limit:
            raise ValueError(f'Unstable calibration load: range={spread:.2f}/{limit:.2f}, drift={drift:.2f}/{drift_limit:.2f}')
        if action == 'zero':
            self._remember_pending_zero(raw, spread)
            # Existing valid calibration stays intact until known-load confirmation.
        elif action == 'anchor':
            # Both profiles use input-referred ADC2-equivalent counts. Preserve
            # established ADC1 slope (initially inherited from ADC2), changing
            # only ADC1's offset in the field.
            cal = ScaleCalibration(raw - value/source.scale, source.scale, True,
                                   self._cfg.adc2_gain, 'adc1', provisional=inherited or source.provisional)
            save_calibration(self._cfg.calibration_path, cal)
            self._cal = cal
            self._reset_filter()
            if value == 0:
                self._remember_pending_zero(raw, spread)
            log.warning('ADC1 one-point anchor: known_kg=%s; scale=%s; inherited=%s; provisional=%s',
                        value, source.scale, inherited, cal.provisional)
        elif action == 'span':
            if not hasattr(self, '_pending_zero'):
                raise ValueError('Capture empty machine zero first')
            delta = raw - self._pending_zero
            minimum_delta = max(10, 10 * max(spread, self._pending_zero_noise))
            if not math.isfinite(value) or value <= 0 or abs(delta) < minimum_delta:
                raise ValueError(f'Known load too small relative to noise: delta={abs(delta):.2f}, required={minimum_delta:.2f}')
            cal = ScaleCalibration(self._pending_zero, value / delta, True, self._cfg.adc2_gain, self._cfg.frontend)
            save_calibration(self._cfg.calibration_path, cal)
            self._cal = cal
            self._reset_filter()
            self._clear_pending_zero()

    def prepare(self) -> None:
        self._init_ads1263()

    def uses_passive_parallel_mode(self) -> bool:
        return (
            self._cfg.driver.lower() == "ads1263"
            and not self._cfg.simulate
            and self._cfg.frontend.lower() == "adc2"
            and self._cfg.reference_mode.lower() == "internal"
        )

    def _init_ads1263(self) -> None:
        if self._adc_ready:
            return
        waveshare_path = Path(self._cfg.waveshare_path)
        if not waveshare_path.exists():
            raise RuntimeError(f"waveshare path not found: {waveshare_path}")
        if str(waveshare_path) not in sys.path:
            sys.path.insert(0, str(waveshare_path))
        try:
            import ADS1263  # type: ignore
        except Exception as e:
            raise RuntimeError(f"cannot import ADS1263 from {waveshare_path}: {e}") from e

        self._adc_mod = ADS1263
        self._adc_dev = ADS1263.ADS1263()
        frontend = self._cfg.frontend.lower()
        if frontend == "adc2":
            init_result = self._adc_dev.ADS1263_init_ADC2(self._cfg.adc2_rate)
            if init_result == -1:
                raise RuntimeError("ADS1263_init_ADC2 failed")
        else:
            init_result = self._adc_dev.ADS1263_init_ADC1(self._cfg.adc_rate)
            if init_result == -1:
                raise RuntimeError("ADS1263_init_ADC1 failed")
        self._configure_ads1263_weight_mode(frontend)
        self._adc_ready = True
        log.info(
            "ADS1263 initialized frontend=%s adc1_rate=%s adc2_rate=%s reference=%s",
            frontend,
            self._cfg.adc_rate,
            self._cfg.adc2_rate,
            self._cfg.reference_mode,
        )

    def _to_signed32(self, value: int) -> int:
        if value & 0x80000000:
            return int(value - (1 << 32))
        return int(value)

    def _to_signed24(self, value: int) -> int:
        if value & 0x800000:
            return int(value - (1 << 24))
        return int(value)

    def _diff_channel_from_ain_pair(self, pos: int, neg: int) -> tuple[int, float]:
        if abs(pos - neg) != 1:
            raise ValueError(f"diff pair must be adjacent INx numbers (got {pos} and {neg})")
        base = min(pos, neg)
        if base % 2 != 0:
            base -= 1
        channel = base // 2
        if channel < 0 or channel > 4:
            raise ValueError(f"IN pair {pos}/{neg} not supported by ADS1263 diff channels")

        adc_pos = channel * 2
        adc_neg = channel * 2 + 1
        if pos == adc_pos and neg == adc_neg:
            return channel, 1.0
        if pos == adc_neg and neg == adc_pos:
            return channel, -1.0
        return channel, 1.0

    def _refmux_from_ain_pair(self, pos: int, neg: int) -> tuple[int, bool]:
        ref_reverse = False
        if (pos, neg) == (0, 1):
            rmux_p, rmux_n = 0x01, 0x01
        elif (pos, neg) == (1, 0):
            rmux_p, rmux_n = 0x01, 0x01
            ref_reverse = True
        elif (pos, neg) == (2, 3):
            rmux_p, rmux_n = 0x02, 0x02
        elif (pos, neg) == (3, 2):
            rmux_p, rmux_n = 0x02, 0x02
            ref_reverse = True
        elif (pos, neg) == (4, 5):
            rmux_p, rmux_n = 0x03, 0x03
        elif (pos, neg) == (5, 4):
            rmux_p, rmux_n = 0x03, 0x03
            ref_reverse = True
        else:
            raise ValueError(
                "ADS1263 external reference supports only AIN0/AIN1, AIN2/AIN3, or AIN4/AIN5 "
                f"(got {pos}/{neg})"
            )
        return (rmux_p << 3) | rmux_n, ref_reverse

    def _configure_ads1263_weight_mode(self, frontend: str) -> None:
        assert self._adc_mod is not None
        assert self._adc_dev is not None

        regs = getattr(self._adc_mod, "ADS1263_REG", None)
        cmds = getattr(self._adc_mod, "ADS1263_CMD", None)
        delays = getattr(self._adc_mod, "ADS1263_DELAY", None)
        adc2_rates = getattr(self._adc_mod, "ADS1263_ADC2_DRATE", None)
        adc2_gains = getattr(self._adc_mod, "ADS1263_ADC2_GAIN", None)
        if regs is None or cmds is None:
            raise RuntimeError("ADS1263 register definitions not found in Waveshare module")

        meas_ch, meas_sign = self._diff_channel_from_ain_pair(self._cfg.channel_pos, self._cfg.channel_neg)

        if self._cfg.adc_burst or (self._cfg.adc_profiles and frontend == 'adc1'):
            for name, val in [('REG_INTERFACE', 0x05), ('REG_POWER', 0x01)]:
                self._adc_dev.ADS1263_WriteReg(regs[name], val)
                if self._adc_dev.ADS1263_ReadData(regs[name])[0] != val:
                    raise RuntimeError(f'{name} verification failed')
            time.sleep(.05)  # internal reference settling after reset

        if self._cfg.adc_profiles and frontend == 'adc1':
            self._adc_dev.ADS1263_WriteCmd(cmds['CMD_STOP1'])
            self._adc_dev.ADS1263_WriteCmd(cmds['CMD_STOP2'])
            # Internal reference, PGA32, 100 SPS, sinc3; no change to wiring.
            settings = {'REG_MODE0': 0, 'REG_MODE1': 0x40, 'REG_MODE2': 0x57,
                        'REG_REFMUX': 0, 'REG_INPMUX': (meas_ch*2 << 4) | (meas_ch*2+1)}
            for name, val in settings.items():
                self._adc_dev.ADS1263_WriteReg(regs[name], val)
                if self._adc_dev.ADS1263_ReadData(regs[name])[0] != val:
                    raise RuntimeError(f'{name} verification failed')
            self._ads_measurement_channel, self._ads_measurement_sign = meas_ch, meas_sign
            return

        if frontend == "adc2":
            if delays is None or adc2_rates is None or adc2_gains is None:
                raise RuntimeError("ADS1263 ADC2 definitions not found in Waveshare module")
            reference_mode = self._cfg.reference_mode.lower()
            if reference_mode not in {"internal", "avdd"}:
                raise ValueError(f"unsupported reference_mode: {self._cfg.reference_mode}")
            ref_flag = 0x00 if reference_mode == "internal" else 0x20
            gain_key = f"ADS1263_ADC2_GAIN_{self._cfg.adc2_gain}"
            if gain_key not in adc2_gains:
                raise ValueError(f"unsupported ADC2 gain: {self._cfg.adc2_gain}")
            adc2cfg = ref_flag | (adc2_rates[self._cfg.adc2_rate] << 6) | adc2_gains[gain_key]
            self._adc_dev.ADS1263_SetMode(1)
            self._adc_dev.ADS1263_WriteCmd(cmds["CMD_STOP2"])
            self._adc_dev.ADS1263_WriteReg(regs["REG_ADC2CFG"], adc2cfg)
            if self._adc_dev.ADS1263_ReadData(regs["REG_ADC2CFG"])[0] != adc2cfg:
                raise RuntimeError("ADC2 gain/rate/reference register verification failed")
            self._adc_dev.ADS1263_WriteReg(regs["REG_MODE0"], delays["ADS1263_DELAY_8d8ms"])
            self._ads_measurement_channel = meas_ch
            self._ads_measurement_sign = meas_sign
            return

        refmux, ref_reverse = self._refmux_from_ain_pair(self._cfg.ref_pos, self._cfg.ref_neg)
        self._adc_dev.ADS1263_WriteCmd(cmds["CMD_STOP1"])
        self._adc_dev.ADS1263_SetMode(1)
        try:
            mode0 = 0x80 if ref_reverse else 0x00
            self._adc_dev.ADS1263_WriteReg(regs["REG_MODE0"], mode0)
            self._adc_dev.ADS1263_WriteReg(regs["REG_REFMUX"], refmux)
        except Exception as e:
            log.warning("ADS1263 external reference configuration was not confirmed, continuing anyway: %s", e)
        self._adc_dev.ADS1263_SetDiffChannal(meas_ch)
        self._adc_dev.ADS1263_WriteCmd(cmds["CMD_START1"])
        self._ads_measurement_channel = meas_ch
        self._ads_measurement_sign = meas_sign

    def _read_ads1263_diff(self, diff_channel_index: int) -> float:
        self._init_ads1263()
        assert self._adc_mod is not None
        assert self._adc_dev is not None

        if self._burst_active:
            from host_monitor.adc_transport import read_conversion
            return float(read_conversion(self._adc_mod, self._adc_dev, self.adc_profile))

        if self._cfg.frontend.lower() == "adc2":
            return self._read_ads1263_diff_adc2(diff_channel_index)

        read_one = getattr(self._adc_dev, "ADS1263_GetChannalValue", None)
        if read_one is None:
            read_one = getattr(self._adc_dev, "ADS1263_GetChannelValue", None)
        if read_one is None:
            raise RuntimeError("ADS1263 channel read method not found")
        value = read_one(int(diff_channel_index))
        return float(self._to_signed32(int(value)))

    def _read_ads1263_diff_adc2(self, diff_channel_index: int) -> float:
        assert self._adc_mod is not None
        assert self._adc_dev is not None
        cmds = getattr(self._adc_mod, "ADS1263_CMD", None)
        set_diff = getattr(self._adc_dev, "ADS1263_SetDiffChannal_ADC2", None)
        read_fn = getattr(self._adc_dev, "ADS1263_Read_ADC2_Data", None)
        if cmds is None or set_diff is None or read_fn is None:
            raise RuntimeError("ADS1263 ADC2 methods not found")

        set_diff(int(diff_channel_index))
        self._adc_dev.ADS1263_WriteCmd(cmds["CMD_START2"])
        try:
            value = read_fn()
        finally:
            self._adc_dev.ADS1263_WriteCmd(cmds["CMD_STOP2"])
        return float(self._to_signed24(int(value)))

    def read_raw_counts(self) -> int:
        if self._cfg.driver.lower() != "ads1263" or self._cfg.simulate:
            raise RuntimeError("raw ADS1263 counts are available only for the real ADS1263 driver")
        self._init_ads1263()
        assert self._ads_measurement_channel is not None
        counts = float(self._read_ads1263_diff(self._ads_measurement_channel))
        return int(counts * self._ads_measurement_sign)

    def read_ratio(self) -> float:
        if self._cfg.driver.lower() != "ads1263" or self._cfg.simulate:
            raise RuntimeError("ratio is available only for the real ADS1263 driver")

        counts: list[int] = []
        n = max(1, int(self._cfg.sample_count))
        burst = self._cfg.adc_burst
        if burst:
            self._init_ads1263()
            # Restart once per batch so the first result is fresh after idle.
            cmds = self._adc_mod.ADS1263_CMD
            adc = self.adc_profile
            self._adc_dev.ADS1263_WriteCmd(cmds[f'CMD_STOP{adc}'])
            if adc == 2:
                self._adc_dev.ADS1263_SetDiffChannal_ADC2(self._ads_measurement_channel)
            self._adc_dev.ADS1263_WriteCmd(cmds[f'CMD_START{adc}'])
            self._burst_active = True
        try:
            for _ in range(n):
                counts.append(self.read_raw_counts())
        finally:
            if burst:
                self._burst_active = False
                self._adc_dev.ADS1263_WriteCmd(cmds[f'CMD_STOP{adc}'])

        if self._cfg.trim_fraction > 0 and len(counts) >= 5:
            counts.sort()
            k = int(len(counts) * float(self._cfg.trim_fraction))
            if k > 0 and len(counts) - 2 * k > 0:
                counts = counts[k : len(counts) - k]

        avg_counts = float(sum(counts) / len(counts))
        if self._cfg.frontend.lower() == "adc2":
            # Preserve input-referred raw units across PGA gains. The offset
            # still depends on gain, so calibration must match adc2_gain.
            return avg_counts / self._cfg.adc2_gain
        if self._cfg.adc_profiles:
            # Input-referred ADC2-equivalent counts, only for independent ADC1 calibration.
            return avg_counts / (256 * 32)
        return avg_counts / ADC_FULL_SCALE

    def read_raw(self) -> float:
        if self._cfg.driver.lower() == "ads1263" and not self._cfg.simulate:
            return self.read_ratio()

        if self._cfg.simulate:
            self._t += 1
            base = 1000.0 + 50.0 * (random.random() - 0.5)
            drift = (self._t % 200) / 200.0
            return base + drift * 10.0
        raise RuntimeError("weight driver not implemented yet (disable weight or enable simulate)")

    def _is_valid_weight_value(self, value: float) -> bool:
        if not math.isfinite(value):
            return False
        if self._cfg.invalid_below_kg is not None and value < float(self._cfg.invalid_below_kg):
            return False
        if self._cfg.invalid_above_kg is not None and value > float(self._cfg.invalid_above_kg):
            return False
        return True

    def read_weight(self) -> Weight:
        if not self._cfg.enabled:
            return Weight(weight=None)
        try:
            raw = self.read_raw()
            self._read_failure_count = 0
            if self._cfg.adc_profiles and not self.calibrated:
                self._reset_filter()
                return Weight(weight=None)
            if self._cfg.frontend.lower() == 'adc2' and self._cal.adc2_gain != self._cfg.adc2_gain:
                self._reset_filter()
                return Weight(weight=None)
            now = time.monotonic()
            if self._last_read_time is not None and now - self._last_read_time > 3:
                self._reset_filter()
            self._last_read_time = now
            value = (raw - self._cal.offset) * self._cal.scale
            if not self._is_valid_weight_value(float(value)):
                self._adaptive_filter.reset()
                self._filtered_weight = None
                self._recent_weights.clear()
                self._display.reset()
                self._invalid_weight_reads += 1
                self._reseed_after_invalid = True
                self._reseed_valid_reads = 0
                if self._invalid_weight_reads == 1 or self._invalid_weight_reads % 60 == 0:
                    log.warning("weight value rejected as invalid: %s", value)
                return Weight(weight=None, raw=float(value))

            self._invalid_weight_reads = 0
            if self._cfg.adaptive_filter:
                filtered = self._adaptive_filter.update(float(value), now)
                return Weight(weight=self._display.update(filtered), raw=float(value))
            if self._reseed_after_invalid:
                self._recent_weights.append(float(value))
                self._reseed_valid_reads += 1
                if self._reseed_valid_reads == 1:
                    self._filtered_weight = float(value)
                    return Weight(weight=self._display.update(float(value)), raw=float(value))

                median_value = float(statistics.median(self._recent_weights))
                self._filtered_weight = median_value
                if self._reseed_valid_reads >= 2:
                    self._reseed_after_invalid = False
                    self._reseed_valid_reads = 0
                return Weight(weight=self._display.update(median_value), raw=float(value))

            alpha = max(0.0, min(1.0, float(self._cfg.smoothing_alpha)))
            fast_alpha = max(alpha, min(1.0, float(self._cfg.fast_smoothing_alpha)))
            fast_threshold = max(0.0, float(self._cfg.fast_change_threshold_kg))
            zero_deadband = max(0.0, float(self._cfg.zero_deadband_kg))
            self._recent_weights.append(float(value))
            median_value = statistics.median(self._recent_weights)
            if self._filtered_weight is None:
                self._filtered_weight = float(median_value)
            else:
                delta = abs(float(median_value) - float(self._filtered_weight))
                selected_alpha = fast_alpha if delta >= fast_threshold else alpha
                self._filtered_weight = float(
                    selected_alpha * median_value + (1.0 - selected_alpha) * self._filtered_weight
                )
            display_weight = float(self._filtered_weight)
            if abs(display_weight) <= zero_deadband and abs(float(median_value)) <= zero_deadband:
                display_weight = 0.0
                self._filtered_weight = 0.0
            return Weight(weight=self._display.update(display_weight), raw=float(value))
        except Exception as e:
            from host_monitor.adc_transport import ADCSignalError
            if self._cfg.adc_burst and not isinstance(e, ADCSignalError):
                self._adc_ready = False
            self._reset_filter()
            self._read_failure_count += 1
            if self._read_failure_count == 1 or self._read_failure_count % 60 == 0:
                log.warning("weight read failed (%s consecutive): %s", self._read_failure_count, e)
            return Weight(weight=None)

    def tare(self) -> float:
        raw = self.read_raw()
        self._cal.offset = float(raw)
        self._reset_filter()
        save_calibration(self._cfg.calibration_path, self._cal)
        return self._cal.offset

    def calibrate_with_known(self, known_kg: float) -> float:
        if known_kg <= 0:
            raise ValueError("known_kg must be > 0")
        raw = self.read_raw()
        delta = raw - self._cal.offset
        if abs(delta) < 1e-9:
            raise RuntimeError("calibration delta too small; check load is applied")
        self._cal.scale = float(known_kg / delta)
        self._reset_filter()
        save_calibration(self._cfg.calibration_path, self._cal)
        return self._cal.scale
