# Host Monitor Agent Notes

This repository contains the Raspberry Pi "host" device code for the Korovki
system. The public upstream is:

```text
https://github.com/idkotin/malina_for_korovki/tree/master
```

The local working copy is the source of truth for edits:

```text
C:\Users\Windows\projects\Korovki
```

## Project Shape

- Python package: `host_monitor`
- CLI entrypoint: `host-monitor = host_monitor.main:main`
- Calibration CLI: `host-monitor-calibrate = host_monitor.calibrate:main`
- Buffered events inspection CLI: `host-monitor-events = host_monitor.events_dump:main`
- Main config: `config.yaml`
- systemd unit: `systemd/host-monitor.service`
- Remote-access scripts: `remote_access/amnezia/`

Runtime target is Raspberry Pi OS, normally installed at:

```text
/opt/host-monitor
```

The systemd service runs:

```bash
/opt/host-monitor/.venv/bin/host-monitor --config /opt/host-monitor/config.yaml
```

## What The Device Does

The Raspberry Pi "host" gathers and sends machine telemetry:

- GPS from SIM7600 serial NMEA ports.
- LTE RSSI/access technology from the modem AT/events reader.
- SMS and call events from the modem AT port.
- Weight from a Waveshare ADS1263 HAT, optionally disabled or simulated.
- Wi-Fi/AP client MAC addresses via `hostapd_cli` or `ip neigh`.
- CPU temperature.

Normal telemetry and modem events go to separate HTTP endpoints configured in
`config.yaml`:

```yaml
send:
  url: "http://127.0.0.1:8000/telemetry"
events:
  url: "http://127.0.0.1:8000/modem-events"
```

The current local device id is:

```text
ISRK_Hozain_rasp
```

## Main Runtime Flow

`host_monitor/main.py` schedules fresh telemetry while blocking device and network work runs in `host_monitor/workers.py`:

1. Load config and create required folders.
2. Create two SQLite queues:
   - `telemetry` table for normal telemetry.
   - `events` table for SMS/call events.
3. Start the GPS reader thread.
4. Create the weight reader.
5. Create HTTP senders for telemetry and events.
6. Start the modem events reader thread.
7. Start independent workers for continuous weight sampling, Wi-Fi polling,
   fresh HTTP dispatch, and SQLite backlog flushing.
8. On each loop tick, collect snapshots from GPS, weight and Wi-Fi without
   waiting for hardware or network I/O, then add LTE, CPU and reader health.
9. Build a `Telemetry` model and submit it to the non-blocking dispatcher.
10. Drain modem events into their independent dispatcher.
11. Log worker and buffer status every 10 seconds.

The loop sleeps against `send.interval_s`. Never move ADS reads, Wi-Fi shell
commands, HTTP requests, or backlog replay back into this scheduler.

## Telemetry Payload

Telemetry is built in `host_monitor/telemetry_builder.py` and typed by
`host_monitor/models.py`.

Current fields:

- `device_id`
- `timestamp`
- `lat`, `lon`
- `gps_valid`, `gps_satellites`, `gps_quality`
- `speed_kmh`
- `weight`
- `raw`
- `weight_valid`
- `wifi_clients`
- `cpu_temp_c`
- `lte_rssi_dbm`
- `lte_access_tech`
- `events_reader_ok`

The backend currently expects numeric fields to be numbers, not `null`. The
builder converts missing values to `0`, `0.0`, `false`, empty list, or `"0"`.
`host_monitor/sender.py` also sanitizes older buffered telemetry before resend.

Do not rename fields without coordinating with the server API.

## Sending And Buffering

HTTP sending is in `host_monitor/sender.py` and uses a synchronous
`httpx.Client`.

Important behavior:

- `send_one(payload)` sends a dict as JSON and raises on non-2xx responses.
- `send_json_string_one(payload_json)` sends already serialized JSON.
- `send_buffered_telemetry_one(payload_json)` loads JSON, sanitizes legacy nulls,
  and sends as normal JSON.
- Batch helpers currently loop over individual POSTs; there is no true batch API
  call yet.

Buffering is in `host_monitor/buffer.py`.

The queue is SQLite-backed:

```yaml
buffer:
  sqlite_path: "./data/buffer.sqlite3"
  max_rows: 200000
  max_rows_events: 50000
```

`SqliteQueue` stores rows as:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
created_utc TEXT NOT NULL,
payload_json TEXT NOT NULL
```

It enables:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

Queue behavior:

- `put()` serializes compact JSON with `ensure_ascii=False`.
- If row count exceeds `max_rows`, oldest rows are deleted.
- `peek_batch(limit)` reads oldest rows first: `ORDER BY id ASC`.
- `delete_ids(ids)` removes rows only after a successful POST.
- `oldest_age_s()` is used for health/status logging.

Flush behavior in `main.py`:

- Telemetry and events have separate backoff timers.
- Backoff starts at `1.0s` and doubles up to `60.0s` on failure.
- Backoff resets to `1.0s` after a successful flush attempt.
- Flush is given a `0.2s` time budget per main loop.
- Each row is deleted immediately after its individual send succeeds.

This means the buffer is durable and FIFO, but a large backlog may drain slowly
because sending is deliberately time-budgeted.

## Idle Sleep

`send.idle_sleep_enabled` slows only regular telemetry sends. It does not stop:

- GPS reading.
- Weight reading.
- Modem event reading.
- Buffer flushing.
- Status logging.

The idle state is based on GPS speed:

- `movement_speed_kmh`: speed threshold for movement.
- `movement_confirm_s`: movement must persist this long to wake normal sending.
- `idle_after_s`: no confirmed movement this long activates sleep mode.
- `idle_interval_s`: telemetry interval while sleeping.

Keep this logic conservative. Short GPS speed spikes should not immediately wake
normal telemetry.

## Modem Events

`host_monitor/modem_events.py` reads SMS and calls in a daemon thread.

Important details:

- It probes candidate AT ports and accepts only a port that answers `AT` with
  `OK`.
- It uses SMS PDU mode (`AT+CMGF=0`) so long multipart SMS can be assembled.
- It polls unread SMS by default and can also handle `+CMTI` when polling is
  disabled.
- It emits event dicts into an internal queue, which `main.py` drains and sends.
- LTE RSSI/access snapshot is read through the same AT reader to avoid AT port
  contention.

Events have this shape before `main.py` adds `device_id`:

```json
{"type":"sms","timestamp":"...","from":"+7999...","text":"..."}
{"type":"call","timestamp":"...","from":"+7999...","text":""}
```

## GPS

`host_monitor/gps_reader.py` runs in a daemon thread.

It auto-detects candidate `/dev/ttyUSB*` ports and baud rates by looking for
NMEA. It parses:

- `GGA`: coordinates, quality, satellites.
- `RMC`: coordinates, fix status, speed.
- `GNS`: coordinates, mode, satellites.

The thread keeps the newest parsed position and merges supplemental fields from
different sentence types.

## Weight

`host_monitor/weight_reader.py` supports ADS1263 and simulation.

Current local config has:

```yaml
weight:
  enabled: false
  driver: "ads1263"
  simulate: false
  frontend: "adc2"
  reference_mode: "internal"
```

Recommended hardware mode is passive parallel sniffing of the existing weighing
terminal, using ADC2 with internal reference. When weight is disabled or reading
fails, telemetry still sends `weight: 0.0` and `weight_valid: false`.

Be careful changing calibration, filtering, ADC frontend, or reference mode:
wrong settings can make field diagnostics very confusing.

## Wi-Fi Clients

`host_monitor/wifi_clients.py` gets client MACs from:

1. `hostapd_cli -i <ap_interface> all_sta`
2. Fallback: `ip neigh show dev <ap_interface>`

The configured AP interface is usually `wlan0`.

## Development Commands

Install locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

Run:

```bash
python -m host_monitor.main --config ./config.yaml
```

Compile-check all Python files:

```bash
python -m compileall host_monitor
```

Inspect SQLite buffer:

```bash
sqlite3 ./data/buffer.sqlite3 "select 'telemetry', count(*) from telemetry union all select 'events', count(*) from events;"
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from telemetry order by id desc limit 10;"
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from events order by id desc limit 10;"
```

## Editing Guidance

- Keep runtime changes compatible with Raspberry Pi OS and Python 3.10+.
- Prefer config changes in `config.yaml`/`host_monitor/config.py` over hardcoded
  behavior.
- Keep the main loop responsive; avoid long blocking calls there.
- Use threads for continuous serial readers, following the GPS/modem pattern.
- Do not let AT-port code fight over the same modem port. The events reader is
  intentionally reused for LTE metrics when events are enabled.
- Preserve the SQLite queue's durable FIFO semantics unless the user explicitly
  wants different resend behavior.
- If changing payload shape, update `models.py`, `telemetry_builder.py`,
  `sender.py` sanitizer if needed, README examples, and server expectations.
- If changing buffer schema, handle migration from existing
  `./data/buffer.sqlite3`.
- Do not commit local data/logs/secrets generated on a Raspberry Pi.

## Quick Checks After Changes

At minimum:

- Run `python -m compileall host_monitor`.
- If touching config models, load `config.yaml`.
- If touching payload fields, build a sample telemetry payload.
- If touching buffer code, test `put()`, `peek_batch()`, `delete_ids()`, and
  trimming behavior.
- If touching modem code, test on hardware or clearly state that hardware
  validation was not possible.
