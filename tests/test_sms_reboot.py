from __future__ import annotations

import unittest
from unittest.mock import patch

from host_monitor.modem_events import ModemEventsCfg, ModemEventsReader, normalize_sms_number
from host_monitor.system_actions import request_system_reboot


ALLOWED_NUMBER = "+79991234567"


def make_reader(action, *, enabled: bool = True) -> ModemEventsReader:
    return ModemEventsReader(
        ModemEventsCfg(
            enabled=True,
            port=None,
            candidate_ports=[],
            baud=115200,
            sms_reboot_enabled=enabled,
            sms_reboot_allowed_number=ALLOWED_NUMBER,
            sms_reboot_command="/reboot",
        ),
        reboot_action=action,
    )


class SmsRebootTests(unittest.TestCase):
    def test_normalizes_common_russian_number_forms(self) -> None:
        self.assertEqual(normalize_sms_number("+7 (999) 123-45-67"), "79991234567")
        self.assertEqual(normalize_sms_number("8 999 123 45 67"), "79991234567")

    def test_authorized_exact_command_requests_reboot_and_is_consumed(self) -> None:
        calls: list[str] = []
        reader = make_reader(lambda: calls.append("reboot"))

        reader._dispatch_event({"type": "sms", "from": ALLOWED_NUMBER, "text": " /reboot\n"})

        self.assertEqual(calls, ["reboot"])
        self.assertEqual(reader.drain(), [])

    def test_wrong_number_does_not_request_reboot_and_sms_is_preserved(self) -> None:
        calls: list[str] = []
        reader = make_reader(lambda: calls.append("reboot"))
        event = {"type": "sms", "from": "+79990000000", "text": "/reboot"}

        reader._dispatch_event(event)

        self.assertEqual(calls, [])
        self.assertEqual(reader.drain(), [event])

    @patch("host_monitor.system_actions.subprocess.Popen")
    def test_system_reboot_uses_fixed_systemctl_argv_without_shell(self, popen) -> None:
        request_system_reboot()

        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/usr/bin/systemctl", "reboot"])
        self.assertNotIn("shell", kwargs)

    def test_wrong_or_case_changed_command_does_not_request_reboot(self) -> None:
        calls: list[str] = []
        reader = make_reader(lambda: calls.append("reboot"))

        reader._dispatch_event({"type": "sms", "from": ALLOWED_NUMBER, "text": "/Reboot"})

        self.assertEqual(calls, [])
        self.assertEqual(len(reader.drain()), 1)

    def test_disabled_feature_does_not_request_reboot(self) -> None:
        calls: list[str] = []
        reader = make_reader(lambda: calls.append("reboot"), enabled=False)

        reader._dispatch_event({"type": "sms", "from": ALLOWED_NUMBER, "text": "/reboot"})

        self.assertEqual(calls, [])
        self.assertEqual(len(reader.drain()), 1)

    def test_duplicate_authorized_command_requests_reboot_once(self) -> None:
        calls: list[str] = []
        reader = make_reader(lambda: calls.append("reboot"))
        event = {"type": "sms", "from": ALLOWED_NUMBER, "text": "/reboot"}

        reader._dispatch_event(event)
        reader._dispatch_event(event)

        self.assertEqual(calls, ["reboot"])
        self.assertEqual(reader.drain(), [])

    def test_failed_reboot_action_preserves_sms_for_diagnostics(self) -> None:
        def fail() -> None:
            raise RuntimeError("no systemd")

        reader = make_reader(fail)
        event = {"type": "sms", "from": ALLOWED_NUMBER, "text": "/reboot"}

        reader._dispatch_event(event)

        self.assertEqual(reader.drain(), [event])


if __name__ == "__main__":
    unittest.main()
