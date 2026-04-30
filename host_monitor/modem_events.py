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
GSM7_DEFAULT_ALPHABET = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e"
    "\x1b\u00c6\u00e6\u00df\u00c9 !\"#\u00a4%&'()*+,-./"
    "0123456789:;<=>?\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
GSM7_EXT_ALPHABET = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "\u20ac",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class ModemEventsCfg:
    enabled: bool
    port: str | None
    candidate_ports: list[str]
    baud: int
    sms_poll_interval_s: float = 30.0


@dataclass(frozen=True)
class DecodedSms:
    idx: int
    sender: str
    text: str
    concat_ref: str | None = None
    concat_total: int | None = None
    concat_seq: int | None = None


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


def _at_ping_ok(ser: serial.Serial, timeout_s: float = 1.5) -> bool:
    """
    Confirm that the selected serial port is a real AT command port.
    We treat the port as valid only if the modem explicitly returns OK.
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(b"AT\r")
    ser.flush()

    deadline = time.time() + timeout_s
    while True:
        line = _at_readline(ser, deadline)
        if line is None:
            return False
        if line == "OK":
            return True
        if line.startswith("ERROR"):
            return False


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


def _decode_semi_octet_number(addr_hex: str, digit_count: int, toa: int) -> str:
    digits: list[str] = []
    for i in range(0, len(addr_hex), 2):
        pair = addr_hex[i : i + 2]
        if len(pair) < 2:
            continue
        digits.extend([pair[1], pair[0]])
    value = "".join(digits)[:digit_count].rstrip("F")
    if value and (toa & 0x70) == 0x10 and not value.startswith("+"):
        return "+" + value
    return value


def _decode_gsm7(data: bytes, septet_count: int, skip_septets: int = 0) -> str:
    chars: list[str] = []
    escape = False
    total = max(0, int(septet_count))
    for i in range(total):
        bit_offset = i * 7
        byte_i = bit_offset // 8
        shift = bit_offset % 8
        if byte_i >= len(data):
            break
        value = (data[byte_i] >> shift) & 0x7F
        if shift > 1 and byte_i + 1 < len(data):
            value |= (data[byte_i + 1] << (8 - shift)) & 0x7F

        if i < skip_septets:
            continue
        if escape:
            chars.append(GSM7_EXT_ALPHABET.get(value, " "))
            escape = False
            continue
        if value == 0x1B:
            escape = True
            continue
        chars.append(GSM7_DEFAULT_ALPHABET[value] if value < len(GSM7_DEFAULT_ALPHABET) else " ")
    return "".join(chars).strip()


def _sms_dcs_alphabet(dcs: int) -> str:
    alphabet_bits = dcs & 0x0C
    if alphabet_bits == 0x08:
        return "ucs2"
    if alphabet_bits == 0x04:
        return "8bit"
    return "gsm7"


def _parse_concat_udh(udh: bytes) -> tuple[str | None, int | None, int | None]:
    pos = 0
    while pos + 2 <= len(udh):
        iei = udh[pos]
        iedl = udh[pos + 1]
        pos += 2
        data = udh[pos : pos + iedl]
        pos += iedl
        if iei == 0x00 and len(data) == 3:
            return (f"8:{data[0]}", int(data[1]), int(data[2]))
        if iei == 0x08 and len(data) == 4:
            ref = (data[0] << 8) | data[1]
            return (f"16:{ref}", int(data[2]), int(data[3]))
    return None, None, None


def _decode_sms_deliver_pdu(idx: int, pdu_hex: str) -> DecodedSms | None:
    pdu_hex = "".join(ch for ch in pdu_hex.strip() if not ch.isspace())
    if not pdu_hex or not HEX_RE.match(pdu_hex):
        return None
    try:
        data = bytes.fromhex(pdu_hex)
    except ValueError:
        return None
    if len(data) < 12:
        return None

    pos = 0
    smsc_len = data[pos]
    pos += 1 + smsc_len
    if pos >= len(data):
        return None

    first_octet = data[pos]
    pos += 1
    if (first_octet & 0x03) != 0x00:
        return None
    has_udh = bool(first_octet & 0x40)

    if pos + 2 > len(data):
        return None
    sender_len = data[pos]
    pos += 1
    sender_toa = data[pos]
    pos += 1
    sender_octets = (sender_len + 1) // 2
    if pos + sender_octets + 10 > len(data):
        return None
    sender_hex = data[pos : pos + sender_octets].hex().upper()
    pos += sender_octets
    sender = _decode_semi_octet_number(sender_hex, sender_len, sender_toa)

    pos += 1  # TP-PID
    dcs = data[pos]
    pos += 1
    pos += 7  # TP-SCTS
    if pos >= len(data):
        return None
    udl = data[pos]
    pos += 1

    alphabet = _sms_dcs_alphabet(dcs)
    if alphabet == "gsm7":
        user_data_len = (udl * 7 + 7) // 8
    else:
        user_data_len = udl
    user_data = data[pos : min(len(data), pos + user_data_len)]

    udh_octets = 0
    concat_ref: str | None = None
    concat_total: int | None = None
    concat_seq: int | None = None
    if has_udh and user_data:
        udhl = int(user_data[0])
        udh_octets = min(len(user_data), 1 + udhl)
        concat_ref, concat_total, concat_seq = _parse_concat_udh(user_data[1:udh_octets])

    if alphabet == "ucs2":
        body = user_data[udh_octets:]
        text = body.decode("utf-16-be", errors="replace").replace("\x00", "").strip()
    elif alphabet == "8bit":
        body = user_data[udh_octets:]
        text = body.decode("utf-8", errors="replace").strip()
    else:
        skip_septets = ((udh_octets * 8) + 6) // 7 if has_udh else 0
        text = _decode_gsm7(user_data, udl, skip_septets=skip_septets)

    return DecodedSms(
        idx=idx,
        sender=sender,
        text=text,
        concat_ref=concat_ref,
        concat_total=concat_total,
        concat_seq=concat_seq,
    )


def _assemble_sms_events(messages: list[DecodedSms]) -> tuple[list[dict], list[int]]:
    events: list[dict] = []
    processed_indices: list[int] = []
    groups: dict[tuple[str, str, int], list[DecodedSms]] = {}

    for msg in messages:
        if msg.concat_ref and msg.concat_total and msg.concat_total > 1 and msg.concat_seq:
            key = (msg.sender, msg.concat_ref, msg.concat_total)
            groups.setdefault(key, []).append(msg)
            continue
        events.append({"type": "sms", "timestamp": utc_now_iso(), "from": msg.sender, "text": msg.text})
        processed_indices.append(msg.idx)

    for (sender, ref, total), parts in groups.items():
        by_seq = {p.concat_seq: p for p in parts if p.concat_seq is not None}
        if all(seq in by_seq for seq in range(1, total + 1)):
            ordered = [by_seq[seq] for seq in range(1, total + 1)]
            text = "".join(p.text for p in ordered)
            events.append({"type": "sms", "timestamp": utc_now_iso(), "from": sender, "text": text})
            processed_indices.extend(p.idx for p in ordered)
            log.info("SMS multipart assembled: from=%s ref=%s parts=%s text_len=%s", sender, ref, total, len(text))
        else:
            have = sorted(seq for seq in by_seq if seq is not None)
            log.info("SMS multipart incomplete: from=%s ref=%s have=%s total=%s", sender, ref, have, total)

    return events, processed_indices


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
                # Basic probe: only accept ports that explicitly answer "OK" to AT.
                if not _at_ping_ok(ser):
                    ser.close()
                    continue
                log.info("AT port selected: %s baud=%s", p, self._cfg.baud)
                return ser
            except Exception as e:
                last_exc = e
                continue
        raise RuntimeError(f"no modem events port available: {last_exc}")

    def _configure_modem(self, ser: serial.Serial) -> None:
        # PDU mode preserves multipart SMS headers, so we can assemble long SMS.
        _at_cmd(ser, "ATE0")
        _at_cmd(ser, "AT+CMGF=0")
        # Force SIM storage (often "SM") so that +CMTI indices match CMGR.
        _at_cmd(ser, 'AT+CPMS="SM","SM","SM"')
        _at_cmd(ser, "AT+CLIP=1")
        # CNMI=2,1,0,0,0 -> new SMS indication (+CMTI) and store in memory
        _at_cmd(ser, "AT+CNMI=2,1,0,0,0")

    def _read_sms_by_index(self, ser: serial.Serial, idx: int) -> dict | None:
        lines = _at_cmd(ser, f"AT+CMGR={idx}", timeout_s=3.0)
        pdu_line = ""
        for ln in lines:
            if HEX_RE.match(ln.strip()):
                pdu_line = ln.strip()
                break
        if not pdu_line:
            return None
        sms = _decode_sms_deliver_pdu(idx, pdu_line)
        if sms is None:
            return None
        # Best effort: delete after read to avoid memory filling up
        _at_cmd(ser, f"AT+CMGD={idx}", timeout_s=2.0)
        log.info("SMS received: idx=%s from=%s text_len=%s", idx, sms.sender, len(sms.text))
        return {"type": "sms", "timestamp": utc_now_iso(), "from": sms.sender, "text": sms.text}

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
        # PDU mode keeps UDH multipart metadata; text mode can make long SMS look
        # like separate truncated messages.
        last_out: list[str] = []

        for attempt in range(3):
            out = _at_cmd(ser, "AT+CMGL=0", timeout_s=15.0)
            last_out = out

            messages: list[DecodedSms] = []
            current_idx: int | None = None

            for ln in out:
                if ln.startswith("+CMGL:"):
                    m_idx = re.search(r"^\+CMGL:\s*(\d+)", ln)
                    current_idx = int(m_idx.group(1)) if m_idx else None
                    continue
                if current_idx is not None and HEX_RE.match(ln.strip()):
                    sms = _decode_sms_deliver_pdu(current_idx, ln.strip())
                    if sms is not None:
                        messages.append(sms)
                    current_idx = None

            events, processed_indices = _assemble_sms_events(messages)

            if events:
                for idx in sorted(set(processed_indices)):
                    try:
                        _at_cmd(ser, f"AT+CMGD={idx}", timeout_s=3.0)
                    except Exception:
                        pass
                log.info("SMS polled: count=%s deleted_indices=%s", len(events), sorted(set(processed_indices)))
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
                        # When polling is enabled, we avoid processing +CMTI URC to prevent duplicates.
                        if m and float(self._cfg.sms_poll_interval_s) <= 0.0:
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

