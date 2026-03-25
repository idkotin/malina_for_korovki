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
) -> Telemetry:
    lat = position.lat if position.lat is not None else 0.0
    lon = position.lon if position.lon is not None else 0.0
    gps_quality = position.quality if position.quality is not None else 0
    weight_kg = weight.weight if weight.weight is not None else 0.0

    lte_rssi = lte.rssi_dbm if lte.rssi_dbm is not None else 0
    access_code = 0.0
    if lte.access_tech:
        # Backend expects numbers; encode LTE as 1, everything else as 0.
        access_code = 1.0 if "LTE" in lte.access_tech.upper() else 0.0

    return Telemetry(
        device_id=device_id,
        timestamp=utc_now_iso_no_tz(),
        lat=lat,
        lon=lon,
        weight=weight_kg,
        gps_quality=gps_quality,
        wifi_clients=wifi_clients,
        cpu_temp_c=cpu_temp_c if cpu_temp_c is not None else 0.0,
        lte_rssi_dbm=lte_rssi,
        lte_access_tech=access_code,
    )

