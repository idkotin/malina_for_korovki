from __future__ import annotations

import unittest
from unittest.mock import patch

from host_monitor.modem_events import (
    ModemEventsCfg,
    ModemEventsReader,
    ModemResetRequested,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_reader(clock: FakeClock) -> ModemEventsReader:
    return ModemEventsReader(
        ModemEventsCfg(
            enabled=True,
            port="/dev/ttyUSB2",
            candidate_ports=[],
            baud=115200,
            sim_failure_recovery_enabled=True,
            sim_failure_confirm_s=90.0,
            sim_failure_reset_cooldown_s=1800.0,
            sim_failure_reset_window_s=21600.0,
            sim_failure_max_resets=3,
        ),
        monotonic=clock,
    )


class ModemRecoveryTests(unittest.TestCase):
    def test_confirmed_sim_failure_requests_modem_only_reset(self) -> None:
        clock = FakeClock()
        reader = make_reader(clock)
        replies = [
            ["+CME ERROR: SIM failure"],
            ["+CME ERROR: SIM failure"],
            [],
        ]

        with patch("host_monitor.modem_events._at_cmd", side_effect=replies) as at_cmd:
            reader._poll_sim_health_and_recover(object())  # type: ignore[arg-type]
            clock.advance(90.0)
            with self.assertRaises(ModemResetRequested):
                reader._poll_sim_health_and_recover(object())  # type: ignore[arg-type]

        self.assertEqual(
            [call.args[1] for call in at_cmd.call_args_list],
            ["AT+CPIN?", "AT+CPIN?", "AT+CFUN=1,1"],
        )
        recovery = reader.status()["sim_recovery"]
        self.assertEqual(recovery["sim_state"], "resetting")
        self.assertEqual(recovery["reset_count_in_window"], 1)

    def test_sim_busy_breaks_failure_confirmation(self) -> None:
        clock = FakeClock()
        reader = make_reader(clock)
        replies = [
            ["+CME ERROR: SIM failure"],
            ["+CME ERROR: SIM busy"],
            ["+CME ERROR: SIM failure"],
        ]

        with patch("host_monitor.modem_events._at_cmd", side_effect=replies) as at_cmd:
            reader._poll_sim_health_and_recover(object())  # type: ignore[arg-type]
            clock.advance(60.0)
            reader._poll_sim_health_and_recover(object())  # type: ignore[arg-type]
            clock.advance(60.0)
            reader._poll_sim_health_and_recover(object())  # type: ignore[arg-type]

        self.assertEqual([call.args[1] for call in at_cmd.call_args_list], ["AT+CPIN?"] * 3)
        recovery = reader.status()["sim_recovery"]
        self.assertEqual(recovery["sim_state"], "failure")
        self.assertEqual(recovery["failure_for_s"], 0.0)

    def test_generic_sms_error_never_deletes_sim_messages(self) -> None:
        reader = make_reader(FakeClock())

        with patch("host_monitor.modem_events._at_cmd", return_value=["ERROR"]) as at_cmd:
            events = reader._poll_unread_sms(object())  # type: ignore[arg-type]

        self.assertEqual(events, [])
        self.assertEqual([call.args[1] for call in at_cmd.call_args_list], ["AT+CMGL=0"])

    def test_explicit_sms_memory_full_is_cleared_once_and_retried(self) -> None:
        reader = make_reader(FakeClock())
        replies = [["+CMS ERROR: 322"], [], []]

        with patch("host_monitor.modem_events._at_cmd", side_effect=replies) as at_cmd:
            events = reader._poll_unread_sms(object())  # type: ignore[arg-type]

        self.assertEqual(events, [])
        self.assertEqual(
            [call.args[1] for call in at_cmd.call_args_list],
            ["AT+CMGL=0", "AT+CMGD=1,4", "AT+CMGL=0"],
        )


if __name__ == "__main__":
    unittest.main()
