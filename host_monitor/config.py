from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from host_monitor.models import Level


class DeviceCfg(BaseModel):
    id: str


class SendCfg(BaseModel):
    url: str
    interval_s: float = 0.5
    timeout_s: float = 3.0
    max_batch: int = 50
    idle_sleep_enabled: bool = True
    idle_after_s: float = 900.0
    idle_interval_s: float = 120.0
    movement_confirm_s: float = 5.0
    movement_speed_kmh: float = 2.0


class EventsCfg(BaseModel):
    url: str
    timeout_s: float = 5.0
    max_batch: int = 50


class BufferCfg(BaseModel):
    sqlite_path: str = "./data/buffer.sqlite3"
    max_rows: int = 200000
    max_rows_events: int = 50000


class GpsCfg(BaseModel):
    enabled: bool = True
    # Fixed device path (optional). If null, auto-detect from port_candidates.
    port: str | None = None
    # Candidates for auto-detection. You can also include ttyUSB3 and others.
    port_candidates: list[str] = Field(default_factory=lambda: ["/dev/ttyUSB1", "/dev/ttyUSB3", "/dev/ttyUSB0", "/dev/ttyUSB2"])
    baud: int | None = None
    baud_candidates: list[int] = Field(default_factory=lambda: [9600, 19200, 38400, 57600, 115200])


class WeightCfg(BaseModel):
    enabled: bool = False
    driver: str = "ads1263"
    calibration_path: str = "./data/scale_calibration.json"
    simulate: bool = True
    # Path to cloned Waveshare python folder (contains ADS1263.py).
    waveshare_path: str = "/opt/High-Pricision_AD_HAT/python"
    # Frontend selection:
    # - adc2: passive parallel sniffing, recommended with factory terminal
    # - adc1: legacy direct ADS1263 path with external reference sense
    frontend: str = "adc2"
    # Reference source:
    # - internal: factory terminal powers the bridge, ADS1263 only listens
    # - avdd: ADS1263 board powers the bridge from AVDD/AVSS
    reference_mode: str = "internal"
    # Bridge reference differential inputs (E+ - E-) for adc1 legacy mode.
    ref_pos: int = 0
    ref_neg: int = 1
    # Bridge measurement differential inputs (SIG+ - SIG-).
    # Passive parallel default wiring: SIG+ -> IN0, SIG- -> IN1, E- -> AVSS/GND.
    channel_pos: int = 0
    channel_neg: int = 1
    sample_count: int = 80
    adc_rate: str = "ADS1263_20SPS"
    adc2_rate: str = "ADS1263_ADC2_100SPS"
    # Filtering: trim extremes before averaging ratio.
    trim_fraction: float = 0.2
    smoothing_alpha: float = 0.12
    fast_smoothing_alpha: float = 0.45
    fast_change_threshold_kg: float = 30.0
    zero_deadband_kg: float = 10.0
    median_window: int = 5
    invalid_below_kg: float | None = -1000.0
    invalid_above_kg: float | None = None
    # Avoid division by ~0 when bridge excitation is absent.
    min_ref_abs: float = 1e-9


class WifiCfg(BaseModel):
    enabled: bool = True
    hostapd_cli: str = "hostapd_cli"
    ap_interface: str = "wlan0"


class LteCfg(BaseModel):
    enabled: bool = True
    mmcli: str = "mmcli"
    at_ports: list[str] = Field(default_factory=lambda: ["/dev/ttyUSB0", "/dev/ttyUSB2"])
    at_baud: int = 115200
    events_port: str | None = None
    events_enabled: bool = True
    # Periodically poll SIM memory for unread SMS (robust fallback if +CMTI URC missing).
    sms_poll_interval_s: float = 30.0


class LoggingCfg(BaseModel):
    dir: str = "./logs"
    file: str = "host_monitor.log"
    level: Level = "INFO"
    max_bytes: int = 5_000_000
    backup_count: int = 3


class AppCfg(BaseModel):
    device: DeviceCfg
    send: SendCfg
    events: EventsCfg
    buffer: BufferCfg = Field(default_factory=BufferCfg)
    gps: GpsCfg = Field(default_factory=GpsCfg)
    weight: WeightCfg = Field(default_factory=WeightCfg)
    wifi: WifiCfg = Field(default_factory=WifiCfg)
    lte: LteCfg = Field(default_factory=LteCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("config must be a YAML mapping")
    return raw


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="./config.yaml", help="Path to config.yaml")
    return p.parse_args(argv)


def load_config(path: str) -> AppCfg:
    cfg_path = Path(path)
    data = _load_yaml(cfg_path)
    return AppCfg.model_validate(data)


def ensure_dirs(cfg: AppCfg) -> None:
    Path(cfg.logging.dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.buffer.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.weight.calibration_path).parent.mkdir(parents=True, exist_ok=True)

