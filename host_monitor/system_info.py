from __future__ import annotations

from pathlib import Path


def read_cpu_temp_c() -> float | None:
    # Raspberry Pi: /sys/class/thermal/thermal_zone0/temp returns millidegC
    p = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        raw = p.read_text().strip()
        if not raw:
            return None
        v = float(raw)
        if v > 1000:
            v = v / 1000.0
        return float(v)
    except Exception:
        return None

