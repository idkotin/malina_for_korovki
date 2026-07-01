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
            frontend=cfg.weight.frontend,
            reference_mode=cfg.weight.reference_mode,
            ref_pos=cfg.weight.ref_pos,
            ref_neg=cfg.weight.ref_neg,
            channel_pos=cfg.weight.channel_pos,
            channel_neg=cfg.weight.channel_neg,
            sample_count=cfg.weight.sample_count,
            adc_rate=cfg.weight.adc_rate,
            adc2_rate=cfg.weight.adc2_rate,
            trim_fraction=cfg.weight.trim_fraction,
            smoothing_alpha=cfg.weight.smoothing_alpha,
            fast_smoothing_alpha=cfg.weight.fast_smoothing_alpha,
            fast_change_threshold_kg=cfg.weight.fast_change_threshold_kg,
            zero_deadband_kg=cfg.weight.zero_deadband_kg,
            median_window=cfg.weight.median_window,
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
    last_telemetry_send = 0.0
    last_confirmed_movement = time.time()
    movement_candidate_since: float | None = None
    sleep_active = False

    log.info(
        "started device_id=%s url=%s interval_s=%s idle_sleep_enabled=%s idle_after_s=%s idle_interval_s=%s",
        cfg.device.id,
        cfg.send.url,
        cfg.send.interval_s,
        cfg.send.idle_sleep_enabled,
        cfg.send.idle_after_s,
        cfg.send.idle_interval_s,
    )

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
            gps_valid = pos.lat is not None and pos.lon is not None and (pos.quality or 0) > 0
            weight_valid = w.weight is not None
            events_status = events_reader.status()
            events_reader_ok = (not cfg.lte.events_enabled) or (
                bool(events_status.get("running")) and not events_status.get("last_error")
            )

            module_status = {
                "gps": gps.status(),
                "telemetry_buffer_rows": telemetry_q.count(),
                "telemetry_buffer_oldest_age_s": telemetry_q.oldest_age_s(),
                "events_buffer_rows": events_q.count(),
                "events_buffer_oldest_age_s": events_q.oldest_age_s(),
                "events_reader": events_status,
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
                gps_valid=gps_valid,
                weight_valid=weight_valid,
                events_reader_ok=events_reader_ok,
            )
            payload = telemetry.model_dump(mode="json")

            now = time.time()
            movement_speed_threshold = max(0.0, float(cfg.send.movement_speed_kmh))
            movement_confirm_s = max(0.0, float(cfg.send.movement_confirm_s))
            idle_after_s = max(0.0, float(cfg.send.idle_after_s))
            idle_interval_s = max(float(cfg.send.interval_s), float(cfg.send.idle_interval_s))
            speed_kmh = float(payload.get("speed_kmh") or 0.0)
            movement_sample = bool(gps_valid and speed_kmh >= movement_speed_threshold)

            if cfg.send.idle_sleep_enabled:
                if movement_sample:
                    if movement_candidate_since is None:
                        movement_candidate_since = now
                    if now - movement_candidate_since >= movement_confirm_s:
                        last_confirmed_movement = now
                else:
                    movement_candidate_since = None
                sleep_active = now - last_confirmed_movement >= idle_after_s
            else:
                movement_candidate_since = None
                sleep_active = False
                last_confirmed_movement = now

            current_send_interval_s = idle_interval_s if sleep_active else float(cfg.send.interval_s)
            should_send_telemetry = (
                last_telemetry_send <= 0.0 or now - last_telemetry_send >= current_send_interval_s
            )
            module_status["telemetry_send"] = {
                "sleep_active": sleep_active,
                "current_interval_s": current_send_interval_s,
                "idle_after_s": idle_after_s,
                "idle_for_s": now - last_confirmed_movement,
                "movement_candidate_s": (now - movement_candidate_since) if movement_candidate_since else None,
                "movement_speed_threshold_kmh": movement_speed_threshold,
                "current_speed_kmh": speed_kmh,
            }

            # Send current payload (if fails -> buffer)
            if should_send_telemetry:
                try:
                    sender.send_one(payload)
                except Exception as e:
                    module_status["send_error"] = str(e)
                    telemetry_q.put(payload)
                finally:
                    last_telemetry_send = now
            else:
                module_status["telemetry_send"]["skipped_current"] = True

            # Drain modem events -> send/buffer
            for ev in events_reader.drain(max_items=20):
                ev_payload = {"device_id": cfg.device.id, **ev}
                if ev_payload.get("type") == "sms":
                    log.info(
                        "SMS event ready: from=%s text_len=%s",
                        ev_payload.get("from"),
                        len(str(ev_payload.get("text", ""))),
                    )
                try:
                    events_sender.send_one(ev_payload)
                    if ev_payload.get("type") == "sms":
                        log.info("SMS event sent: text_len=%s", len(str(ev_payload.get("text", ""))))
                except Exception:
                    events_q.put(ev_payload)
                    if ev_payload.get("type") == "sms":
                        log.info("SMS event buffered: text_len=%s", len(str(ev_payload.get("text", ""))))

            # Flush buffers with backoff and time budget
            now = time.time()
            time_budget_s = 0.2
            budget_end = now + time_budget_s

            if now - last_telemetry_flush >= telemetry_flush_backoff_s and time.time() < budget_end:
                last_telemetry_flush = now
                try:
                    batch = telemetry_q.peek_batch(cfg.send.max_batch)
                    if batch:
                        for rid, payload_json in batch:
                            sender.send_buffered_telemetry_one(payload_json)
                            telemetry_q.delete_ids([rid])
                    telemetry_flush_backoff_s = 1.0
                except Exception as e:
                    module_status["telemetry_flush_error"] = str(e)
                    telemetry_flush_backoff_s = min(telemetry_flush_backoff_s * 2, 60.0)

            if now - last_events_flush >= events_flush_backoff_s and time.time() < budget_end:
                last_events_flush = now
                try:
                    batch = events_q.peek_batch(cfg.events.max_batch)
                    if batch:
                        for rid, payload_json in batch:
                            events_sender.send_json_string_one(payload_json)
                            events_q.delete_ids([rid])
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

