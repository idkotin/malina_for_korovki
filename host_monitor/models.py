from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Position(BaseModel):
    lat: float | None = None
    lon: float | None = None
    quality: int | None = None  # 0=no fix, 1=GPS fix, 2=DGPS...
    satellites: int | None = None
    speed_kmh: float | None = None


class Weight(BaseModel):
    weight: float | None = None
    raw: float | None = None


class SystemInfo(BaseModel):
    cpu_temp_c: float | None = None


class LteInfo(BaseModel):
    access_tech: str | None = None  # LTE/UMTS/...
    rssi_dbm: int | None = None


class Telemetry(BaseModel):
    device_id: str
    timestamp: str
    lat: float = 0.0
    lon: float = 0.0
    gps_valid: bool = False
    gps_satellites: int = 0
    speed_kmh: float = 0.0
    weight: float = 0.0
    raw: float = 0.0
    weight_valid: bool = False
    gps_quality: int = 0
    wifi_clients: list[str] = Field(default_factory=list)
    cpu_temp_c: float = 0.0
    lte_rssi_dbm: int = 0
    # Keep as string contract (e.g. "LTE/auto"), but avoid null.
    lte_access_tech: str = "0"
    events_reader_ok: bool = False


Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

