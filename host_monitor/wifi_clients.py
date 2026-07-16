from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass


log = logging.getLogger("host_monitor.wifi")

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
ACTIVE_NEIGH_STATES = {"REACHABLE", "DELAY", "PROBE", "PERMANENT"}


@dataclass(frozen=True)
class WifiCfg:
    enabled: bool
    hostapd_cli: str
    ap_interface: str


def _run(cmd: list[str], timeout_s: float = 2.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        return p.returncode, out.strip()
    except Exception as e:
        return 127, str(e)


def _parse_hostapd_all_sta(text: str) -> list[str]:
    macs: list[str] = []
    for m in MAC_RE.finditer(text):
        macs.append(m.group(1).lower())
    return sorted(set(macs))


def _parse_active_ip_neigh(text: str) -> list[str]:
    """Exclude stale neighbor-cache entries that may belong to disconnected clients."""
    macs: list[str] = []
    for line in text.splitlines():
        fields = line.upper().split()
        if not ACTIVE_NEIGH_STATES.intersection(fields):
            continue
        match = MAC_RE.search(line)
        if match:
            macs.append(match.group(1).lower())
    return sorted(set(macs))


def get_wifi_clients(cfg: WifiCfg) -> tuple[list[str], str | None]:
    if not cfg.enabled:
        return [], None

    # Best for AP mode: hostapd_cli all_sta
    rc, out = _run([cfg.hostapd_cli, "-i", cfg.ap_interface, "all_sta"])
    if rc == 0:
        return _parse_hostapd_all_sta(out), None

    # Fallback: ip neigh show dev wlan0
    rc2, out2 = _run(["ip", "neigh", "show", "dev", cfg.ap_interface])
    if rc2 == 0:
        return _parse_active_ip_neigh(out2), None

    return [], f"hostapd_cli/ip failed: {out or out2}"

