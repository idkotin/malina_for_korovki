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

    def test_watchdog_has_guarded_usb_recovery(self) -> None:
        script = (SYSTEMD / "simcom-ppp-watchdog").read_text(encoding="utf-8")

        self.assertIn('systemctl is-active --quiet "${LTE_UNIT}"', script)
        self.assertIn('if [[ ! -e "${PPP_DEVICE}" ]]', script)
        self.assertIn('MISSING_GRACE_S="${MISSING_GRACE_S:-120}"', script)
        self.assertIn('RESTART_COOLDOWN_S="${RESTART_COOLDOWN_S:-300}"', script)
        self.assertIn('systemctl --no-block restart "${LTE_UNIT}"', script)
        self.assertNotIn("reboot", script.lower())
        self.assertIn('USB_POWER_CYCLE_ENABLED="${USB_POWER_CYCLE_ENABLED:-0}"', script)
        self.assertIn('Raspberry Pi 4 Model B', script)
        self.assertIn('usb_inventory_is_safe', script)
        self.assertIn('uhubctl -l "${USB_HUB_LOCATION}" -a cycle -d 10', script)
        self.assertIn('USB_CYCLE_COOLDOWN_S="${USB_CYCLE_COOLDOWN_S:-1800}"', script)
        self.assertIn('unexpected USB device ${usb_id} is connected', script)

        drop_in = (SYSTEMD / "simcom-ppp-watchdog-usb-cycle.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("Environment=USB_POWER_CYCLE_ENABLED=1", drop_in)
        self.assertIn("Environment=USB_MISSING_GRACE_S=300", drop_in)

        service = (SYSTEMD / "simcom-ppp-watchdog.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("TimeoutStartSec=150", service)

    def test_installer_does_not_replace_live_config(self) -> None:
        script = (SYSTEMD / "install-simcom-ppp.sh").read_text(encoding="utf-8")

        self.assertIn("/etc/ppp/peers/${PEER_NAME}", script)
        self.assertIn("backup_if_exists", script)
        self.assertIn("--restart-lte", script)
        self.assertIn("--enable-usb-power-cycle", script)
        self.assertNotIn("config.yaml", script)


if __name__ == "__main__":
    unittest.main()
