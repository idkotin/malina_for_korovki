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


class BufferCfg(BaseModel):
    sqlite_path: str = "./data/buffer.sqlite3"
    max_rows: int = 200000


class GpsCfg(BaseModel):
    enabled: bool = True
    port: str = "/dev/ttyUSB1"
    baud: int | None = None
    baud_candidates: list[int] = Field(default_factory=lambda: [9600, 19200, 38400, 57600, 115200])


class WeightCfg(BaseModel):
    enabled: bool = False
    driver: str = "ads1263"
    calibration_path: str = "./data/scale_calibration.json"
    simulate: bool = True


class WifiCfg(BaseModel):
    enabled: bool = True
    hostapd_cli: str = "hostapd_cli"
    ap_interface: str = "wlan0"


class LteCfg(BaseModel):
    enabled: bool = True
    mmcli: str = "mmcli"
    at_ports: list[str] = Field(default_factory=lambda: ["/dev/ttyUSB0", "/dev/ttyUSB2"])
    at_baud: int = 115200


class LoggingCfg(BaseModel):
    dir: str = "./logs"
    file: str = "host_monitor.log"
    level: Level = "INFO"
    max_bytes: int = 5_000_000
    backup_count: int = 3


class AppCfg(BaseModel):
    device: DeviceCfg
    send: SendCfg
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

