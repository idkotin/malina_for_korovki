from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import serial


log = logging.getLogger("host_monitor.modem_events")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class ModemEventsCfg:
    enabled: bool
    port: str | None
    candidate_ports: list[str]
    baud: int


def _at_readline(ser: serial.Serial, deadline: float) -> str | None:
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        s = raw.decode("ascii", errors="ignore").strip()
        if s:
            return s
    return None


def _at_cmd(ser: serial.Serial, cmd: str, timeout_s: float = 1.5) -> list[str]:
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    lines: list[str] = []
    deadline = time.time() + timeout_s
    while True:
        line = _at_readline(ser, deadline)
        if line is None:
            break
        if line == "OK":
            break
        if line.startswith("ERROR"):
            lines.append(line)
            break
        lines.append(line)
    return lines


SMS_INDEX_RE = re.compile(r"^\+CMTI:.*?,\s*(\d+)\s*$")
CLIP_RE = re.compile(r"^\+CLIP:\s*\"([^\"]+)\"")


class ModemEventsReader:
    """
    Reads SMS/call events from a SIM7600-like modem AT port.

    Emits event dicts into an internal queue:
    - {"type":"sms","timestamp":...,"from":...,"text":...}
    - {"type":"call","timestamp":...,"from":...,"text":""}
    """

    def __init__(self, cfg: ModemEventsCfg):
        self._cfg = cfg
        self._q: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict = {"enabled": cfg.enabled, "running": False}
        self._last_err: str | None = None
        self._lte_snapshot: dict = {"rssi_dbm": None, "access_tech": None, "ts": None}

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="modem-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def drain(self, max_items: int = 100) -> list[dict]:
        items: list[dict] = []
        for _ in range(max_items):
            try:
                items.append(self._q.get_nowait())  # type: ignore[attr-defined]
            except Exception:
                break
        return items

    def status(self) -> dict:
        s = dict(self._status)
        s["last_error"] = self._last_err
        return s

    def lte_snapshot(self) -> dict:
        return dict(self._lte_snapshot)

    def _open_port(self) -> serial.Serial:
        ports = [self._cfg.port] if self._cfg.port else []
        ports += [p for p in self._cfg.candidate_ports if p not in ports]
        # If user didn't provide candidates, try all ttyUSB ports.
        if not ports:
            ports += [str(p) for p in Path("/dev").glob("ttyUSB*") if str(p) not in ports]
        last_exc = None
        for p in ports:
            try:
                ser = serial.Serial(p, baudrate=self._cfg.baud, timeout=1.0)
                # Basic probe
                _at_cmd(ser, "AT")
                return ser
            except Exception as e:
                last_exc = e
                continue
        raise RuntimeError(f"no modem events port available: {last_exc}")

    def _configure_modem(self, ser: serial.Serial) -> None:
        # Text mode SMS, deliver indications to TE, enable caller ID.
        _at_cmd(ser, "ATE0")
        _at_cmd(ser, "AT+CMGF=1")
        _at_cmd(ser, "AT+CSCS=\"GSM\"")
        _at_cmd(ser, "AT+CLIP=1")
        # CNMI=2,1,0,0,0 -> new SMS indication (+CMTI) and store in memory
        _at_cmd(ser, "AT+CNMI=2,1,0,0,0")

    def _read_sms_by_index(self, ser: serial.Serial, idx: int) -> dict | None:
        lines = _at_cmd(ser, f"AT+CMGR={idx}", timeout_s=3.0)
        # Typical:
        # +CMGR: "REC UNREAD","+7999...",,"26/03/18,12:34:56+12"
        # message text...
        header = None
        text_lines: list[str] = []
        for ln in lines:
            if ln.startswith("+CMGR:"):
                header = ln
            elif not ln.startswith("AT+"):
                text_lines.append(ln)
        if not header:
            return None
        m = re.search(r"^\+CMGR:\s*\"[^\"]+\",\"([^\"]*)\"", header)
        from_num = m.group(1) if m else ""
        text = "\n".join(text_lines).strip() if text_lines else ""
        # Best effort: delete after read to avoid memory filling up
        _at_cmd(ser, f"AT+CMGD={idx}", timeout_s=2.0)
        return {"type": "sms", "timestamp": utc_now_iso(), "from": from_num, "text": text}

    def _poll_lte_metrics(self, ser: serial.Serial) -> None:
        # AT+CSQ => RSSI in 0..31 or 99 unknown
        out = _at_cmd(ser, "AT+CSQ", timeout_s=1.0)
        rssi_dbm = None
        for ln in out:
            m = re.search(r"\+CSQ:\s*(\d+),", ln)
            if not m:
                continue
            csq = int(m.group(1))
            if 0 <= csq <= 31:
                rssi_dbm = -113 + (2 * csq)
            break

        # AT+COPS? gives access technology mode/operator
        tech = None
        out2 = _at_cmd(ser, "AT+COPS?", timeout_s=1.0)
        for ln in out2:
            if "+COPS:" in ln:
                tech = "LTE/auto"
                break

        self._lte_snapshot = {"rssi_dbm": rssi_dbm, "access_tech": tech, "ts": utc_now_iso()}

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._open_port() as ser:
                    self._configure_modem(ser)
                    self._status["running"] = True
                    self._last_err = None
                    backoff = 1.0
                    last_lte_poll = 0.0
                    while not self._stop.is_set():
                        now = time.time()
                        if now - last_lte_poll >= 30.0:
                            try:
                                self._poll_lte_metrics(ser)
                            except Exception as e:
                                self._last_err = str(e)
                            last_lte_poll = now

                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("ascii", errors="ignore").strip()
                        if not line:
                            continue

                        m = SMS_INDEX_RE.match(line)
                        if m:
                            try:
                                idx = int(m.group(1))
                                sms = self._read_sms_by_index(ser, idx)
                                if sms:
                                    self._q.put(sms)
                            except Exception as e:
                                self._last_err = str(e)
                                log.warning("sms read error: %s", e)
                            continue

                        if line == "RING":
                            self._q.put({"type": "call", "timestamp": utc_now_iso(), "from": "", "text": ""})
                            continue

                        m2 = CLIP_RE.match(line)
                        if m2:
                            self._q.put({"type": "call", "timestamp": utc_now_iso(), "from": m2.group(1), "text": ""})
                            continue
            except Exception as e:
                self._status["running"] = False
                self._last_err = str(e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


def event_to_json(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))

