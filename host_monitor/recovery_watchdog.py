from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


log = logging.getLogger("host_monitor.recovery_watchdog")


@dataclass(frozen=True)
class RecoveryWatchdogCfg:
    enabled: bool
    telemetry_inactive_s: float
    terminal_off_below_raw_kg: float
    terminal_off_confirm_s: float
    max_weight_age_s: float
    healthy_success_max_age_s: float
    healthy_reset_confirm_s: float
    state_path: str


class RecoveryWatchdog:
    """Request at most one reboot per confirmed telemetry outage.

    The reboot gate deliberately uses the unfiltered calibrated weight. The
    normal weight pipeline rejects values below -1000 kg as invalid, but that
    same raw value is the production signal that the factory terminal is off.
    """

    def __init__(
        self,
        cfg: RecoveryWatchdogCfg,
        *,
        reboot_action: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._cfg = cfg
        self._reboot_action = reboot_action
        self._monotonic = monotonic
        self._started_monotonic = monotonic()
        self._terminal_off_since: float | None = None
        self._healthy_since: float | None = None
        self._latched = self._load_latched()
        self._last_status: dict[str, Any] = {}

    def _load_latched(self) -> bool:
        path = Path(self._cfg.state_path)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return bool(data.get("reboot_latched", False))
        except Exception:
            # A damaged state file must not permit an uncontrolled reboot loop.
            log.exception("cannot read auto-reboot state; keeping recovery latched for safety")
            return True

    def _persist_latch(self, value: bool, reason: str) -> None:
        path = Path(self._cfg.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        payload = {
            "reboot_latched": bool(value),
            "reason": reason,
            "updated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    @staticmethod
    def _finite(value: float | None) -> bool:
        return value is not None and math.isfinite(float(value))

    def observe(
        self,
        *,
        telemetry_last_success_age_s: float | None,
        raw_weight_kg: float | None,
        weight_age_s: float | None,
    ) -> dict[str, Any]:
        now = self._monotonic()
        configured_inactive_s = max(1.0, float(self._cfg.telemetry_inactive_s))
        if self._finite(telemetry_last_success_age_s):
            effective_success_age_s = max(0.0, float(telemetry_last_success_age_s))
        else:
            effective_success_age_s = max(0.0, now - self._started_monotonic)

        telemetry_inactive = effective_success_age_s >= configured_inactive_s
        healthy_recent = (
            self._finite(telemetry_last_success_age_s)
            and float(telemetry_last_success_age_s)
            <= max(0.1, float(self._cfg.healthy_success_max_age_s))
        )
        weight_fresh = (
            self._finite(weight_age_s)
            and 0.0 <= float(weight_age_s) <= max(0.1, float(self._cfg.max_weight_age_s))
        )
        terminal_off_now = (
            weight_fresh
            and self._finite(raw_weight_kg)
            and float(raw_weight_kg) < float(self._cfg.terminal_off_below_raw_kg)
        )

        if terminal_off_now:
            if self._terminal_off_since is None:
                self._terminal_off_since = now
        else:
            self._terminal_off_since = None
        terminal_off_for_s = (
            0.0 if self._terminal_off_since is None else max(0.0, now - self._terminal_off_since)
        )
        terminal_off_confirmed = (
            self._terminal_off_since is not None
            and terminal_off_for_s >= max(0.0, float(self._cfg.terminal_off_confirm_s))
        )

        if self._latched:
            if healthy_recent:
                if self._healthy_since is None:
                    self._healthy_since = now
                if now - self._healthy_since >= max(0.0, float(self._cfg.healthy_reset_confirm_s)):
                    self._persist_latch(False, "telemetry_recovered")
                    self._latched = False
                    self._healthy_since = None
                    log.warning("auto-reboot latch cleared after sustained telemetry recovery")
            else:
                self._healthy_since = None
        else:
            self._healthy_since = None

        requested = False
        if (
            self._cfg.enabled
            and not self._latched
            and telemetry_inactive
            and terminal_off_confirmed
        ):
            # Persist before requesting reboot so a successful restart cannot
            # immediately begin another 15-minute reboot cycle.
            self._persist_latch(True, "telemetry_inactive_and_terminal_off")
            self._latched = True
            requested = True
            log.critical(
                "AUTO REBOOT: no telemetry ACK for %.1fs and raw weight %.3f kg is below %.3f kg",
                effective_success_age_s,
                float(raw_weight_kg),
                float(self._cfg.terminal_off_below_raw_kg),
            )
            try:
                self._reboot_action()
            except Exception:
                log.exception("automatic reboot request failed; latch remains set for safety")

        self._last_status = {
            "enabled": self._cfg.enabled,
            "latched": self._latched,
            "telemetry_last_success_age_s": effective_success_age_s,
            "telemetry_inactive": telemetry_inactive,
            "raw_weight_kg": raw_weight_kg,
            "weight_age_s": weight_age_s,
            "weight_fresh": weight_fresh,
            "terminal_off_now": terminal_off_now,
            "terminal_off_for_s": terminal_off_for_s,
            "terminal_off_confirmed": terminal_off_confirmed,
            "reboot_requested": requested,
        }
        return dict(self._last_status)

    def status(self) -> dict[str, Any]:
        return dict(self._last_status)
