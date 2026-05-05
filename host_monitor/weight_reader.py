from __future__ import annotations

from collections import deque
import json
import logging
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from host_monitor.models import Weight


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
    trim_fraction: float = 0.1
    smoothing_alpha: float = 0.12
    fast_smoothing_alpha: float = 0.35
    fast_change_threshold_kg: float = 40.0
    zero_deadband_kg: float = 10.0
    median_window: int = 7
    min_ref_abs: float = 1e-9


@dataclass
class ScaleCalibration:
    offset: float = 0.0
    scale: float = 1.0  # kg per raw_unit


def load_calibration(path: str) -> ScaleCalibration:
    p = Path(path)
    if not p.exists():
        return ScaleCalibration()
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return ScaleCalibration(offset=float(obj.get("offset", 0.0)), scale=float(obj.get("scale", 1.0)))
    except Exception:
        return ScaleCalibration()


def save_calibration(path: str, cal: ScaleCalibration) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"offset": cal.offset, "scale": cal.scale}, ensure_ascii=False, indent=2), encoding="utf-8")


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

    def reload_calibration(self) -> None:
        self._cal = load_calibration(self._cfg.calibration_path)

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

        if frontend == "adc2":
            if delays is None or adc2_rates is None or adc2_gains is None:
                raise RuntimeError("ADS1263 ADC2 definitions not found in Waveshare module")
            reference_mode = self._cfg.reference_mode.lower()
            if reference_mode not in {"internal", "avdd"}:
                raise ValueError(f"unsupported reference_mode: {self._cfg.reference_mode}")
            ref_flag = 0x00 if reference_mode == "internal" else 0x20
            adc2cfg = ref_flag | (adc2_rates[self._cfg.adc2_rate] << 6) | adc2_gains["ADS1263_ADC2_GAIN_1"]
            self._adc_dev.ADS1263_SetMode(1)
            self._adc_dev.ADS1263_WriteCmd(cmds["CMD_STOP2"])
            self._adc_dev.ADS1263_WriteReg(regs["REG_ADC2CFG"], adc2cfg)
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
        value = read_fn()
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
        for _ in range(n):
            counts.append(self.read_raw_counts())

        if self._cfg.trim_fraction > 0 and len(counts) >= 5:
            counts.sort()
            k = int(len(counts) * float(self._cfg.trim_fraction))
            if k > 0 and len(counts) - 2 * k > 0:
                counts = counts[k : len(counts) - k]

        avg_counts = float(sum(counts) / len(counts))
        if self._cfg.frontend.lower() == "adc2":
            return avg_counts
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

    def read_weight(self) -> Weight:
        if not self._cfg.enabled:
            return Weight(weight=None)
        try:
            raw = self.read_raw()
            value = (raw - self._cal.offset) * self._cal.scale
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
            return Weight(weight=display_weight)
        except Exception as e:
            log.warning("weight read failed: %s", e)
            return Weight(weight=None)

    def tare(self) -> float:
        raw = self.read_raw()
        self._cal.offset = float(raw)
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
        save_calibration(self._cfg.calibration_path, self._cal)
        return self._cal.scale
