from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass

import serial

from host_monitor.models import LteInfo


log = logging.getLogger("host_monitor.lte")


@dataclass(frozen=True)
class LteCfg:
    enabled: bool
    mmcli: str
    at_ports: list[str]
    at_baud: int


def _run(cmd: list[str], timeout_s: float = 3.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        return p.returncode, out.strip()
    except Exception as e:
        return 127, str(e)


def _mmcli_detect_modem(mmcli: str) -> str | None:
    rc, out = _run([mmcli, "-L"])
    if rc != 0:
        return None
    # Example: /org/freedesktop/ModemManager1/Modem/0 [QUALCOMM INCORPORATED] ...
    m = re.search(r"Modem/(\d+)", out)
    if not m:
        return None
    return m.group(1)


def _mmcli_get_signal(mmcli: str, modem_id: str) -> LteInfo | None:
    # Try signal-get first
    rc, out = _run([mmcli, "-m", modem_id, "--signal-get"])
    if rc == 0:
        # Parse something like: "rssi: -73 dBm" (format varies)
        rssi = None
        m = re.search(r"rssi:\s*(-?\d+)", out, re.IGNORECASE)
        if m:
            try:
                rssi = int(m.group(1))
            except Exception:
                rssi = None
        return LteInfo(ok=True, rssi_dbm=rssi)

    # Fallback: parse bearer/3gpp info for access tech
    rc2, out2 = _run([mmcli, "-m", modem_id])
    if rc2 != 0:
        return None
    tech = None
    m2 = re.search(r"access tech:\s*(.+)", out2, re.IGNORECASE)
    if m2:
        tech = m2.group(1).strip()
    return LteInfo(ok=True, access_tech=tech)


def _at_query(port: str, baud: int, cmd: str, timeout_s: float = 1.5) -> str:
    with serial.Serial(port, baudrate=baud, timeout=timeout_s) as s:
        s.reset_input_buffer()
        s.write((cmd + "\r").encode("ascii"))
        s.flush()
        lines: list[str] = []
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            raw = s.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="ignore").strip()
            if line:
                lines.append(line)
            if line == "OK" or line.startswith("ERROR"):
                break
        return "\n".join(lines)


def _lte_from_at(cfg: LteCfg) -> LteInfo:
    # Minimal, robust: try AT+CSQ for RSSI, AT+COPS? for tech (optional).
    last_err = None
    for p in cfg.at_ports:
        try:
            out = _at_query(p, cfg.at_baud, "AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),", out)
            rssi_dbm = None
            if m:
                csq = int(m.group(1))
                # 0..31 -> -113..-51 dBm, 99 unknown
                if 0 <= csq <= 31:
                    rssi_dbm = -113 + (2 * csq)
            tech = None
            out2 = _at_query(p, cfg.at_baud, "AT+COPS?")
            if "+COPS:" in out2:
                tech = "LTE/auto"
            return LteInfo(ok=True, rssi_dbm=rssi_dbm, access_tech=tech)
        except Exception as e:
            last_err = str(e)
            continue
    return LteInfo(ok=False, error=last_err or "no_at_port_worked")


def get_lte_info(cfg: LteCfg) -> LteInfo:
    if not cfg.enabled:
        return LteInfo(ok=False, error="disabled")

    modem_id = _mmcli_detect_modem(cfg.mmcli)
    if modem_id is not None:
        info = _mmcli_get_signal(cfg.mmcli, modem_id)
        if info is not None:
            if info.access_tech is None:
                # Best-effort: fill from mmcli -m output
                rc, out = _run([cfg.mmcli, "-m", modem_id])
                if rc == 0:
                    m = re.search(r"access tech:\s*(.+)", out, re.IGNORECASE)
                    if m:
                        info.access_tech = m.group(1).strip()
            return info

    return _lte_from_at(cfg)

