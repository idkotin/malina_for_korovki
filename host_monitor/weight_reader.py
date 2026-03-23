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


@dataclass(frozen=True)
class WeightCfg:
    enabled: bool
    driver: str
    calibration_path: str
    simulate: bool
    waveshare_path: str
    channel_pos: int
    channel_neg: int
    sample_count: int
    adc_rate: str


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
    Placeholder until ADC HAT arrives.
    - When simulate=true, returns a slowly changing fake value.
    - When enabled but not implemented, returns error.
    """

    def __init__(self, cfg: WeightCfg):
        self._cfg = cfg
        self._cal = load_calibration(cfg.calibration_path)
        self._t = 0
        self._adc_mod: ModuleType | None = None
        self._adc_dev = None
        self._adc_ready = False

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
        self._adc_dev.ADS1263_init_ADC1(self._cfg.adc_rate)
        self._adc_ready = True
        log.info("ADS1263 initialized rate=%s", self._cfg.adc_rate)

    def _read_ads1263_raw(self) -> float:
        self._init_ads1263()
        assert self._adc_mod is not None
        assert self._adc_dev is not None

        # Library API names vary slightly across versions; keep robust fallback.
        read_one = getattr(self._adc_dev, "ADS1263_GetChannalValue", None)
        if read_one is None:
            read_one = getattr(self._adc_dev, "ADS1263_GetChannelValue", None)
        if read_one is None:
            raise RuntimeError("ADS1263 channel read method not found")

        values: list[float] = []
        n = max(1, int(self._cfg.sample_count))
        for _ in range(n):
            v_pos = float(read_one(int(self._cfg.channel_pos)))
            v_neg = float(read_one(int(self._cfg.channel_neg)))
            values.append(v_pos - v_neg)
        return sum(values) / len(values)

    def read_raw(self) -> float:
        if self._cfg.driver.lower() == "ads1263" and not self._cfg.simulate:
            return self._read_ads1263_raw()
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

