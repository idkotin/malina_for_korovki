from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from host_monitor.gps_reader import GpsCfg, GpsReader, _parse_nmea_line
from host_monitor.models import Position


def make_reader(*, max_fix_age_s: float = 3.0) -> GpsReader:
    return GpsReader(
        GpsCfg(
            enabled=True,
            port=None,
            port_candidates=[],
            baud=115200,
            baud_candidates=[115200],
            max_fix_age_s=max_fix_age_s,
        )
    )


class NmeaParserTests(unittest.TestCase):
    def test_valid_rmc_checksum_is_accepted(self) -> None:
        position = _parse_nmea_line(
            "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        )

        self.assertIsNotNone(position)
        assert position is not None
        self.assertAlmostEqual(position.lat or 0.0, 48.1173, places=4)
        self.assertAlmostEqual(position.lon or 0.0, 11.5166667, places=4)
        self.assertEqual(position.quality, 1)
        self.assertAlmostEqual(position.speed_kmh or 0.0, 41.4848, places=4)

    def test_bad_checksum_is_rejected(self) -> None:
        position = _parse_nmea_line(
            "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*00"
        )

        self.assertIsNone(position)

    def test_sentence_without_checksum_remains_supported(self) -> None:
        position = _parse_nmea_line(
            "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
        )

        self.assertIsNotNone(position)

    def test_gga_source_time_uses_nearest_utc_day(self) -> None:
        position = _parse_nmea_line(
            "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,",
            now_utc=datetime(2026, 7, 18, 12, 35, 20, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(
            position.source_utc_s,
            datetime(2026, 7, 18, 12, 35, 19, tzinfo=timezone.utc).timestamp(),
        )


class GpsFreshnessTests(unittest.TestCase):
    def test_fresh_fix_is_valid_and_carries_age(self) -> None:
        reader = make_reader()
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0)
        reader._last_fix_monotonic = time.monotonic() - 0.2
        reader._last_speed_monotonic = time.monotonic() - 0.2

        position = reader.latest()

        self.assertEqual(position.quality, 1)
        self.assertEqual(position.speed_kmh, 7.0)
        self.assertIsNotNone(position.age_s)
        self.assertLess(position.age_s or 99.0, 1.0)

    def test_old_fix_is_not_reported_as_valid(self) -> None:
        reader = make_reader(max_fix_age_s=3.0)
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0)
        reader._last_fix_monotonic = time.monotonic() - 4.0

        position = reader.latest()

        self.assertEqual(position.quality, 0)
        self.assertIsNone(position.speed_kmh)
        self.assertGreater(position.age_s or 0.0, 3.0)

    def test_recent_serial_line_with_old_nmea_epoch_is_not_valid(self) -> None:
        reader = make_reader(max_fix_age_s=3.0)
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0)
        reader._last_fix_monotonic = time.monotonic() - 0.1
        reader._last_fix_source_utc_s = time.time() - 3600.0
        reader._last_speed_monotonic = time.monotonic() - 0.1

        position = reader.latest()

        self.assertEqual(position.quality, 0)
        self.assertIsNone(position.speed_kmh)
        self.assertGreater(position.age_s or 0.0, 3500.0)

    def test_speed_expires_without_a_recent_rmc_sentence(self) -> None:
        reader = make_reader(max_fix_age_s=3.0)
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=137.048)
        reader._last_fix_monotonic = time.monotonic() - 0.1
        reader._last_fix_source_utc_s = time.time() - 0.1
        reader._last_speed_monotonic = time.monotonic() - 4.0

        position = reader.latest()

        self.assertEqual(position.quality, 1)
        self.assertIsNone(position.speed_kmh)

    def test_serial_failure_discards_previous_fix(self) -> None:
        reader = make_reader()
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0)
        reader._last_fix_monotonic = time.monotonic()

        reader._invalidate_fix()
        position = reader.latest()

        self.assertIsNone(position.lat)
        self.assertIsNone(position.lon)
        self.assertEqual(position.quality, 0)
        self.assertIsNone(position.age_s)


if __name__ == "__main__":
    unittest.main()
