from __future__ import annotations

import json
import logging

import httpx


log = logging.getLogger("host_monitor.sender")


def _sanitize_telemetry_payload(payload: dict) -> dict:
    """
    Server expects numeric fields as floats.
    Older buffered payloads may contain `null`, which the server rejects.
    We patch known keys to `0` / "0" before sending.
    """
    numeric_zero_float = 0.0
    numeric_zero_int = 0

    for k in ("lat", "lon", "weight", "cpu_temp_c"):
        if payload.get(k) is None:
            payload[k] = numeric_zero_float

    for k in ("gps_quality", "lte_rssi_dbm"):
        if payload.get(k) is None:
            payload[k] = numeric_zero_int

    if payload.get("lte_access_tech") is None:
        payload["lte_access_tech"] = "0"

    if payload.get("wifi_clients") is None:
        payload["wifi_clients"] = []

    return payload


class Sender:
    def __init__(self, url: str, timeout_s: float):
        self._url = url
        self._timeout = timeout_s
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def send_one(self, payload: dict) -> None:
        r = self._client.post(self._url, json=payload)
        r.raise_for_status()

    def send_json_string_one(self, payload_json: str) -> None:
        r = self._client.post(self._url, content=payload_json.encode("utf-8"), headers={"Content-Type": "application/json"})
        r.raise_for_status()

    def send_batch(self, payload_json_list: list[str]) -> None:
        # For now we send as individual POSTs. Easy to change later to a batch endpoint.
        for s in payload_json_list:
            payload = json.loads(s)
            payload = _sanitize_telemetry_payload(payload)
            self.send_one(payload)

    def send_json_string_batch(self, payload_json_list: list[str]) -> None:
        for s in payload_json_list:
            self.send_json_string_one(s)

