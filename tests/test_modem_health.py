from __future__ import annotations

import unittest
from unittest.mock import patch

from host_monitor.modem_events import (
    ModemEventsCfg,
    ModemEventsReader,
    _parse_modem_temperature_c,
    _parse_modem_voltage_v,
)


class ModemHealthParsingTests(unittest.TestCase):
    def test_parses_temperature_variants(self) -> None:
        self.assertEqual(_parse_modem_temperature_c(["+CPMUTEMP: 46"]), 46.0)
        self.assertEqual(_parse_modem_temperature_c(["+CPMUTEMP: -5.5 C"]), -5.5)
        self.assertIsNone(_parse_modem_temperature_c(["ERROR"]))

    def test_parses_voltage_variants(self) -> None:
        self.assertEqual(_parse_modem_voltage_v(["+CBC: 3.591V"]), 3.591)
        self.assertEqual(_parse_modem_voltage_v(["+CBC: 3591mV"]), 3.591)
        self.assertIsNone(_parse_modem_voltage_v(["ERROR"]))

    def test_poll_publishes_health_from_the_single_at_reader(self) -> None:
        reader = ModemEventsReader(
            ModemEventsCfg(
                enabled=True,
                port="/dev/ttyUSB2",
                candidate_ports=[],
                baud=115200,
            )
        )
        replies = [
            ["+CSQ: 20,99"],
            ['+COPS: 0,0,"MTS RUS",7'],
            ["+CPMUTEMP: 52"],
            ["+CBC: 3.742V"],
        ]

        with patch("host_monitor.modem_events._at_cmd", side_effect=replies) as at_cmd:
            reader._poll_lte_metrics(object())  # type: ignore[arg-type]

        self.assertEqual(
            [call.args[1] for call in at_cmd.call_args_list],
            ["AT+CSQ", "AT+COPS?", "AT+CPMUTEMP", "AT+CBC"],
        )
        snapshot = reader.lte_snapshot()
        self.assertEqual(snapshot["rssi_dbm"], -73)
        self.assertEqual(snapshot["access_tech"], "LTE/auto")
        self.assertEqual(snapshot["module_temp_c"], 52.0)
        self.assertEqual(snapshot["module_voltage_v"], 3.742)


if __name__ == "__main__":
    unittest.main()
