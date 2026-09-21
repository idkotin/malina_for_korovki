from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from host_monitor.models import Level


class DeviceCfg(BaseModel):
    id: str


class SendCfg(BaseModel):
    url: str
    interval_s: float = 2.0
    timeout_s: float = 2.0
    max_batch: int = 20
    idle_sleep_enabled: bool = False
    idle_after_s: float = 1800.0
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
    max_fix_age_s: float = 3.0
    # If the UART reader ever falls behind, discard queued bytes instead of
    # timestamping old NMEA sentences as newly arrived fixes. 4096 bytes are
    # roughly 0.36 seconds at 115200 baud.
    max_serial_backlog_bytes: int = 4096
    # Absolute NMEA UTC validation protects modem-backed serial ports from
    # queued old data. Disable it for a direct UART GNSS receiver so a cold Pi
    # without NTP/RTC can still acquire a valid position.
    validate_source_time: bool = True


class WeightCfg(BaseModel):
    enabled: bool = False
    require_calibration: bool = False
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
    scan_interval_s: float = 1.0
    max_snapshot_age_s: float = 2.0


class LteCfg(BaseModel):
    enabled: bool = True
    mmcli: str = "mmcli"
    at_ports: list[str] = Field(default_factory=lambda: ["/dev/ttyUSB0", "/dev/ttyUSB2"])
    at_baud: int = 115200
    events_port: str | None = None
    events_enabled: bool = True
    # Periodically poll SIM memory for unread SMS (robust fallback if +CMTI URC missing).
    sms_poll_interval_s: float = 30.0
    # Recover only a sustained, explicit `AT+CPIN? -> SIM failure` by resetting
    # the SIM7600 itself. Generic internet/operator failures never trigger it.
    sim_failure_recovery_enabled: bool = False
    sim_failure_poll_interval_s: float = 30.0
    sim_failure_confirm_s: float = 90.0
    sim_failure_reset_cooldown_s: float = 1800.0
    sim_failure_reset_window_s: float = 21600.0
    sim_failure_max_resets: int = 3
    sim_failure_reset_settle_s: float = 20.0


class SmsRebootCfg(BaseModel):
    """Manual reboot command received through the modem SMS reader."""

    enabled: bool = False
    allowed_number: str | None = None
    command: str = "/reboot"


class AutoRebootCfg(BaseModel):
    """Guarded recovery for a sustained loss of acknowledged telemetry."""

    enabled: bool = False
    telemetry_inactive_s: float = 900.0
    terminal_off_below_raw_kg: float = -1000.0
    terminal_off_confirm_s: float = 30.0
    max_weight_age_s: float = 10.0
    healthy_success_max_age_s: float = 10.0
    healthy_reset_confirm_s: float = 60.0
    state_path: str = "./data/auto_reboot_state.json"


class LoggingCfg(BaseModel):
    dir: str = "./logs"
    file: str = "host_monitor.log"
    level: Level = "INFO"
    max_bytes: int = 5_000_000
    backup_count: int = 3


class LocalScaleCfg(BaseModel):
    enabled: bool = False
    listen: str = '127.0.0.1'
    port: int = Field(default=8765, ge=0, le=65535)


class PanelCfg(BaseModel):
    enabled: bool = False
    state_path: str = './data/panel.json'
    data_pin: int = 5
    clock_pin: int = 6
    latch_pin: int = 13
    blank_pin: int = 19
    minus_pin: int = 20
    plus_pin: int = 21
    power_pin: int = 16
    net_pin: int = 26
    admin_pins: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_hardware(self):
        pins = [getattr(self, name) for name in ('data_pin', 'clock_pin', 'latch_pin', 'blank_pin',
                                                'minus_pin', 'plus_pin', 'power_pin', 'net_pin')]
        if len(set(pins)) != len(pins) or any(p not in range(2, 28) or p in (7, 8, 9, 10, 11, 14, 15, 17, 18, 22) for p in pins):
            raise ValueError('Panel GPIO conflicts with ADC/UART or is duplicated')
        if self.enabled and (set(self.admin_pins) != {'zero', 'span'} or
            len(set(self.admin_pins.values())) != 2 or any(len(p) != 4 or not p.isascii() or not p.isdigit() for p in self.admin_pins.values())):
            raise ValueError('Configure distinct four-digit zero/span PINs in the live config')
        return self


class AppCfg(BaseModel):
    device: DeviceCfg
    send: SendCfg
    events: EventsCfg
    buffer: BufferCfg = Field(default_factory=BufferCfg)
    gps: GpsCfg = Field(default_factory=GpsCfg)
    weight: WeightCfg = Field(default_factory=WeightCfg)
    wifi: WifiCfg = Field(default_factory=WifiCfg)
    lte: LteCfg = Field(default_factory=LteCfg)
    sms_reboot: SmsRebootCfg = Field(default_factory=SmsRebootCfg)
    auto_reboot: AutoRebootCfg = Field(default_factory=AutoRebootCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)
    local_scale: LocalScaleCfg = Field(default_factory=LocalScaleCfg)
    panel: PanelCfg = Field(default_factory=PanelCfg)

    @model_validator(mode='after')
    def standalone_recovery(self):
        if self.weight.require_calibration and self.auto_reboot.enabled:
            raise ValueError('Terminal-off reboot guard is incompatible with standalone scales')
        return self


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
    Path(cfg.auto_reboot.state_path).parent.mkdir(parents=True, exist_ok=True)

