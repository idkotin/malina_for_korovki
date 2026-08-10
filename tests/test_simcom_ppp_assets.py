from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"


class SimcomPppAssetTests(unittest.TestCase):
    def test_udev_rule_matches_stable_ppp_interface(self) -> None:
        rule = (SYSTEMD / "99-simcom-ppp.rules").read_text(encoding="utf-8")

        self.assertIn('ID_VENDOR_ID}=="1e0e"', rule)
        self.assertIn('ID_MODEL_ID}=="9001"', rule)
        self.assertIn('ID_USB_INTERFACE_NUM}=="03"', rule)
        self.assertIn('SYMLINK+="simcom-ppp"', rule)

    def test_watchdog_is_conservative(self) -> None:
        script = (SYSTEMD / "simcom-ppp-watchdog").read_text(encoding="utf-8")

        self.assertIn('systemctl is-active --quiet "${LTE_UNIT}"', script)
        self.assertIn('if [[ ! -e "${PPP_DEVICE}" ]]', script)
        self.assertIn('MISSING_GRACE_S="${MISSING_GRACE_S:-120}"', script)
        self.assertIn('RESTART_COOLDOWN_S="${RESTART_COOLDOWN_S:-300}"', script)
        self.assertIn('systemctl --no-block restart "${LTE_UNIT}"', script)
        self.assertNotIn("reboot", script.lower())

    def test_installer_does_not_replace_live_config(self) -> None:
        script = (SYSTEMD / "install-simcom-ppp.sh").read_text(encoding="utf-8")

        self.assertIn("/etc/ppp/peers/${PEER_NAME}", script)
        self.assertIn("backup_if_exists", script)
        self.assertIn("--restart-lte", script)
        self.assertNotIn("config.yaml", script)


if __name__ == "__main__":
    unittest.main()
