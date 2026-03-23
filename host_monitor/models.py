from __future__ import annotations

from typing import Any, Literal

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
    timestamp_utc: str
    seq: int

    position: Position = Field(default_factory=Position)
    weight: Weight = Field(default_factory=Weight)
    wifi_clients: list[str] = Field(default_factory=list)  # MAC list

    system: SystemInfo = Field(default_factory=SystemInfo)
    lte: LteInfo = Field(default_factory=LteInfo)

    status: dict[str, Any] = Field(default_factory=dict)  # module health, last errors


class BufferRow(BaseModel):
    id: int
    created_utc: str
    payload_json: str


Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

