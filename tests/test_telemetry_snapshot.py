from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from host_monitor.models import LteInfo, Position, Weight
from host_monitor.sender import _sanitize_telemetry_payload
from host_monitor.telemetry_builder import build_telemetry


def build_snapshot(*, position: Position, weight: Weight):
    return build_telemetry(
        device_id="Hozain_01",
        position=position,
        weight=weight,
        wifi_clients=[],
        cpu_temp_c=50.0,
        lte=LteInfo(access_tech="LTE", rssi_dbm=-70),
        gps_valid=True,
        weight_valid=weight.weight is not None,
        events_reader_ok=True,
    )


class TelemetrySnapshotTests(unittest.TestCase):
    def test_snapshot_uses_pi_clock_and_current_values(self) -> None:
        position = Position(
            lat=55.1,
            lon=82.8,
            quality=1,
            satellites=12,
            speed_kmh=8.5,
            age_s=0.2,
        )
        weight = Weight(weight=1234.5, raw=1235.0)
        with patch("host_monitor.telemetry_builder.utc_now_iso_no_tz", return_value="2026-07-17T10:20:30"):
            packet = build_snapshot(position=position, weight=weight)

        self.assertEqual(packet.timestamp, "2026-07-17T10:20:30")
        self.assertEqual(packet.lat, 55.1)
        self.assertEqual(packet.lon, 82.8)
        self.assertEqual(packet.speed_kmh, 8.5)
        self.assertEqual(packet.gps_age_s, 0.2)
        self.assertEqual(packet.weight, 1234.5)

    def test_invalid_gps_does_not_publish_old_coordinates(self) -> None:
        packet = build_telemetry(
            device_id="Hozain_01",
            position=Position(lat=55.1, lon=82.8, quality=0, speed_kmh=7.0, age_s=4.0),
            weight=Weight(weight=10.0, raw=10.2),
            wifi_clients=[],
            cpu_temp_c=50.0,
            lte=LteInfo(access_tech="LTE", rssi_dbm=-70),
            gps_valid=False,
            weight_valid=True,
            events_reader_ok=True,
        )

        self.assertFalse(packet.gps_valid)
        self.assertEqual(packet.lat, 0.0)
        self.assertEqual(packet.lon, 0.0)
        self.assertEqual(packet.speed_kmh, 0.0)
        self.assertEqual(packet.gps_age_s, 4.0)

    def test_missing_components_keep_packet(self) -> None:
        with patch("host_monitor.telemetry_builder.utc_now_iso_no_tz", return_value="2026-07-17T10:20:32"):
            packet = build_snapshot(
                position=Position(),
                weight=Weight(weight=None, raw=None),
            )

        self.assertEqual(packet.timestamp, "2026-07-17T10:20:32")
        self.assertFalse(packet.gps_valid)
        self.assertFalse(packet.weight_valid)

    def test_resend_preserves_snapshot_values_and_timestamp(self) -> None:
        packet = build_snapshot(
            position=Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0),
            weight=Weight(weight=10.0, raw=10.2),
        ).model_dump(mode="json")
        original = json.loads(json.dumps(packet))

        resent = _sanitize_telemetry_payload(packet)

        for key in ("timestamp", "lat", "lon", "speed_kmh", "weight", "raw"):
            self.assertEqual(resent[key], original[key])


if __name__ == "__main__":
    unittest.main()
