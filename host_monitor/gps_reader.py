from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
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


def _parse_nmea_line(line: str) -> Position | None:
    # We keep it minimal: accept GGA or RMC.
    if not line.startswith("$"):
        return None
    parts = line.strip().split(",")
    if not parts:
        return None
    kind = parts[0]

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
            return Position(quality=quality, satellites=satellites)
        lat, lon = latlon
        return Position(lat=lat, lon=lon, quality=quality, satellites=satellites)

    if kind.endswith("RMC") and len(parts) >= 7:
        # $..RMC,time,status,lat,N,lon,E,...
        status = parts[2].upper() if parts[2] else ""
        latlon = _parse_lat_lon(parts[3], parts[4], parts[5], parts[6])
        if latlon is None:
            return Position()
        lat, lon = latlon
        ok = status == "A"
        return Position(lat=lat, lon=lon, quality=1 if ok else 0)

    return None


class GpsReader:
    def __init__(self, cfg: GpsCfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._latest: Position = Position()
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_line_ts = 0.0

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
        with self._lock:
            return self._latest

    def status(self) -> dict:
        age_s = time.time() - self._last_line_ts if self._last_line_ts else None
        return {"enabled": self._cfg.enabled, "age_s": age_s, "last_error": self._last_error}

    def _candidate_ports(self) -> list[str]:
        fixed = [self._cfg.port] if self._cfg.port else []
        dynamic = [str(p) for p in Path("/dev").glob("ttyUSB*")]
        merged: list[str] = []
        for p in fixed + self._cfg.port_candidates + dynamic:
            if p and p not in merged:
                merged.append(p)
        return merged

    def _open_serial(self) -> serial.Serial:
        timeout = 1.0
        bauds = [self._cfg.baud] if self._cfg.baud else self._cfg.baud_candidates
        last_err = None
        for port in self._candidate_ports():
            for b in bauds:
                try:
                    s = serial.Serial(port, b, timeout=timeout)
                    t0 = time.time()
                    found = False
                    while time.time() - t0 < 2.0:
                        raw = s.readline()
                        if not raw:
                            continue
                        line = raw.decode("ascii", errors="ignore")
                        if "$G" in line or "$N" in line:
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
        raise RuntimeError(f"failed to detect GPS port/baud: {last_err or 'no NMEA'}")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._open_serial() as ser:
                    backoff = 1.0
                    while not self._stop.is_set():
                        # Drain serial input and keep only the newest parsed position.
                        latest_pos: Position | None = None
                        got_any = False
                        deadline = time.time() + 0.2
                        while time.time() < deadline:
                            raw = ser.readline()
                            if not raw:
                                continue
                            got_any = True
                            self._last_line_ts = time.time()
                            line = raw.decode("ascii", errors="ignore").strip()
                            pos = _parse_nmea_line(line)
                            if pos is not None:
                                latest_pos = pos
                        if latest_pos is not None:
                            with self._lock:
                                current = self._latest
                                self._latest = Position(
                                    lat=latest_pos.lat if latest_pos.lat is not None else current.lat,
                                    lon=latest_pos.lon if latest_pos.lon is not None else current.lon,
                                    quality=latest_pos.quality if latest_pos.quality is not None else current.quality,
                                    satellites=latest_pos.satellites if latest_pos.satellites is not None else current.satellites,
                                )
                        elif not got_any:
                            time.sleep(0.02)
            except Exception as e:
                log.warning("GPS reader error: %s", e)
                self._last_error = str(e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

