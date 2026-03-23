# host-monitor (Raspberry Pi)

Telemetry client for Raspberry Pi + SIM7600 + ADS1263.

- Sends telemetry via HTTP POST (JSON) at configured frequency
- Buffers unsent telemetry/events in SQLite (FIFO) during network outages
- Reads GPS from modem serial ports with auto-detect
- Reads SMS/call events from AT port and sends to separate API
- Supports ADS1263 load-cell path with calibration file

## 1) Telemetry JSON format

Current telemetry payload is flat and clean:

```json
{
  "device_id": "isrk_hozyain_01",
  "timestamp": "2026-03-11T20:34:38",
  "lat": 55.109311,
  "lon": 82.812417,
  "weight": 1234.56,
  "gps_quality": 1,
  "wifi_clients": ["aa:bb:cc:dd:ee:ff"],
  "cpu_temp_c": 61.2,
  "lte_rssi_dbm": -75,
  "lte_access_tech": "LTE"
}
```

SMS/call events use separate endpoint (`events.url`):

```json
{
  "device_id": "isrk_hozyain_01",
  "type": "sms",
  "timestamp": "2026-03-11T20:35:01",
  "from": "+79991234567",
  "text": "hello"
}
```

Call event example (same schema, empty `text`):

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

Enable SPI (required for ADS1263):

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable
sudo reboot
```

Clone project and install:

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone https://github.com/idkotin/malina_for_korovki.git host-monitor
sudo chown -R $USER:$USER /opt/host-monitor
cd /opt/host-monitor
python3 -m pip install -U pip
python3 -m pip install .
```

Install Waveshare ADS1263 python library:

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
- `send.interval_s`: telemetry period (0.5 means 2 packets per second)
- `gps.port`: fixed GPS ttyUSB port (`null` for auto-detect)
- `gps.port_candidates`: all potential GPS ports
- `lte.at_ports`: all potential AT ports
- `weight.enabled`: enable/disable load-cell processing
- `weight.simulate`: if true, use generated fake value

## 4) Run manually

```bash
cd /opt/host-monitor
host-monitor --config ./config.yaml
```

## 5) Run as systemd service

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

## 6) Logs

Service logs (journald):

```bash
journalctl -u host-monitor -f
```

File logs:

```bash
tail -f /opt/host-monitor/logs/host_monitor.log
```

## 7) ADS1263 wiring (from combiner to AD HAT)

On the AD HAT screw terminals you have: `IN0..IN9`, `AVSS`, `AVDD`, `GND`.

Recommended default (matches config `channel_pos: 0`, `channel_neg: 1`):

- `Combiner SIG+` -> `IN0`
- `Combiner SIG-` -> `IN1`
- `Combiner EXC+ (E+)` -> `AVDD`
- `Combiner EXC- (E-)` -> `AVSS`
- Shield/ground (if present) -> `GND`

If your combiner labels differ, map by function: `signal +/-` to `INx`, `excitation +/-` to `AVDD/AVSS`.

## 8) Calibration workflow (zero + known weight)

Before calibration:

- `weight.enabled: true`
- `weight.simulate: false`
- Correct wiring is connected

Commands:

```bash
cd /opt/host-monitor
host-monitor-calibrate --config ./config.yaml tare
host-monitor-calibrate --config ./config.yaml calibrate --known-kg 100
```

Calibration is saved in:

- `weight.calibration_path` (default `./data/scale_calibration.json`)

## 9) If no combiner yet

Set in config:

```yaml
weight:
  enabled: false
```

Service runs normally, `weight` field will be `null`, no crash.

