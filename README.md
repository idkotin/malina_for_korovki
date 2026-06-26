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
  "speed_kmh": 18.52,
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
- `speed_kmh`: GPS speed over ground in km/h, parsed from NMEA RMC; `0.0` when there is no valid fix
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

Long SMS note:

- The modem reader uses SMS PDU mode and assembles multipart SMS before buffering or sending
- This matters for Russian UCS2 messages, which are often split by the operator into several parts
- Logs include `SMS multipart assembled` and `SMS event sent: text_len=...` for verification

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
sudo apt install -y python3 python3-pip python3-venv git hostapd modemmanager sqlite3
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
- `weight.frontend`: `adc2` for passive parallel sniffing, `adc1` for legacy direct path
- `weight.reference_mode`: `internal` for passive parallel mode, `avdd` for standalone bridge power mode

Telemetry health flags:

- `gps_valid`: whether the current GPS fix is valid
- `weight_valid`: whether the current weight sample is valid
- `events_reader_ok`: whether the modem events reader is currently healthy

Default load-cell wiring in config:

```yaml
weight:
  frontend: "adc2"
  reference_mode: "internal"
  channel_pos: 0
  channel_neg: 1
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

Electrical connection from the summing box output to the ADS1263 HAT in the recommended passive mode:

- `SIG+` -> `IN0`
- `SIG-` -> `IN1`
- `E-` -> `AVSS/GND`
- `E+` -> do not connect to `AVDD` in passive mode
- `shield/drain` -> keep it as shield continuity; do not use it as a signal conductor

ADC behavior in this project:

- Passive parallel default: `adc2` frontend with internal reference, differential input `IN0 - IN1`
- The factory terminal keeps exciting the bridge
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

## 8) Remote access

Remote access files live in [`remote_access/amnezia`](./remote_access/amnezia). Do not commit real server IPs, passwords, private keys, exported Amnezia configs, or local `known_hosts` files. The repo `.gitignore` already excludes the common secret files in that folder.

### Method A: direct AmneziaWG access

In AmneziaVPN on the admin laptop:

- Add or open the self-hosted server.
- Use the `AmneziaWG` protocol and set the server UDP port to the chosen value, for example `1234`.
- Create a separate access profile for the laptop, for example `admin-laptop`, in the format for the AmneziaVPN app.
- Create a separate access profile for Raspberry Pi, for example `raspberry-pi`, in `AmneziaWG native config` format.
- Save the Raspberry Pi export as `amnezia_for_awg.conf` and copy it to `/opt/host-monitor/remote_access/amnezia/amnezia_for_awg.conf` on the Pi.

Install the AmneziaWG client service on Raspberry Pi:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips <VPN_CIDR> \
  --install-packages
```

`<VPN_CIDR>` must be the internal Amnezia network that should be reachable through VPN, for example `10.8.1.0/24`. Do not use `0.0.0.0/0` unless you intentionally want the Pi default route to go through VPN.

Check the VPN service:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/check_remote_access.sh --interface awg0
sudo systemctl status amneziawg-client@awg0 --no-pager
sudo awg show awg0
ip addr show awg0
```

Connect from the laptop when both devices are connected to AmneziaWG:

```powershell
ping <RPI_VPN_IP>
ssh pi@<RPI_VPN_IP>
```

If VNC is enabled on Raspberry Pi, connect the VNC client to:

```text
<RPI_VPN_IP>:5900
```

### Method B: reverse SSH fallback

Use this only if the laptop and Raspberry Pi are both connected to AmneziaWG, but direct client-to-client access does not work.

Create a tunnel key on Raspberry Pi:

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/korovki_pi_tunnel -N ""
ssh-keyscan -p 22 <SERVER_IP> > ~/.ssh/korovki_server_known_hosts
scp ~/.ssh/korovki_pi_tunnel.pub root@<SERVER_IP>:/tmp/id_ed25519_pi_tunnel.pub
```

Prepare a restricted tunnel user on the server:

```bash
git clone https://github.com/idkotin/malina_for_korovki.git /opt/host-monitor-remote-tools
cd /opt/host-monitor-remote-tools
sudo bash ./remote_access/amnezia/prepare_reverse_ssh_server.sh \
  --public-key-file /tmp/id_ed25519_pi_tunnel.pub \
  --user pi-tunnel \
  --remote-ssh-port 2222 \
  --remote-vnc-port 5901
```

Enable the reverse tunnel service on Raspberry Pi:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_reverse_ssh.sh \
  --server-host <SERVER_IP> \
  --server-user pi-tunnel \
  --identity-file ~/.ssh/korovki_pi_tunnel \
  --known-hosts ~/.ssh/korovki_server_known_hosts \
  --remote-ssh-port 2222 \
  --enable-vnc \
  --remote-vnc-port 5901 \
  --install-packages
```

Check fallback service autostart:

```bash
sudo systemctl status reverse-ssh.service --no-pager
sudo journalctl -u reverse-ssh.service -n 100 --no-pager
```

Connect through the server:

```powershell
ssh -J root@<SERVER_IP> -p 2222 pi@127.0.0.1
```

For VNC through fallback, open a local tunnel from the laptop:

```powershell
ssh -L 5901:127.0.0.1:5901 root@<SERVER_IP>
```

Then connect the VNC client to:

```text
127.0.0.1:5901
```

## 9) Buffer inspection and cleanup

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

That SQLite example intentionally shows only the first 200 characters. To inspect full SMS text without truncation:

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor-events --config ./config.yaml --limit 10
```

To print complete raw JSON for each buffered event:

```bash
host-monitor-events --config ./config.yaml --limit 10 --full-json
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

## 10) Calibration workflow

Before calibration:

- `weight.enabled: true`
- `weight.simulate: false`
- The bridge tap is wired correctly
- The factory weighing terminal is powered and exciting the bridge

Command:

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor-calibrate --config ./config.yaml
```

Workflow:

- In passive `adc2 + internal` mode, start the calibration script first
- Then power on the factory terminal and wait until its own display reaches zero
- Only after that enter the first known weight and capture point 1
- Put the first known load on the machine and wait until the reading stabilizes
- Enter the current known total weight in kg when the script asks for the first point. `0` is allowed for an unloaded first point
- Add or change the load and wait for stabilization again
- Press Enter when the script asks to continue, then enter the new known total weight in kg
- The script computes and saves both `offset` and `scale` from those two measured points

If `config.yaml` is a machine-local live config, keep it out of the normal update flow:

```bash
cd /opt/host-monitor
git update-index --skip-worktree config.yaml
```

To let Git manage it again later:

```bash
cd /opt/host-monitor
git update-index --no-skip-worktree config.yaml
```

If `git pull` is already blocked by a local `config.yaml`, keep your live config and update like this:

```bash
cd /opt/host-monitor
cp config.yaml config.yaml.bak-$(date +%F-%H%M%S)
git stash push -m local-config -- config.yaml
git pull
git stash pop
```

If `stash pop` leaves conflict markers like `<<<<<<<`, restore your known-good local config from backup and resolve the index:

```bash
cd /opt/host-monitor
cp config.yaml.bak-YYYY-MM-DD-HHMMSS config.yaml
git add config.yaml
git restore --staged config.yaml
git update-index --skip-worktree config.yaml
```

Calibration is stored in:

- `weight.calibration_path` (default `./data/scale_calibration.json`)

## 11) If the load-cell tap is not connected yet

Set in config:

```yaml
weight:
  enabled: false
```

The service will still run normally. The telemetry packet will keep sending `weight: 0.0` and `weight_valid: false`.
