from __future__ import annotations

from datetime import datetime, timezone

from host_monitor.models import LteInfo, Position, SystemInfo, Telemetry, Weight


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_telemetry(
    *,
    device_id: str,
    seq: int,
    position: Position,
    weight: Weight,
    wifi_clients: list[str],
    cpu_temp_c: float | None,
    lte: LteInfo,
    module_status: dict,
) -> Telemetry:
    return Telemetry(
        device_id=device_id,
        timestamp_utc=utc_now_iso(),
        seq=seq,
        position=position,
        weight=weight,
        wifi_clients=wifi_clients,
        system=SystemInfo(cpu_temp_c=cpu_temp_c),
        lte=lte,
        status=module_status,
    )

