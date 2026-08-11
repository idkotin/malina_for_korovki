from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"


class SimcomDiagnosticsAssetTests(unittest.TestCase):
    def test_journal_is_persistent_and_bounded(self) -> None:
        config = (SYSTEMD / "99-korovki-journal.conf").read_text(encoding="utf-8")

        self.assertIn("Storage=persistent", config)
        self.assertIn("SystemMaxUse=256M", config)
        self.assertIn("SystemKeepFree=512M", config)
        self.assertIn("MaxRetentionSec=14day", config)
        self.assertIn("SyncIntervalSec=30s", config)

    def test_periodic_probe_does_not_touch_modem_control_protocols(self) -> None:
        script = (SYSTEMD / "simcom-diagnostics").read_text(encoding="utf-8")

        self.assertIn("vcgencmd get_throttled", script)
        self.assertIn("lsusb", script)
        self.assertIn("uhubctl -l 1-1", script)
        self.assertIn("ip -br address show ppp0", script)
        self.assertIn("ping -c 1 -W 2 1.1.1.1", script)
        self.assertIn("getent ahostsv4 vi-korm.ru", script)
        self.assertNotIn("qmicli", script)
        self.assertNotIn("AT+", script)
        self.assertNotIn("ttyUSB2", script)

    def test_incident_bundle_is_bounded_and_contains_relevant_journals(self) -> None:
        script = (SYSTEMD / "capture-simcom-incident").read_text(encoding="utf-8")

        self.assertIn("KEEP_FILES=\"${KEEP_FILES:-30}\"", script)
        self.assertIn('journalctl -k --since "20 minutes ago"', script)
        self.assertIn("journalctl -u lte.service", script)
        self.assertIn("journalctl -u host-monitor.service", script)
        self.assertIn("journalctl -t simcom-ppp-watchdog", script)
        self.assertIn("journalctl -b -1 -k -n 1000", script)
        self.assertIn("gzip -9", script)

    def test_watchdog_captures_before_mutating_recovery(self) -> None:
        script = (SYSTEMD / "simcom-ppp-watchdog").read_text(encoding="utf-8")

        usb_capture = script.index('capture_incident "before-usb-power-cycle"')
        usb_cycle = script.index('uhubctl -l "${USB_HUB_LOCATION}" -a cycle -d 10')
        lte_capture = script.index('capture_incident "before-lte-restart"')
        lte_restart = script.index('systemctl --no-block restart "${LTE_UNIT}"')
        self.assertLess(usb_capture, usb_cycle)
        self.assertLess(lte_capture, lte_restart)

    def test_installer_enables_timer_and_ppp_transition_hooks(self) -> None:
        script = (SYSTEMD / "install-simcom-diagnostics.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("/var/log/journal", script)
        self.assertIn("systemctl restart systemd-journald.service", script)
        self.assertIn("systemctl enable --now simcom-diagnostics.timer", script)
        self.assertIn("/etc/ppp/ip-up.d/90-simcom-ppp-transition", script)
        self.assertIn("/etc/ppp/ip-down.d/90-simcom-ppp-transition", script)
        self.assertIn('"${SCRIPT_DIR}/simcom-ppp-watchdog"', script)


if __name__ == "__main__":
    unittest.main()
