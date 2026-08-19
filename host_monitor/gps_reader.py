from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import serial

from host_monitor.models import Position


log = logging.getLogger("host_monitor.gps")


@dataclass(frozen=True)
class GpsCfg:
    enabled: bool
    port: str | None
    port_candidates: list[str]
    baud: int | None
    baud_candidates: list[int]
    max_fix_age_s: float = 3.0
    max_serial_backlog_bytes: int = 4096
    validate_source_time: bool = True


def _parse_lat_lon(lat_str: str, lat_hemi: str, lon_str: str, lon_hemi: str) -> tuple[float, float] | None:
    # NMEA ddmm.mmmm (lat), dddmm.mmmm (lon)
    if not lat_str or not lon_str or not lat_hemi or not lon_hemi:
        return None
    try:
        lat_dd = float(lat_str[:2])
        lat_mm = float(lat_str[2:])
        lon_dd = float(lon_str[:3])
        lon_mm = float(lon_str[3:])
        lat = lat_dd + (lat_mm / 60.0)
        lon = lon_dd + (lon_mm / 60.0)
        if lat_hemi.upper() == "S":
            lat = -lat
        if lon_hemi.upper() == "W":
            lon = -lon
        return lat, lon
    except Exception:
        return None


def _parse_float(value: str) -> float | None:
    try:
        return float(value) if value else None
    except Exception:
        return None


def _parse_nmea_utc_s(kind: str, parts: list[str], now_utc: datetime | None = None) -> float | None:
    if len(parts) < 2 or not parts[1]:
        return None
    value = parts[1]
    if len(value) < 6:
        return None
    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        second_value = float(value[4:])
        second = int(second_value)
        microsecond = int(round((second_value - second) * 1_000_000))
        if microsecond >= 1_000_000:
            second += 1
            microsecond -= 1_000_000
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
    except (TypeError, ValueError):
        return None

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if kind.endswith("RMC") and len(parts) > 9 and parts[9]:
        date_value = parts[9]
        if len(date_value) != 6:
            return None
        try:
            day = int(date_value[0:2])
            month = int(date_value[2:4])
            year_2d = int(date_value[4:6])
            year = 2000 + year_2d if year_2d < 80 else 1900 + year_2d
            return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None

    # GGA/GNS carry time-of-day but no date. Select the nearest UTC day so
    # midnight rollover cannot turn a fresh fix into a 24-hour-old one.
    base = datetime(now.year, now.month, now.day, hour, minute, second, microsecond, tzinfo=timezone.utc)
    candidates = (base - timedelta(days=1), base, base + timedelta(days=1))
    return min(candidates, key=lambda candidate: abs((candidate - now).total_seconds())).timestamp()


def _nmea_body_if_checksum_valid(line: str) -> str | None:
    line = line.strip()
    # Some u-blox configurations emit UBX binary and NMEA on the same UART.
    # A UBX packet can then precede a valid NMEA sentence before the next LF.
    # Keep the NMEA suffix instead of dropping the whole serial line.
    nmea_start = line.find("$")
    if nmea_start < 0:
        return None
    line = line[nmea_start:]
    if "*" not in line:
        # Some modem firmware omits checksums. Keep compatibility, but verify
        # every sentence that does provide one.
        return line
    body, checksum_text = line.rsplit("*", 1)
    if len(checksum_text) < 2:
        return None
    try:
        expected = int(checksum_text[:2], 16)
    except ValueError:
        return None
    actual = 0
    for char in body[1:]:
        actual ^= ord(char)
    return body if actual == expected else None


def _parse_nmea_line(line: str, now_utc: datetime | None = None) -> Position | None:
    # We keep it minimal: accept GGA, RMC or GNS.
    body = _nmea_body_if_checksum_valid(line)
    if body is None:
        return None
    parts = body.split(",")
    if not parts:
        return None
    kind = parts[0]
    source_utc_s = _parse_nmea_utc_s(kind, parts, now_utc)

    if kind.endswith("GGA") and len(parts) >= 8:
        # $..GGA,time,lat,N,lon,E,quality,num_sats,...
        latlon = _parse_lat_lon(parts[2], parts[3], parts[4], parts[5])
        quality = None
        satellites = None
        try:
            quality = int(parts[6]) if parts[6] else 0
        except Exception:
            quality = None
        try:
            satellites = int(parts[7]) if parts[7] else 0
        except Exception:
            satellites = None
        if latlon is None:
            return Position(quality=quality, satellites=satellites, source_utc_s=source_utc_s)
        lat, lon = latlon
        return Position(lat=lat, lon=lon, quality=quality, satellites=satellites, source_utc_s=source_utc_s)

    if kind.endswith("RMC") and len(parts) >= 7:
        # $..RMC,time,status,lat,N,lon,E,speed_knots,...
        status = parts[2].upper() if parts[2] else ""
        ok = status == "A"
        latlon = _parse_lat_lon(parts[3], parts[4], parts[5], parts[6])
        speed_knots = _parse_float(parts[7]) if len(parts) >= 8 else None
        speed_kmh = speed_knots * 1.852 if speed_knots is not None and ok else None
        if latlon is None:
            return Position(quality=1 if ok else 0, speed_kmh=speed_kmh, source_utc_s=source_utc_s)
        lat, lon = latlon
        return Position(lat=lat, lon=lon, quality=1 if ok else 0, speed_kmh=speed_kmh, source_utc_s=source_utc_s)

    if kind.endswith("GNS") and len(parts) >= 8:
        # $..GNS,time,lat,N,lon,E,mode,num_sats,...
        latlon = _parse_lat_lon(parts[2], parts[3], parts[4], parts[5])
        mode = parts[6].upper() if parts[6] else ""
        satellites = None
        try:
            satellites = int(parts[7]) if parts[7] else 0
        except Exception:
            satellites = None
        quality = 1 if any(ch in mode for ch in ("A", "D", "F", "R")) else 0
        if latlon is None:
            return Position(quality=quality, satellites=satellites, source_utc_s=source_utc_s)
        lat, lon = latlon
        return Position(lat=lat, lon=lon, quality=quality, satellites=satellites, source_utc_s=source_utc_s)

    return None


def _ubx_checksum_valid(packet: bytes) -> bool:
    if len(packet) < 8 or packet[:2] != b"\xb5\x62":
        return False
    payload_len = int.from_bytes(packet[4:6], "little")
    if len(packet) != payload_len + 8:
        return False
    ck_a = 0
    ck_b = 0
    for value in packet[2:-2]:
        ck_a = (ck_a + value) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return packet[-2:] == bytes((ck_a, ck_b))


def _parse_ubx_packet(packet: bytes) -> Position | None:
    """Parse a checksum-verified UBX-NAV-PVT navigation solution."""
    if not _ubx_checksum_valid(packet) or packet[2:4] != b"\x01\x07":
        return None
    payload = packet[6:-2]
    if len(payload) != 92:
        return None

    fix_type = payload[20]
    flags = payload[21]
    num_sv = payload[23]
    flags3 = int.from_bytes(payload[78:80], "little")
    fix_ok = bool(flags & 0x01) and fix_type >= 2 and not bool(flags3 & 0x01)

    source_utc_s: float | None = None
    valid = payload[11]
    if valid & 0x03 == 0x03:
        try:
            year = int.from_bytes(payload[4:6], "little")
            month, day, hour, minute, second = payload[6:11]
            nano = struct.unpack_from("<i", payload, 16)[0]
            # datetime does not accept the leap-second value 60. The source
            # timestamp is supplemental, so omit it for that one epoch.
            if second <= 59:
                base = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                source_utc_s = base.timestamp() + (nano / 1_000_000_000.0)
        except (OverflowError, ValueError):
            source_utc_s = None

    if not fix_ok:
        return Position(quality=0, satellites=num_sv, source_utc_s=source_utc_s)

    lon_raw, lat_raw = struct.unpack_from("<ii", payload, 24)
    lon = lon_raw * 1e-7
    lat = lat_raw * 1e-7
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return Position(quality=0, satellites=num_sv, source_utc_s=source_utc_s)
    ground_speed_mm_s = struct.unpack_from("<i", payload, 60)[0]
    speed_kmh = max(0.0, ground_speed_mm_s / 1000.0 * 3.6)
    return Position(
        lat=lat,
        lon=lon,
        quality=1,
        satellites=num_sv,
        speed_kmh=speed_kmh,
        source_utc_s=source_utc_s,
    )


class _GnssStreamParser:
    """Frame interleaved UBX packets and NMEA lines from one UART stream."""

    _UBX_SYNC = b"\xb5\x62"
    _MAX_UBX_PAYLOAD = 4096
    _MAX_NMEA_LINE = 1024
    _MAX_BUFFER = 131072

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Position]:
        if data:
            self._buffer.extend(data)
        if len(self._buffer) > self._MAX_BUFFER:
            del self._buffer[: len(self._buffer) - self._MAX_BUFFER]

        positions: list[Position] = []
        while self._buffer:
            ubx_at = self._buffer.find(self._UBX_SYNC)
            nmea_at = self._buffer.find(b"$")
            starts = [index for index in (ubx_at, nmea_at) if index >= 0]
            if not starts:
                # Preserve a possible first sync byte across chunk boundaries.
                tail = self._buffer[-1:] if self._buffer[-1:] == b"\xb5" else b""
                self._buffer.clear()
                self._buffer.extend(tail)
                break

            start = min(starts)
            if start:
                del self._buffer[:start]

            if self._buffer.startswith(self._UBX_SYNC):
                if len(self._buffer) < 6:
                    break
                payload_len = int.from_bytes(self._buffer[4:6], "little")
                if payload_len > self._MAX_UBX_PAYLOAD:
                    del self._buffer[0]
                    continue
                packet_len = payload_len + 8
                if len(self._buffer) < packet_len:
                    break
                packet = bytes(self._buffer[:packet_len])
                del self._buffer[:packet_len]
                position = _parse_ubx_packet(packet)
                if position is not None:
                    positions.append(position)
                continue

            newline_at = self._buffer.find(b"\n")
            next_ubx = self._buffer.find(self._UBX_SYNC, 1)
            next_nmea = self._buffer.find(b"$", 1)
            next_starts = [index for index in (next_ubx, next_nmea) if index >= 0]
            if next_starts and (newline_at < 0 or min(next_starts) < newline_at):
                # The current '$' was binary payload noise, not a sentence.
                del self._buffer[: min(next_starts)]
                continue
            if newline_at < 0:
                if len(self._buffer) > self._MAX_NMEA_LINE:
                    del self._buffer[0]
                    continue
                break
            raw_line = bytes(self._buffer[: newline_at + 1])
            del self._buffer[: newline_at + 1]
            position = _parse_nmea_line(raw_line.decode("ascii", errors="ignore"))
            if position is not None:
                positions.append(position)
        return positions


def _merge_position(base: Position, update: Position) -> Position:
    return Position(
        lat=update.lat if update.lat is not None else base.lat,
        lon=update.lon if update.lon is not None else base.lon,
        quality=update.quality if update.quality is not None else base.quality,
        satellites=update.satellites if update.satellites is not None else base.satellites,
        speed_kmh=update.speed_kmh if update.speed_kmh is not None else base.speed_kmh,
        source_utc_s=update.source_utc_s if update.source_utc_s is not None else base.source_utc_s,
    )


class GpsReader:
    def __init__(self, cfg: GpsCfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._latest: Position = Position()
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_line_monotonic: float | None = None
        self._last_fix_monotonic: float | None = None
        self._last_fix_source_utc_s: float | None = None
        self._last_speed_monotonic: float | None = None
        self._last_stale_source_log_monotonic = 0.0
        self._last_backlog_log_monotonic = 0.0

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="gps-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def latest(self) -> Position:
        now = time.monotonic()
        wall_now = time.time()
        with self._lock:
            snapshot = self._latest.model_copy(deep=True)
            arrival_age_s = None if self._last_fix_monotonic is None else max(0.0, now - self._last_fix_monotonic)
            source_utc_s = self._last_fix_source_utc_s
        source_age_s = (
            None
            if source_utc_s is None or not self._cfg.validate_source_time
            else abs(wall_now - source_utc_s)
        )
        ages = [age for age in (arrival_age_s, source_age_s) if age is not None]
        age_s = max(ages) if ages else None
        snapshot.age_s = age_s
        if age_s is None or age_s > max(0.1, float(self._cfg.max_fix_age_s)):
            snapshot.quality = 0
            snapshot.speed_kmh = None
        with self._lock:
            speed_age_s = None if self._last_speed_monotonic is None else max(0.0, now - self._last_speed_monotonic)
        if speed_age_s is None or speed_age_s > max(0.1, float(self._cfg.max_fix_age_s)):
            snapshot.speed_kmh = None
        return snapshot

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            line_age_s = None if self._last_line_monotonic is None else max(0.0, now - self._last_line_monotonic)
            fix_age_s = None if self._last_fix_monotonic is None else max(0.0, now - self._last_fix_monotonic)
            source_utc_s = self._last_fix_source_utc_s
        source_age_s = None if source_utc_s is None else abs(time.time() - source_utc_s)
        return {
            "enabled": self._cfg.enabled,
            "age_s": line_age_s,
            "fix_age_s": fix_age_s,
            "source_age_s": source_age_s,
            "last_error": self._last_error,
        }

    def _invalidate_fix(self) -> None:
        with self._lock:
            self._latest = Position()
            self._last_fix_monotonic = None
            self._last_fix_source_utc_s = None
            self._last_speed_monotonic = None

    def _discard_excess_backlog(self, ser: serial.Serial) -> bool:
        """Drop queued UART data if it can no longer be considered live."""
        limit = max(0, int(self._cfg.max_serial_backlog_bytes))
        if limit == 0:
            return False
        queued = int(ser.in_waiting)
        if queued <= limit:
            return False

        ser.reset_input_buffer()
        self._invalidate_fix()
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_backlog_log_monotonic >= 10.0:
            log.warning(
                "GPS serial backlog discarded: queued_bytes=%s limit_bytes=%s",
                queued,
                limit,
            )
            self._last_backlog_log_monotonic = now_monotonic
        return True

    def _candidate_ports(self) -> list[str]:
        # A configured port is an explicit hardware binding. Do not silently
        # fall back to an unrelated USB modem/GNSS device if it disappears.
        if self._cfg.port:
            return [self._cfg.port]

        dynamic = [str(p) for p in Path("/dev").glob("ttyUSB*")]
        merged: list[str] = []
        for p in self._cfg.port_candidates + dynamic:
            if p and p not in merged:
                merged.append(p)
        return merged

    def _open_serial(self) -> serial.Serial:
        timeout = 0.2
        bauds = [self._cfg.baud] if self._cfg.baud else self._cfg.baud_candidates
        last_err = None
        for port in self._candidate_ports():
            for b in bauds:
                try:
                    s = serial.Serial(port, b, timeout=timeout)
                    s.reset_input_buffer()
                    parser = _GnssStreamParser()
                    t0 = time.monotonic()
                    found = False
                    while time.monotonic() - t0 < 2.0:
                        raw = s.read(s.in_waiting or 1)
                        if not raw:
                            continue
                        if parser.feed(raw):
                            found = True
                            break
                    if found:
                        log.info("GPS source detected: port=%s baud=%s", port, b)
                        self._last_error = None
                        return s
                    s.close()
                except Exception as e:
                    last_err = str(e)
                    continue
        raise RuntimeError(f"failed to detect GPS port/baud: {last_err or 'no supported NMEA/UBX navigation data'}")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._open_serial() as ser:
                    backoff = 1.0
                    parser = _GnssStreamParser()
                    while not self._stop.is_set():
                        if self._discard_excess_backlog(ser):
                            # A live 10 Hz receiver will provide a replacement
                            # fix almost immediately after the buffer reset.
                            continue
                        # Drain serial input and keep only the newest parsed position.
                        latest_pos: Position | None = None
                        got_any = False
                        deadline = time.monotonic() + 0.2
                        latest_fix_monotonic: float | None = None
                        latest_fix_source_utc_s: float | None = None
                        latest_speed_monotonic: float | None = None
                        while time.monotonic() < deadline:
                            raw = ser.read(ser.in_waiting or 1)
                            if not raw:
                                continue
                            got_any = True
                            line_monotonic = time.monotonic()
                            with self._lock:
                                self._last_line_monotonic = line_monotonic
                            for pos in parser.feed(raw):
                                # Keep the freshest coordinates from the latest sentence in this read window,
                                # but preserve supplemental fields when a newer NMEA or UBX message omits them.
                                latest_pos = pos if latest_pos is None else _merge_position(latest_pos, pos)
                                if pos.speed_kmh is not None:
                                    latest_speed_monotonic = line_monotonic
                                if pos.lat is not None and pos.lon is not None and (pos.quality or 0) > 0:
                                    latest_fix_monotonic = line_monotonic
                                    latest_fix_source_utc_s = pos.source_utc_s
                        if latest_pos is not None:
                            source_age_s = (
                                None
                                if latest_fix_source_utc_s is None or not self._cfg.validate_source_time
                                else abs(time.time() - latest_fix_source_utc_s)
                            )
                            if source_age_s is not None and source_age_s > max(0.1, float(self._cfg.max_fix_age_s)):
                                # The tty line is arriving now, but its NMEA epoch is old: this is
                                # a serial/modem backlog, not a current fix. Drop the kernel buffer
                                # so the next read starts at the live edge.
                                ser.reset_input_buffer()
                                self._invalidate_fix()
                                now_monotonic = time.monotonic()
                                if now_monotonic - self._last_stale_source_log_monotonic >= 10.0:
                                    log.warning("stale NMEA source time discarded: age_s=%.3f", source_age_s)
                                    self._last_stale_source_log_monotonic = now_monotonic
                                continue
                            with self._lock:
                                self._latest = _merge_position(self._latest, latest_pos)
                                if latest_fix_monotonic is not None:
                                    self._last_fix_monotonic = latest_fix_monotonic
                                    self._last_fix_source_utc_s = latest_fix_source_utc_s
                                if latest_speed_monotonic is not None:
                                    self._last_speed_monotonic = latest_speed_monotonic
                        elif not got_any:
                            time.sleep(0.02)
            except Exception as e:
                log.warning("GPS reader error: %s", e)
                self._last_error = str(e)
                self._invalidate_fix()
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

