# host-monitor (Raspberry Pi)

Russian version: [README.ru.md](./README.ru.md)

Telemetry client for Raspberry Pi 4 + SIM7600 + Waveshare ADS1263.

- Sends telemetry via HTTP POST (JSON) at configured frequency
- Buffers unsent telemetry and modem events in SQLite during outages
- Reads GPS from modem serial ports with auto-detect
- Reads SMS/call events from the modem AT port and sends them to a separate API
- Reads weight from a passive parallel tap of an existing load-cell system

## 1) Telemetry JSON format

Current telemetry payload:

```json
{
  "device_id": "isrk_hozyain_01",
  "timestamp": "2026-03-11T20:34:38",
  "lat": 55.109311,
  "lon": 82.812417,
  "gps_valid": true,
  "gps_satellites": 12,
  "weight": 1234.56,
  "weight_valid": true,
  "gps_quality": 1,
  "wifi_clients": ["aa:bb:cc:dd:ee:ff"],
  "cpu_temp_c": 61.2,
  "lte_rssi_dbm": -75,
  "lte_access_tech": "LTE",
  "events_reader_ok": true
}
```

Field meaning for health flags:

- `gps_valid`: `true` when current GPS coordinates come from a valid fix
- `weight_valid`: `true` when the current weight value was read successfully
- `events_reader_ok`: `true` when the modem events reader is healthy, or when modem events are disabled in config

SMS/call events use a separate endpoint (`events.url`):

```json
{
  "device_id": "isrk_hozyain_01",
  "type": "sms",
  "timestamp": "2026-03-11T20:35:01",
  "from": "+79991234567",
  "text": "hello"
}
```

Call event example:

```json
{
  "device_id": "isrk_hozyain_01",
  "type": "call",
  "timestamp": "2026-03-11T20:35:10",
  "from": "+79991234567",
  "text": ""
}
```

## 2) Install on Raspberry Pi OS

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git hostapd modemmanager
```

Enable SPI for ADS1263:

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable
sudo reboot
```

Clone project and install it in a virtual environment:

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone https://github.com/idkotin/malina_for_korovki.git host-monitor
sudo chown -R $USER:$USER /opt/host-monitor
cd /opt/host-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

Run manually with the venv Python:

```bash
/opt/host-monitor/.venv/bin/python -m host_monitor.main --config /opt/host-monitor/config.yaml
```

Install the Waveshare ADS1263 Python library:

```bash
cd /opt
git clone https://github.com/waveshareteam/High-Pricision_AD_HAT.git
```

Set `weight.waveshare_path` in `config.yaml`:

```yaml
weight:
  waveshare_path: "/opt/High-Pricision_AD_HAT/python"
```

## 3) Configure

Edit `/opt/host-monitor/config.yaml`.

Important fields:

- `send.url`: telemetry API URL
- `events.url`: SMS/call events API URL
- `send.interval_s`: telemetry period
- `gps.port`: fixed GPS serial port (`null` for auto-detect)
- `gps.port_candidates`: candidate GPS ports
- `lte.at_ports`: candidate AT ports
- `weight.enabled`: enable or disable weight reading
- `weight.simulate`: use a fake weight value instead of the ADC
- `weight.ref_pos` / `weight.ref_neg`: ADC reference sense pair
- `weight.channel_pos` / `weight.channel_neg`: ADC measurement pair

Telemetry health flags:

- `gps_valid`: whether the current GPS fix is valid
- `weight_valid`: whether the current weight sample is valid
- `events_reader_ok`: whether the modem events reader is currently healthy

Default load-cell wiring in config:

```yaml
weight:
  ref_pos: 0
  ref_neg: 1
  channel_pos: 2
  channel_neg: 3
```

## 4) Existing load-cell system integration

This project is designed for a passive parallel measurement path on an existing agricultural machine.

- The factory weighing terminal stays connected and keeps exciting the bridge
- This project does not replace the factory terminal
- This project does not power the bridge from the ADS1263 board
- The ADS1263 only senses the existing excitation and the bridge output

Hardware assumptions:

- The machine already has 3 load cells connected through a summing/junction box
- We tap the cable after the summing box, in parallel with the factory weighing terminal
- Available lines are `E+`, `E-`, `SIG+`, `SIG-`, and `shield/drain`
- The bridge excitation is expected to come from the factory terminal

Electrical connection from the summing box output to the ADS1263 HAT:

- `E+` -> `IN0`
- `E-` -> `IN1`
- `SIG+` -> `IN2`
- `SIG-` -> `IN3`
- `shield/drain` -> keep it as shield continuity; do not use it as a signal conductor

ADC behavior in this project:

- Differential measurement input is `IN2 - IN3`
- External differential reference is `IN0 - IN1`
- The ADC treats `E+ / E-` only as reference sense
- The ADC does not drive `E+ / E-`
- This is not an HX711-style bridge-powering setup

Grounding note:

- Do not use shield as `SIG-` or `E-`
- Connect shield to your local ground only if that matches the machine grounding scheme and does not create a ground loop

## 5) Run manually

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor --config ./config.yaml
```

Without activating the environment:

```bash
/opt/host-monitor/.venv/bin/host-monitor --config ./config.yaml
```

## 6) Run as a systemd service

```bash
sudo cp /opt/host-monitor/systemd/host-monitor.service /etc/systemd/system/host-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now host-monitor
sudo systemctl status host-monitor
```

Restart and stop:

```bash
sudo systemctl restart host-monitor
sudo systemctl stop host-monitor
```

## 7) Logs

Service logs:

```bash
journalctl -u host-monitor -f
```

File logs:

```bash
tail -f /opt/host-monitor/logs/host_monitor.log
```

## 8) Buffer inspection and cleanup

Install `sqlite3` if it is missing:

```bash
sudo apt install -y sqlite3
```

Show how many buffered telemetry/event rows are currently stored:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select 'telemetry' as table_name, count(*) as rows from telemetry union all select 'events', count(*) from events;"
```

Show the latest buffered telemetry rows:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from telemetry order by id desc limit 10;"
```

Show the latest buffered modem event rows:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from events order by id desc limit 10;"
```

Clear only buffered telemetry rows:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "delete from telemetry;"
```

Clear only buffered modem event rows:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "delete from events;"
```

Clear the whole buffer database:

```bash
cd /opt/host-monitor
rm -f ./data/buffer.sqlite3 ./data/buffer.sqlite3-shm ./data/buffer.sqlite3-wal
```

## 9) Calibration workflow

Before calibration:

- `weight.enabled: true`
- `weight.simulate: false`
- The bridge tap is wired correctly
- The factory weighing terminal is powered and exciting the bridge

Commands:

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor-calibrate --config ./config.yaml tare
host-monitor-calibrate --config ./config.yaml calibrate --known-kg 100
```

Calibration is stored in:

- `weight.calibration_path` (default `./data/scale_calibration.json`)

## 10) If the load-cell tap is not connected yet

Set in config:

```yaml
weight:
  enabled: false
```

The service will still run normally. The telemetry packet will keep sending `weight: 0.0` and `weight_valid: false`.
