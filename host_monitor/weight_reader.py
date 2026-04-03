from __future__ import annotations

import json
import logging
import random
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
    # Bridge reference differential inputs (E+ - E-)
    ref_pos: int
    ref_neg: int
    # Bridge measurement differential inputs (SIG+ - SIG-)
    channel_pos: int
    channel_neg: int
    sample_count: int
    adc_rate: str
    # Trim fraction for ratio filtering. Example: 0.1 removes 10% smallest and 10% largest samples.
    trim_fraction: float = 0.1
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
    Weight pipeline for ADS1263:
    - real ADS1263 path with external bridge reference sense
    - calibration persisted as offset + scale
    - optional simulation path for bench tests without hardware
    """

    def __init__(self, cfg: WeightCfg):
        self._cfg = cfg
        self._cal = load_calibration(cfg.calibration_path)
        self._t = 0
        self._adc_mod: ModuleType | None = None
        self._adc_dev = None
        self._adc_ready = False
        self._ads1263_measurement_channel: int | None = None
        self._ads1263_measurement_sign = 1.0

    def reload_calibration(self) -> None:
        self._cal = load_calibration(self._cfg.calibration_path)

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
        # Rate string is from Waveshare examples, e.g. ADS1263_20SPS
        init_result = self._adc_dev.ADS1263_init_ADC1(self._cfg.adc_rate)
        if init_result == -1:
            raise RuntimeError("ADS1263_init_ADC1 failed")
        self._configure_ads1263_weight_mode()
        self._adc_ready = True
        log.info("ADS1263 initialized rate=%s", self._cfg.adc_rate)

    def _to_signed32(self, v: int) -> int:
        # ADS1263 driver returns raw 32-bit words; convert to signed int.
        if v & 0x80000000:
            return int(v - (1 << 32))
        return int(v)

    def _diff_channel_from_ain_pair(self, pos: int, neg: int) -> tuple[int, float]:
        """
        ADS1263_GetChannalValue(Channel) expects Channel in [0..4] meaning:
          0 => AIN0-AIN1
          1 => AIN2-AIN3
          2 => AIN4-AIN5
          3 => AIN6-AIN7
          4 => AIN8-AIN9

        We return:
          (diff_channel_index, polarity_sign)
        polarity_sign is +1 if ADC's AIN+ corresponds to `pos`, else -1.
        """
        if abs(pos - neg) != 1:
            raise ValueError(f"diff pair must be adjacent INx numbers (got {pos} and {neg})")
        base = min(pos, neg)
        if base % 2 != 0:
            # normalize so base is even
            base = min(pos, neg) - 1
        ch = base // 2
        if ch < 0 or ch > 4:
            raise ValueError(f"IN pair {pos}/{neg} not supported by ADS1263 diff channels")

        adc_pos = ch * 2
        adc_neg = ch * 2 + 1
        # ADC always does (AIN{even} - AIN{odd})
        if pos == adc_pos and neg == adc_neg:
            return ch, 1.0
        if pos == adc_neg and neg == adc_pos:
            return ch, -1.0
        # Fallback (shouldn't happen due to adjacency check)
        return ch, 1.0

    def _refmux_from_ain_pair(self, pos: int, neg: int) -> tuple[int, bool]:
        """
        ADS1263 ADC1 external reference accepts only AIN0/AIN1, AIN2/AIN3, AIN4/AIN5.
        REFMUX uses RMUXP bits [5:3] and RMUXN bits [2:0].
        """
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

    def _configure_ads1263_weight_mode(self) -> None:
        assert self._adc_mod is not None
        assert self._adc_dev is not None

        regs = getattr(self._adc_mod, "ADS1263_REG", None)
        cmds = getattr(self._adc_mod, "ADS1263_CMD", None)
        if regs is None or cmds is None:
            raise RuntimeError("ADS1263 register definitions not found in Waveshare module")

        meas_ch, meas_sign = self._diff_channel_from_ain_pair(self._cfg.channel_pos, self._cfg.channel_neg)
        refmux, ref_reverse = self._refmux_from_ain_pair(self._cfg.ref_pos, self._cfg.ref_neg)

        # Stop conversions before changing ADC1 routing.
        self._adc_dev.ADS1263_WriteCmd(cmds["CMD_STOP1"])
        # Use differential mode so ADS1263_GetChannalValue(1) means IN2-IN3.
        self._adc_dev.ADS1263_SetMode(1)
        # Waveshare's library is inconsistent about ADC1 register readback on some boards.
        # In field use we still attempt to switch MODE0/REFMUX, but we do not abort just
        # because the helper library cannot confirm the write with a readback value.
        try:
            mode0 = 0x80 if ref_reverse else 0x00
            self._adc_dev.ADS1263_WriteReg(regs["REG_MODE0"], mode0)
            # Reference is sensed from the existing machine bridge excitation (E+ -> IN0, E- -> IN1).
            self._adc_dev.ADS1263_WriteReg(regs["REG_REFMUX"], refmux)
        except Exception as e:
            log.warning("ADS1263 external reference configuration was not confirmed, continuing anyway: %s", e)
        # Prime the measurement input mux to the configured bridge signal pair.
        self._adc_dev.ADS1263_SetDiffChannal(meas_ch)
        self._adc_dev.ADS1263_WriteCmd(cmds["CMD_START1"])
        self._ads1263_measurement_channel = meas_ch
        self._ads1263_measurement_sign = meas_sign

    def _read_ads1263_diff(self, diff_channel_index: int) -> float:
        self._init_ads1263()
        assert self._adc_mod is not None
        assert self._adc_dev is not None

        # Library API names vary slightly across versions; keep robust fallback.
        read_one = getattr(self._adc_dev, "ADS1263_GetChannalValue", None)
        if read_one is None:
            read_one = getattr(self._adc_dev, "ADS1263_GetChannelValue", None)
        if read_one is None:
            raise RuntimeError("ADS1263 channel read method not found")

        # ADS1263 returns raw ADC codes; we read a single diff channel.
        v = read_one(int(diff_channel_index))
        return float(self._to_signed32(int(v)))

    def read_raw_counts(self) -> int:
        if self._cfg.driver.lower() != "ads1263" or self._cfg.simulate:
            raise RuntimeError("raw ADS1263 counts are available only for the real ADS1263 driver")
        self._init_ads1263()
        assert self._ads1263_measurement_channel is not None
        counts = float(self._read_ads1263_diff(self._ads1263_measurement_channel))
        return int(counts * self._ads1263_measurement_sign)

    def read_ratio(self) -> float:
        if self._cfg.driver.lower() == "ads1263" and not self._cfg.simulate:
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
            # With REFMUX on IN0/IN1 the ADC code is already normalized to bridge excitation.
            return avg_counts / ADC_FULL_SCALE
        raise RuntimeError("ratio is available only for the real ADS1263 driver")

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
            return Weight(weight=float(value))
        except Exception as e:
            log.warning("weight read failed: %s", e)
            return Weight(weight=None)

    def tare(self) -> float:
        # Tare sets ratio offset (zero).
        raw = self.read_raw()
        self._cal.offset = float(raw)
        save_calibration(self._cfg.calibration_path, self._cal)
        return self._cal.offset

    def calibrate_with_known(self, known_kg: float) -> float:
        if known_kg <= 0:
            raise ValueError("known_kg must be > 0")
        raw = self.read_raw()
        delta = raw - self._cal.offset  # ratio delta
        if abs(delta) < 1e-9:
            raise RuntimeError("calibration delta too small; check load is applied")
        self._cal.scale = float(known_kg / delta)  # kg per ratio unit
        save_calibration(self._cfg.calibration_path, self._cal)
        return self._cal.scale

