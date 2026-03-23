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
    return Telemetry(
        device_id=device_id,
        timestamp=utc_now_iso_no_tz(),
        lat=position.lat,
        lon=position.lon,
        weight=weight.weight,
        gps_quality=position.quality,
        wifi_clients=wifi_clients,
        cpu_temp_c=cpu_temp_c,
        lte_rssi_dbm=lte.rssi_dbm,
        lte_access_tech=lte.access_tech,
    )

