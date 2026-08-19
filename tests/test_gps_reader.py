from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from host_monitor.gps_reader import (
    GpsCfg,
    GpsReader,
    _GnssStreamParser,
    _parse_nmea_line,
    _parse_ubx_packet,
)
from host_monitor.models import Position


class FakeSerial:
    def __init__(self, queued: int):
        self.in_waiting = queued
        self.reset_calls = 0

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1


def make_nav_pvt_packet(
    *,
    lat: float = 52.4254,
    lon: float = 85.7300,
    speed_kmh: float = 7.2,
    satellites: int = 12,
    valid_fix: bool = True,
) -> bytes:
    payload = bytearray(92)
    payload[4:6] = (2026).to_bytes(2, "little")
    payload[6:11] = bytes((8, 19, 14, 7, 36))
    payload[11] = 0x07
    payload[20] = 3 if valid_fix else 0
    payload[21] = 0x01 if valid_fix else 0
    payload[23] = satellites
    payload[24:28] = int(round(lon * 1e7)).to_bytes(4, "little", signed=True)
    payload[28:32] = int(round(lat * 1e7)).to_bytes(4, "little", signed=True)
    payload[60:64] = int(round(speed_kmh / 3.6 * 1000)).to_bytes(4, "little", signed=True)
    header_and_payload = b"\x01\x07" + len(payload).to_bytes(2, "little") + payload
    ck_a = 0
    ck_b = 0
    for value in header_and_payload:
        ck_a = (ck_a + value) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xb5\x62" + header_and_payload + bytes((ck_a, ck_b))


def make_reader(*, max_fix_age_s: float = 3.0, validate_source_time: bool = True) -> GpsReader:
    return GpsReader(
        GpsCfg(
            enabled=True,
            port=None,
            port_candidates=[],
            baud=115200,
            baud_candidates=[115200],
            max_fix_age_s=max_fix_age_s,
            max_serial_backlog_bytes=4096,
            validate_source_time=validate_source_time,
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

    def test_rmc_after_ubx_binary_prefix_is_accepted(self) -> None:
        position = _parse_nmea_line(
            "\x00\xb5b\x01\x07garbage$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        )

        self.assertIsNotNone(position)
        assert position is not None
        self.assertAlmostEqual(position.lat or 0.0, 48.1173, places=4)
        self.assertAlmostEqual(position.speed_kmh or 0.0, 41.4848, places=4)

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


class UbxParserTests(unittest.TestCase):
    def test_valid_nav_pvt_is_accepted(self) -> None:
        position = _parse_ubx_packet(make_nav_pvt_packet())

        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(position.quality, 1)
        self.assertEqual(position.satellites, 12)
        self.assertAlmostEqual(position.lat or 0.0, 52.4254, places=6)
        self.assertAlmostEqual(position.lon or 0.0, 85.73, places=6)
        self.assertAlmostEqual(position.speed_kmh or 0.0, 7.2, places=3)

    def test_bad_ubx_checksum_is_rejected(self) -> None:
        packet = bytearray(make_nav_pvt_packet())
        packet[-1] ^= 0xFF

        self.assertIsNone(_parse_ubx_packet(bytes(packet)))

    def test_invalid_nav_pvt_does_not_publish_coordinates(self) -> None:
        position = _parse_ubx_packet(make_nav_pvt_packet(valid_fix=False))

        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(position.quality, 0)
        self.assertIsNone(position.lat)
        self.assertIsNone(position.lon)

    def test_fragmented_mixed_stream_returns_ubx_and_nmea(self) -> None:
        parser = _GnssStreamParser()
        ubx = make_nav_pvt_packet()
        nmea = b"$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\r\n"
        stream = b"garbage$noise" + ubx + nmea

        positions = []
        for offset in range(0, len(stream), 7):
            positions.extend(parser.feed(stream[offset : offset + 7]))

        self.assertEqual(len(positions), 2)
        self.assertAlmostEqual(positions[0].lat or 0.0, 52.4254, places=6)
        self.assertAlmostEqual(positions[1].lat or 0.0, 48.1173, places=4)


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

    def test_direct_uart_can_ignore_absolute_time_before_ntp_sync(self) -> None:
        reader = make_reader(max_fix_age_s=3.0, validate_source_time=False)
        reader._latest = Position(lat=55.1, lon=82.8, quality=1, speed_kmh=7.0)
        reader._last_fix_monotonic = time.monotonic() - 0.1
        reader._last_fix_source_utc_s = time.time() - 3600.0
        reader._last_speed_monotonic = time.monotonic() - 0.1

        position = reader.latest()

        self.assertEqual(position.quality, 1)
        self.assertEqual(position.speed_kmh, 7.0)
        self.assertLess(position.age_s or 99.0, 1.0)

    def test_fixed_port_does_not_fall_back_to_modem_candidates(self) -> None:
        reader = GpsReader(
            GpsCfg(
                enabled=True,
                port="/dev/serial0",
                port_candidates=["/dev/ttyUSB1"],
                baud=115200,
                baud_candidates=[9600],
            )
        )

        self.assertEqual(reader._candidate_ports(), ["/dev/serial0"])

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

    def test_excess_serial_backlog_is_discarded_and_fix_invalidated(self) -> None:
        reader = make_reader()
        reader._latest = Position(lat=55.1, lon=82.8, quality=1)
        reader._last_fix_monotonic = time.monotonic()
        serial_port = FakeSerial(queued=4097)

        discarded = reader._discard_excess_backlog(serial_port)  # type: ignore[arg-type]

        self.assertTrue(discarded)
        self.assertEqual(serial_port.reset_calls, 1)
        self.assertEqual(reader.latest().quality, 0)

    def test_small_serial_backlog_is_kept(self) -> None:
        reader = make_reader()
        serial_port = FakeSerial(queued=4096)

        discarded = reader._discard_excess_backlog(serial_port)  # type: ignore[arg-type]

        self.assertFalse(discarded)
        self.assertEqual(serial_port.reset_calls, 0)


if __name__ == "__main__":
    unittest.main()
