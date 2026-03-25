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
    sms_poll_interval_s: float = 30.0


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


def decode_maybe_ucs2(text: str) -> str:
    """
    Some operators send UCS2/UTF-16-BE as HEX string.
    If we detect hex-looking content, try to decode as utf-16-be.
    Otherwise return the input as-is.
    """
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1].strip()

    # Some firmwares insert whitespace into UCS2 hex.
    t_compact = "".join(ch for ch in t if not ch.isspace())
    if len(t_compact) >= 4 and (len(t_compact) % 2 == 0) and HEX_RE.match(t_compact):
        try:
            b = bytes.fromhex(t_compact)
            decoded = b.decode("utf-16-be", errors="strict")
            return decoded.replace("\x00", "").strip()
        except Exception:
            return text
    return text


SMS_INDEX_RE = re.compile(r"^\+CMTI:.*?,\s*(\d+)\s*$")
# Example variants:
#   +CLIP: "+7999...",4,0,0,"",0
#   +CLIP: +7999...,4,0,0,"",0
CLIP_RE = re.compile(r'^\+CLIP:\s*\"?([^\",]+)')
HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


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
        self._pending_call_ts: float | None = None

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
                log.info("AT port selected: %s baud=%s", p, self._cfg.baud)
                return ser
            except Exception as e:
                last_exc = e
                continue
        raise RuntimeError(f"no modem events port available: {last_exc}")

    def _configure_modem(self, ser: serial.Serial) -> None:
        # Text mode SMS, set storage, enable caller ID, configure URCs.
        _at_cmd(ser, "ATE0")
        _at_cmd(ser, "AT+CMGF=1")
        # Force SIM storage (often "SM") so that +CMTI indices match CMGR.
        _at_cmd(ser, 'AT+CPMS="SM","SM","SM"')
        # Use UCS2 output; then we decode HEX/utf-16-be with decode_maybe_ucs2().
        _at_cmd(ser, 'AT+CSCS="UCS2"')
        _at_cmd(ser, "AT+CLIP=1")
        # Clear SIM SMS memory on startup to avoid SMS FULL and old unread messages.
        _at_cmd(ser, "AT+CMGD=1,4", timeout_s=3.0)
        # CNMI=2,1,0,0,0 -> new SMS indication (+CMTI) and store in memory
        _at_cmd(ser, "AT+CNMI=2,1,0,0,0")

    def _read_sms_by_index(self, ser: serial.Serial, idx: int) -> dict | None:
        lines = _at_cmd(ser, f"AT+CMGR={idx}", timeout_s=3.0)
        # Typical:
        # +CMGR: "REC UNREAD","+7999...",,"26/03/18,12:34:56+12"
        # message text...
        header = None
        from_num = ""
        text_lines: list[str] = []
        for ln in lines:
            if ln.startswith("+CMGR:"):
                header = ln
            elif ln in ("OK", "ERROR") or ln.startswith("AT+"):
                continue
            else:
                text_lines.append(ln)
        if not header:
            return None
        # +CMGR: "REC UNREAD","+7999...",...,"timestamp"
        quoted = re.findall(r'"([^"]*)"', header)
        # quoted[0]=status, quoted[1]=phone, quoted[2]=subaddress (often ""), ...
        from_num = quoted[1].strip() if len(quoted) >= 2 else ""
        # With AT+CSCS="UCS2" some firmwares output phone in UTF-16BE hex too.
        from_num = decode_maybe_ucs2(from_num)
        text = "\n".join(text_lines).strip() if text_lines else ""
        text = decode_maybe_ucs2(text)
        # Best effort: delete after read to avoid memory filling up
        _at_cmd(ser, f"AT+CMGD={idx}", timeout_s=2.0)
        log.info("SMS received: idx=%s from=%s text_len=%s", idx, from_num, len(text))
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

    def _poll_unread_sms(self, ser: serial.Serial) -> list[dict]:
        # Use CMGL="REC UNREAD" to avoid dependency on +CMTI URC.
        last_out: list[str] = []
        phone_re = re.compile(r"^\+?\d{5,15}$")

        for attempt in range(3):
            out = _at_cmd(ser, 'AT+CMGL="REC UNREAD"', timeout_s=15.0)
            last_out = out

            events: list[dict] = []
            current_idx: int | None = None
            current_from: str = ""
            current_text_lines: list[str] = []
            processed_indices: list[int] = []

            def flush_current() -> None:
                nonlocal current_idx, current_from, current_text_lines
                if current_idx is None:
                    return
                text = "\n".join(current_text_lines).strip()
                text = decode_maybe_ucs2(text)
                events.append(
                    {"type": "sms", "timestamp": utc_now_iso(), "from": current_from, "text": text}
                )
                processed_indices.append(current_idx)
                current_idx = None
                current_from = ""
                current_text_lines = []

            for ln in out:
                if ln.startswith("+CMGL:"):
                    flush_current()
                    m_idx = re.search(r"^\+CMGL:\s*(\d+)", ln)
                    current_idx = int(m_idx.group(1)) if m_idx else None

                    quoted = re.findall(r'"([^"]*)"', ln)
                    phone = ""
                    for q in quoted:
                        if phone_re.match(q.strip()):
                            phone = q.strip()
                            break
                    if not phone and len(quoted) >= 2:
                        phone = quoted[1].strip()
                    current_from = decode_maybe_ucs2(phone)
                else:
                    if current_idx is not None:
                        current_text_lines.append(ln)

            flush_current()

            if events and processed_indices:
                for idx in processed_indices:
                    try:
                        _at_cmd(ser, f"AT+CMGD={idx}", timeout_s=5.0)
                    except Exception:
                        continue
                log.info("SMS polled: count=%s deleted=%s", len(events), len(processed_indices))
                return events

            # If modem reports full/err, clear and retry.
            upper = "\n".join(out).upper()
            if ("SMS FULL" in upper) or any("ERROR" == x for x in out) or ("+CMS ERROR" in upper):
                try:
                    _at_cmd(ser, "AT+CMGD=1,4", timeout_s=10.0)
                except Exception:
                    pass
                continue

            # No events and no clear reason.
            break

        # Give caller empty list; include last_out in logs for debugging.
        if last_out:
            log.info("SMS polled: no events (last_out_head=%s)", (last_out[:5]))
        return []

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
                    last_sms_poll = 0.0
                    self._pending_call_ts = None
                    while not self._stop.is_set():
                        now = time.time()
                        if now - last_lte_poll >= 30.0:
                            try:
                                self._poll_lte_metrics(ser)
                            except Exception as e:
                                self._last_err = str(e)
                            last_lte_poll = now

                        if now - last_sms_poll >= float(self._cfg.sms_poll_interval_s):
                            try:
                                sms_events = self._poll_unread_sms(ser)
                                for ev in sms_events:
                                    log.info("SMS polled: from=%s text_len=%s", ev.get("from"), len(ev.get("text", "")))
                                    self._q.put(ev)
                            except Exception as e:
                                self._last_err = str(e)
                                log.warning("SMS poll error: %s", e)
                            last_sms_poll = now

                        # If we saw RING but never got CLIP, send a fallback call event.
                        if self._pending_call_ts is not None and (now - self._pending_call_ts) >= 12.0:
                            self._q.put({"type": "call", "timestamp": utc_now_iso(), "from": "", "text": ""})
                            self._pending_call_ts = None

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
                                log.info("CMTI indication: idx=%s", idx)
                                sms = self._read_sms_by_index(ser, idx)
                                if sms:
                                    self._q.put(sms)
                            except Exception as e:
                                self._last_err = str(e)
                                log.warning("sms read error: %s", e)
                            continue

                        # When SIM memory is full or there is an SMS-related error,
                        # clear all SMS to restore reception.
                        if "SMS FULL" in line.upper() or line.startswith("+CMS ERROR"):
                            try:
                                log.warning("SMS capacity/error detected (%s). Clearing SIM SMS memory.", line)
                                _at_cmd(ser, "AT+CMGD=1,4", timeout_s=3.0)
                            except Exception as e:
                                self._last_err = str(e)
                            continue

                        if line == "RING":
                            # Don't enqueue yet: wait for +CLIP (it contains the phone).
                            log.info("CALL URC: RING (waiting CLIP)")
                            self._pending_call_ts = now
                            continue

                        m2 = CLIP_RE.match(line)
                        if m2:
                            from_num = m2.group(1).strip()
                            log.info("CALL URC: CLIP from=%s", from_num)
                            self._q.put({"type": "call", "timestamp": utc_now_iso(), "from": from_num, "text": ""})
                            self._pending_call_ts = None
                            continue
            except Exception as e:
                self._status["running"] = False
                self._last_err = str(e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


def event_to_json(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))

