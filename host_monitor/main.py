from __future__ import annotations

import logging
import time

from host_monitor.buffer import SqliteQueue
from host_monitor.config import ensure_dirs, load_config, parse_args
from host_monitor.gps_reader import GpsCfg as GpsCfgDC
from host_monitor.gps_reader import GpsReader
from host_monitor.log_setup import setup_logging
from host_monitor.lte_info import LteCfg as LteCfgDC
from host_monitor.lte_info import get_lte_info
from host_monitor.modem_events import ModemEventsCfg, ModemEventsReader
from host_monitor.sender import Sender
from host_monitor.system_info import read_cpu_temp_c
from host_monitor.telemetry_builder import build_telemetry
from host_monitor.weight_reader import WeightCfg as WeightCfgDC
from host_monitor.weight_reader import WeightReader
from host_monitor.wifi_clients import WifiCfg as WifiCfgDC
from host_monitor.wifi_clients import get_wifi_clients
from host_monitor.models import LteInfo


log = logging.getLogger("host_monitor")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    setup_logging(cfg.logging)

    telemetry_q = SqliteQueue(sqlite_path=cfg.buffer.sqlite_path, table="telemetry", max_rows=cfg.buffer.max_rows)
    events_q = SqliteQueue(sqlite_path=cfg.buffer.sqlite_path, table="events", max_rows=cfg.buffer.max_rows_events)

    gps = GpsReader(
        GpsCfgDC(
            enabled=cfg.gps.enabled,
            port=cfg.gps.port,
            port_candidates=cfg.gps.port_candidates,
            baud=cfg.gps.baud,
            baud_candidates=cfg.gps.baud_candidates,
        )
    )
    gps.start()

    weight = WeightReader(
        WeightCfgDC(
            enabled=cfg.weight.enabled,
            driver=cfg.weight.driver,
            calibration_path=cfg.weight.calibration_path,
            simulate=cfg.weight.simulate,
            waveshare_path=cfg.weight.waveshare_path,
            ref_pos=cfg.weight.ref_pos,
            ref_neg=cfg.weight.ref_neg,
            channel_pos=cfg.weight.channel_pos,
            channel_neg=cfg.weight.channel_neg,
            sample_count=cfg.weight.sample_count,
            adc_rate=cfg.weight.adc_rate,
            trim_fraction=cfg.weight.trim_fraction,
            min_ref_abs=cfg.weight.min_ref_abs,
        )
    )

    sender = Sender(cfg.send.url, timeout_s=cfg.send.timeout_s)
    events_sender = Sender(cfg.events.url, timeout_s=cfg.events.timeout_s)

    events_reader = ModemEventsReader(
        ModemEventsCfg(
            enabled=cfg.lte.events_enabled,
            port=cfg.lte.events_port,
            candidate_ports=cfg.lte.at_ports,
            baud=cfg.lte.at_baud,
            sms_poll_interval_s=cfg.lte.sms_poll_interval_s,
        )
    )
    events_reader.start()

    seq = 0
    last_log = 0.0
    last_telemetry_flush = 0.0
    last_events_flush = 0.0
    telemetry_flush_backoff_s = 1.0
    events_flush_backoff_s = 1.0

    log.info("started device_id=%s url=%s interval_s=%s", cfg.device.id, cfg.send.url, cfg.send.interval_s)

    try:
        while True:
            t0 = time.time()
            seq += 1

            # Collect
            pos = gps.latest()
            w = weight.read_weight()
            wifi_clients, wifi_err = get_wifi_clients(
                WifiCfgDC(enabled=cfg.wifi.enabled, hostapd_cli=cfg.wifi.hostapd_cli, ap_interface=cfg.wifi.ap_interface)
            )
            if cfg.lte.events_enabled:
                # LTE metrics come from the AT events reader (prevents AT port contention).
                lte = LteInfo()
                snap = events_reader.lte_snapshot()
                if snap.get("rssi_dbm") is not None:
                    lte.rssi_dbm = int(snap["rssi_dbm"])
                if snap.get("access_tech"):
                    lte.access_tech = str(snap["access_tech"])
            else:
                lte = get_lte_info(
                    LteCfgDC(enabled=cfg.lte.enabled, mmcli=cfg.lte.mmcli, at_ports=cfg.lte.at_ports, at_baud=cfg.lte.at_baud)
                )
            cpu_temp = read_cpu_temp_c()

            module_status = {
                "gps": gps.status(),
                "telemetry_buffer_rows": telemetry_q.count(),
                "telemetry_buffer_oldest_age_s": telemetry_q.oldest_age_s(),
                "events_buffer_rows": events_q.count(),
                "events_buffer_oldest_age_s": events_q.oldest_age_s(),
                "events_reader": events_reader.status(),
                "flush": {
                    "telemetry_backoff_s": telemetry_flush_backoff_s,
                    "events_backoff_s": events_flush_backoff_s,
                },
            }
            if wifi_err:
                module_status["wifi_scan_error"] = wifi_err

            telemetry = build_telemetry(
                device_id=cfg.device.id,
                position=pos,
                weight=w,
                wifi_clients=wifi_clients,
                cpu_temp_c=cpu_temp,
                lte=lte,
            )
            payload = telemetry.model_dump(mode="json")

            # Send current payload (if fails -> buffer)
            try:
                sender.send_one(payload)
            except Exception as e:
                module_status["send_error"] = str(e)
                telemetry_q.put(payload)

            # Drain modem events -> send/buffer
            for ev in events_reader.drain(max_items=20):
                ev_payload = {"device_id": cfg.device.id, **ev}
                try:
                    events_sender.send_one(ev_payload)
                except Exception:
                    events_q.put(ev_payload)

            # Flush buffers with backoff and time budget
            now = time.time()
            time_budget_s = 0.2
            budget_end = now + time_budget_s

            if now - last_telemetry_flush >= telemetry_flush_backoff_s and time.time() < budget_end:
                last_telemetry_flush = now
                try:
                    batch = telemetry_q.peek_batch(cfg.send.max_batch)
                    if batch:
                        ids = [rid for rid, _ in batch]
                        sender.send_batch([p for _, p in batch])
                        telemetry_q.delete_ids(ids)
                    telemetry_flush_backoff_s = 1.0
                except Exception as e:
                    module_status["telemetry_flush_error"] = str(e)
                    telemetry_flush_backoff_s = min(telemetry_flush_backoff_s * 2, 60.0)

            if now - last_events_flush >= events_flush_backoff_s and time.time() < budget_end:
                last_events_flush = now
                try:
                    batch = events_q.peek_batch(cfg.events.max_batch)
                    if batch:
                        ids = [rid for rid, _ in batch]
                        events_sender.send_json_string_batch([p for _, p in batch])
                        events_q.delete_ids(ids)
                    events_flush_backoff_s = 1.0
                except Exception as e:
                    module_status["events_flush_error"] = str(e)
                    events_flush_backoff_s = min(events_flush_backoff_s * 2, 60.0)

            # Periodic status log
            if now - last_log >= 10.0:
                last_log = now
                log.info(
                    "seq=%s gps_fix=%s weight=%s wifi=%s lte_rssi=%s tbuf=%s ebuf=%s status=%s",
                    seq,
                    pos.quality,
                    w.weight,
                    len(wifi_clients),
                    lte.rssi_dbm,
                    telemetry_q.count(),
                    events_q.count(),
                    module_status,
                )

            # Sleep to keep interval
            dt = time.time() - t0
            sleep_s = max(0.0, cfg.send.interval_s - dt)
            time.sleep(sleep_s)
    finally:
        try:
            gps.stop()
        except Exception:
            pass
        try:
            sender.close()
        except Exception:
            pass
        try:
            events_reader.stop()
        except Exception:
            pass
        try:
            events_sender.close()
        except Exception:
            pass

