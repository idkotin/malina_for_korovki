from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Position(BaseModel):
    lat: float | None = None
    lon: float | None = None
    quality: int | None = None  # 0=no fix, 1=GPS fix, 2=DGPS...


class Weight(BaseModel):
    weight: float | None = None


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
    weight: float = 0.0
    gps_quality: int = 0
    wifi_clients: list[str] = Field(default_factory=list)
    cpu_temp_c: float = 0.0
    lte_rssi_dbm: int = 0
    # Numeric code: 0 = unknown/none, 1 = LTE
    lte_access_tech: float = 0.0


Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

