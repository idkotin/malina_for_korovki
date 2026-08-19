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
- Public/default config: `config.yaml`
- Production config (outside Git): `/etc/host-monitor/config.yaml`
- systemd unit: `systemd/host-monitor.service`
- Remote-access scripts: `remote_access/amnezia/`

Runtime target is Raspberry Pi OS, normally installed at:

```text
/opt/host-monitor
```

The systemd service runs:

```bash
/opt/host-monitor/.venv/bin/host-monitor --config /etc/host-monitor/config.yaml
```

## Raspberry Pi Update Procedure

Never edit the tracked `/opt/host-monitor/config.yaml` on a production Pi and
never hide it with `assume-unchanged` or `skip-worktree`. The live settings are
kept at `/etc/host-monitor/config.yaml`, outside the repository, so source
updates cannot overwrite or conflict with them.

Normal updates are performed only with:

```bash
cd /opt/host-monitor
./update-device.sh
```

The updater accepts untracked backup/build files, but refuses to overwrite
tracked source changes. It pulls `master` with `--ff-only`, refreshes the
editable Python installation, installs the current systemd unit, preserves the
external live config, and restarts the service.

For the one-time migration from an older checkout, first run
`sudo bash ./systemd/install-host-monitor.sh` while the known-good live
`config.yaml` is still present. After verifying `/etc/host-monitor/config.yaml`,
the tracked checkout copy may be restored with `git restore config.yaml`.

## What The Device Does

The Raspberry Pi "host" gathers and sends machine telemetry:

- GPS from the HGLRC M100 Mini direct UART NMEA stream (`/dev/serial0`).
- LTE RSSI/access technology from the modem AT/events reader.
- SMS and call events from the modem AT port.
- Weight from a Waveshare ADS1263 HAT, optionally disabled or simulated.
- Wi-Fi/AP client MAC addresses via `hostapd_cli` or `ip neigh`.
- CPU temperature.

Normal telemetry and modem events go to separate HTTP endpoints configured in
the live `/etc/host-monitor/config.yaml`:

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
- `gps_valid`, `gps_satellites`, `gps_quality`, `gps_age_s`
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
- The same 30-second poll reads SIM7600 module temperature (`AT+CPMUTEMP`) and
  voltage (`AT+CBC`) and writes a `modem health:` journal line. Never probe
  these commands from a second process while `host-monitor` owns the AT port.
- Optional SIM/UIM recovery uses that same reader. It resets the modem with
  `AT+CFUN=1,1` only after a sustained explicit `AT+CPIN? -> SIM failure` and is
  guarded by a cooldown and rolling reset limit.
- Never treat a generic SMS `ERROR` as full storage. `AT+CMGD=1,4` is allowed
  only for an explicit storage-full indication.

Events have this shape before `main.py` adds `device_id`:

```json
{"type":"sms","timestamp":"...","from":"+7999...","text":"..."}
{"type":"call","timestamp":"...","from":"+7999...","text":""}
```

## GPS

`host_monitor/gps_reader.py` runs in a daemon thread. The current production
hardware is an HGLRC M100 Mini on the Raspberry Pi GPIO UART at 115200 baud.
When `gps.port` is fixed, the reader must not fall back to unrelated ttyUSB
devices. Direct UART deployments may disable absolute NMEA/system-clock
comparison so GPS remains usable before NTP sync; monotonic arrival freshness
must remain enforced.

The M100 can interleave NMEA and binary UBX on the same saturated UART. The
reader must frame both protocols and accept checksum-valid `UBX-NAV-PVT` as a
first-class position source alongside GGA/RMC/GNS. Never treat arbitrary `$`
bytes inside UBX payloads as NMEA, and never publish a NAV-PVT position unless
`gnssFixOK`, a 2D-or-better `fixType`, valid coordinate ranges, and the UBX
checksum all pass.

It auto-detects candidate `/dev/ttyUSB*` ports and baud rates by looking for
NMEA. It parses:

- `GGA`: coordinates, quality, satellites.
- `RMC`: coordinates, fix status, speed.
- `GNS`: coordinates, mode, satellites.

The thread keeps the newest parsed position and merges supplemental fields from
different sentence types.

GPS fixes are aged with `time.monotonic()`. A fix older than
`gps.max_fix_age_s` is emitted with `gps_valid=false` and zero coordinates;
serial failures immediately discard the previous fix. NMEA checksums are
validated when present, and serial auto-detection accepts only parseable
GGA/RMC/GNS sentences. GGA/GNS time-of-day and RMC date/time are also compared
with the NTP-synchronized system clock. An old NMEA epoch invalidates the fix
and clears the serial input buffer, so a tty backlog cannot masquerade as a
fresh coordinate. RMC speed expires independently when RMC updates stop.

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

## Durable telemetry outbox (2026-07-17)

- `send.interval_s` controls fresh packet creation and the immediate send attempt. A request never waits for `send.max_batch` rows.
- Every telemetry packet is committed to the existing SQLite queue with `synchronous=FULL`; its row ID is the persistent `packet_id`, and `queue_metadata` stores one durable `stream_id`.
- `TelemetryOutboxWorker` is the only telemetry HTTP sender. It sends the fresh row first plus the oldest backlog rows, up to 20 by default, and deletes only server-confirmed `acked_packet_ids`.
- The modem events dispatcher/flusher remains separate. Existing telemetry rows require no data rewrite and receive the same stream identity after upgrade.
- Local checks: compile and the full unittest suite must pass with the bundled
  Python runtime. Hardware/systemd validation must still be performed on the
  Raspberry Pi.

## SIM7600 incident and guarded recovery (2026-07-19/20)

Read `INCIDENT_2026-07-19_20.md` before changing GPS, PPP, buffering, remote
access, reboot logic or production replay behavior.

- The confirmed 07:34 outage removed the complete SIM7600 USB composite device
  and all `ttyUSB` ports. It was not caused by the GPS parser, server SQLite,
  Amnezia or the durable outbox.
- About 42,000 locally buffered packets were accepted after restart. The second
  abrupt outage occurred after the local buffer had drained, so replay traffic
  was not the trigger.
- `lte.service` with `pppd call megafon` is the only production PPP owner. The
  obsolete `sim7600-ppp.service` is disabled. Never call its generic `poff`
  stop action: it SIGHUPs the working PPP session too.
- A production server replay from 14:49:32 to 14:52:44 made the website appear
  stale while the Pi received HTTP 202 every two seconds. Do not infer device
  inactivity from the current map; use host ACK age.
- Manual `/reboot` SMS is allow-listed by the live config only.
- Optional automatic reboot requires both 900 seconds without an acknowledged
  telemetry batch and a fresh `Weight.raw < -1000 kg` for 30 seconds. Missing
  or stale weight blocks reboot. A persistent latch prevents a reboot loop and
  clears only after 60 seconds of sustained ACK recovery.
- The auto-reboot policy must stay in the host process. Moving it to an external
  watcher that uses a stale last-known weight could reboot while the factory
  terminal is currently on, which is explicitly forbidden.

Implementation map:

- `TelemetryOutboxWorker.status().last_success_age_s` is the only inactivity
  clock. Website visibility, Amnezia reachability and server replay state are
  not reboot signals.
- `RecoveryWatchdog` in `host_monitor/recovery_watchdog.py` applies the two
  guards and persists its latch before calling `request_system_reboot()`.
- `main.py` passes `Weight.raw` plus the sampler age. Never replace it with the
  filtered `Weight.weight`, because values below the validity threshold become
  `None` there.
- Public `config.yaml` keeps both SMS and automatic reboot disabled. Phone
  numbers and production enablement belong only in the live Pi config.
- The 2026-08-09 outage left the USB composite device and GNSS alive but put the
  SIM/UIM into an error state. A modem-only `AT+CFUN=1,1` restored it. Public
  config also keeps this modem-only workaround disabled until deliberately
  enabled on the live Pi.
- The 2026-08-10 outage repeatedly removed the SIM7600 USB composite device.
  The persistent `pppd` process kept the disconnected `/dev/ttyUSB3` minor
  reserved, so USB interface `03` returned under a different `ttyUSBN` name and
  `lte.service` stayed falsely active without `ppp0`. Production PPP must use
  the verified udev alias `/dev/simcom-ppp`; install it with
  `systemd/install-simcom-ppp.sh`. Its optional timer restarts `lte.service`
  after two minutes without `ppp0` when the alias exists. Production hardware
  testing on 2026-08-11 also validated a full external-USB power cycle on the
  Raspberry Pi 4 onboard hub. That second stage must remain opt-in, wait five
  minutes, validate the Pi model and USB inventory, and keep a 30-minute
  cooldown. It must never reboot the Pi and must refuse to cycle USB while an
  unexpected USB device is connected.
- Install `systemd/install-simcom-diagnostics.sh` when persistent field evidence
  is required. It bounds persistent journald storage, records a compact
  USB/PPP/power snapshot every minute, logs PPP up/down hooks, and stores the
  newest 30 compressed incident bundles. Diagnostic probes must never open an
  AT, GPS, or QMI port because `host-monitor` and PPP already own them.
