from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from host_monitor.recovery_watchdog import RecoveryWatchdog, RecoveryWatchdogCfg


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_cfg(state_path: Path, *, enabled: bool = True) -> RecoveryWatchdogCfg:
    return RecoveryWatchdogCfg(
        enabled=enabled,
        telemetry_inactive_s=900.0,
        terminal_off_below_raw_kg=-1000.0,
        terminal_off_confirm_s=30.0,
        max_weight_age_s=10.0,
        healthy_success_max_age_s=10.0,
        healthy_reset_confirm_s=60.0,
        state_path=str(state_path),
    )


class RecoveryWatchdogTests(unittest.TestCase):
    def test_requests_only_after_both_guards_are_continuously_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = FakeClock()
            calls: list[str] = []
            watchdog = RecoveryWatchdog(
                make_cfg(Path(td) / "state.json"),
                reboot_action=lambda: calls.append("reboot"),
                monotonic=clock,
            )

            first = watchdog.observe(
                telemetry_last_success_age_s=None,
                raw_weight_kg=-1500.0,
                weight_age_s=1.0,
            )
            self.assertFalse(first["telemetry_inactive"])
            clock.advance(899.0)
            almost = watchdog.observe(
                telemetry_last_success_age_s=None,
                raw_weight_kg=-1500.0,
                weight_age_s=1.0,
            )
            self.assertFalse(almost["reboot_requested"])

            clock.advance(1.0)
            triggered = watchdog.observe(
                telemetry_last_success_age_s=None,
                raw_weight_kg=-1500.0,
                weight_age_s=1.0,
            )

            self.assertTrue(triggered["telemetry_inactive"])
            self.assertTrue(triggered["terminal_off_confirmed"])
            self.assertTrue(triggered["reboot_requested"])
            self.assertEqual(calls, ["reboot"])

    def test_threshold_is_strictly_below_minus_1000(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = FakeClock()
            calls: list[str] = []
            watchdog = RecoveryWatchdog(
                make_cfg(Path(td) / "state.json"),
                reboot_action=lambda: calls.append("reboot"),
                monotonic=clock,
            )
            clock.advance(1000.0)

            status = watchdog.observe(
                telemetry_last_success_age_s=1000.0,
                raw_weight_kg=-1000.0,
                weight_age_s=1.0,
            )

            self.assertFalse(status["terminal_off_now"])
            self.assertEqual(calls, [])

    def test_stale_weight_never_allows_reboot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = FakeClock()
            calls: list[str] = []
            watchdog = RecoveryWatchdog(
                make_cfg(Path(td) / "state.json"),
                reboot_action=lambda: calls.append("reboot"),
                monotonic=clock,
            )
            clock.advance(1000.0)

            status = watchdog.observe(
                telemetry_last_success_age_s=1000.0,
                raw_weight_kg=-2000.0,
                weight_age_s=11.0,
            )

            self.assertFalse(status["weight_fresh"])
            self.assertFalse(status["terminal_off_now"])
            self.assertEqual(calls, [])

    def test_one_reboot_latches_persistently_until_sustained_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            clock = FakeClock()
            calls: list[str] = []
            watchdog = RecoveryWatchdog(
                make_cfg(state_path),
                reboot_action=lambda: calls.append("first"),
                monotonic=clock,
            )
            watchdog.observe(
                telemetry_last_success_age_s=1000.0,
                raw_weight_kg=-2000.0,
                weight_age_s=1.0,
            )
            clock.advance(30.0)
            watchdog.observe(
                telemetry_last_success_age_s=1030.0,
                raw_weight_kg=-2000.0,
                weight_age_s=1.0,
            )
            self.assertEqual(calls, ["first"])
            self.assertTrue(json.loads(state_path.read_text(encoding="utf-8"))["reboot_latched"])

            after_restart_calls: list[str] = []
            after_restart = RecoveryWatchdog(
                make_cfg(state_path),
                reboot_action=lambda: after_restart_calls.append("second"),
                monotonic=clock,
            )
            clock.advance(2000.0)
            after_restart.observe(
                telemetry_last_success_age_s=2000.0,
                raw_weight_kg=-2000.0,
                weight_age_s=1.0,
            )
            self.assertEqual(after_restart_calls, [])

            after_restart.observe(
                telemetry_last_success_age_s=1.0,
                raw_weight_kg=0.0,
                weight_age_s=1.0,
            )
            clock.advance(60.0)
            recovered = after_restart.observe(
                telemetry_last_success_age_s=1.0,
                raw_weight_kg=0.0,
                weight_age_s=1.0,
            )
            self.assertFalse(recovered["latched"])
            self.assertFalse(json.loads(state_path.read_text(encoding="utf-8"))["reboot_latched"])

    def test_disabled_watchdog_never_requests_reboot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = FakeClock()
            calls: list[str] = []
            watchdog = RecoveryWatchdog(
                make_cfg(Path(td) / "state.json", enabled=False),
                reboot_action=lambda: calls.append("reboot"),
                monotonic=clock,
            )
            clock.advance(1000.0)
            watchdog.observe(
                telemetry_last_success_age_s=1000.0,
                raw_weight_kg=-2000.0,
                weight_age_s=1.0,
            )
            clock.advance(30.0)
            status = watchdog.observe(
                telemetry_last_success_age_s=1030.0,
                raw_weight_kg=-2000.0,
                weight_age_s=1.0,
            )

            self.assertFalse(status["reboot_requested"])
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
