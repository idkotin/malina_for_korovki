from __future__ import annotations

from datetime import datetime, timezone

from host_monitor.models import LteInfo, Position, Telemetry, Weight


def utc_now_iso_no_tz() -> str:
    # UTC timestamp without timezone suffix (as requested by backend format).
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def build_telemetry(
    *,
    device_id: str,
    position: Position,
    weight: Weight,
    wifi_clients: list[str],
    cpu_temp_c: float | None,
    lte: LteInfo,
    gps_valid: bool,
    weight_valid: bool,
    events_reader_ok: bool,
) -> Telemetry:
    lat = position.lat if position.lat is not None else 0.0
    lon = position.lon if position.lon is not None else 0.0
    gps_quality = position.quality if position.quality is not None else 0
    gps_satellites = position.satellites if position.satellites is not None else 0
    weight_kg = weight.weight if weight.weight is not None else 0.0

    lte_rssi = lte.rssi_dbm if lte.rssi_dbm is not None else 0
    lte_access_tech = lte.access_tech if lte.access_tech is not None else "0"

    return Telemetry(
        device_id=device_id,
        timestamp=utc_now_iso_no_tz(),
        lat=lat,
        lon=lon,
        gps_valid=gps_valid,
        gps_satellites=gps_satellites,
        weight=weight_kg,
        weight_valid=weight_valid,
        gps_quality=gps_quality,
        wifi_clients=wifi_clients,
        cpu_temp_c=cpu_temp_c if cpu_temp_c is not None else 0.0,
        lte_rssi_dbm=lte_rssi,
        lte_access_tech=lte_access_tech,
        events_reader_ok=events_reader_ok,
    )

