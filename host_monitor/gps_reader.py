from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import serial

from host_monitor.models import Position


log = logging.getLogger("host_monitor.gps")


@dataclass(frozen=True)
class GpsCfg:
    enabled: bool
    port: str
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

    if kind.endswith("GGA") and len(parts) >= 7:
        # $..GGA,time,lat,N,lon,E,quality,...
        latlon = _parse_lat_lon(parts[2], parts[3], parts[4], parts[5])
        quality = None
        try:
            quality = int(parts[6]) if parts[6] else 0
        except Exception:
            quality = None
        if latlon is None:
            return Position(ok=False, quality=quality, error="no_latlon")
        lat, lon = latlon
        return Position(lat=lat, lon=lon, quality=quality, ok=bool(quality and quality > 0))

    if kind.endswith("RMC") and len(parts) >= 7:
        # $..RMC,time,status,lat,N,lon,E,...
        status = parts[2].upper() if parts[2] else ""
        latlon = _parse_lat_lon(parts[3], parts[4], parts[5], parts[6])
        if latlon is None:
            return Position(ok=False, error="no_latlon")
        lat, lon = latlon
        ok = status == "A"
        return Position(lat=lat, lon=lon, quality=1 if ok else 0, ok=ok)

    return None


class GpsReader:
    def __init__(self, cfg: GpsCfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._latest: Position = Position(ok=False, error="not_started")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_line_ts = 0.0

    def start(self) -> None:
        if not self._cfg.enabled:
            with self._lock:
                self._latest = Position(ok=False, error="disabled")
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
        return {"enabled": self._cfg.enabled, "port": self._cfg.port, "age_s": age_s}

    def _open_serial(self) -> serial.Serial:
        timeout = 1.0
        if self._cfg.baud:
            return serial.Serial(self._cfg.port, self._cfg.baud, timeout=timeout)

        # Auto-detect: try candidates until we see a valid NMEA sentence quickly.
        for b in self._cfg.baud_candidates:
            try:
                s = serial.Serial(self._cfg.port, b, timeout=timeout)
                t0 = time.time()
                ok = False
                while time.time() - t0 < 2.0:
                    raw = s.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("ascii", errors="ignore")
                    except Exception:
                        continue
                    if "$G" in line or "$N" in line:
                        ok = True
                        break
                if ok:
                    log.info("GPS baud detected: %s", b)
                    return s
                s.close()
            except Exception:
                continue
        raise RuntimeError("failed to auto-detect GPS baud; set gps.baud in config")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._open_serial() as ser:
                    backoff = 1.0
                    with self._lock:
                        self._latest = Position(ok=False, error="waiting_fix")
                    while not self._stop.is_set():
                        raw = ser.readline()
                        if not raw:
                            continue
                        self._last_line_ts = time.time()
                        line = raw.decode("ascii", errors="ignore").strip()
                        pos = _parse_nmea_line(line)
                        if pos is None:
                            continue
                        with self._lock:
                            self._latest = pos
            except Exception as e:
                log.warning("GPS reader error: %s", e)
                with self._lock:
                    self._latest = Position(ok=False, error=str(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

