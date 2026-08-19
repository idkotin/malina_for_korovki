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
from host_monitor.models import LteInfo
from host_monitor.modem_events import ModemEventsCfg, ModemEventsReader
from host_monitor.recovery_watchdog import RecoveryWatchdog, RecoveryWatchdogCfg
from host_monitor.system_actions import request_system_reboot
from host_monitor.system_info import read_cpu_temp_c
from host_monitor.telemetry_builder import build_telemetry
from host_monitor.weight_reader import WeightCfg as WeightCfgDC
from host_monitor.weight_reader import WeightReader
from host_monitor.wifi_clients import WifiCfg as WifiCfgDC
from host_monitor.wifi_clients import get_wifi_clients
from host_monitor.workers import BufferFlusher, OutboundDispatcher, TelemetryOutboxWorker, WeightSampler, WifiMonitor


log = logging.getLogger("host_monitor")


def _build_weight_reader(cfg) -> WeightReader:
    return WeightReader(
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
            invalid_below_kg=cfg.weight.invalid_below_kg,
            invalid_above_kg=cfg.weight.invalid_above_kg,
        )
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    setup_logging(cfg.logging)

    # Main-thread queue handles overflow and exposes lightweight status. Worker
    # threads open their own SQLite connections to avoid sharing transactions.
    telemetry_q = SqliteQueue(sqlite_path=cfg.buffer.sqlite_path, table="telemetry", max_rows=cfg.buffer.max_rows)
    events_q = SqliteQueue(sqlite_path=cfg.buffer.sqlite_path, table="events", max_rows=cfg.buffer.max_rows_events)

    gps = GpsReader(
        GpsCfgDC(
            enabled=cfg.gps.enabled,
            port=cfg.gps.port,
            port_candidates=cfg.gps.port_candidates,
            baud=cfg.gps.baud,
            baud_candidates=cfg.gps.baud_candidates,
            max_fix_age_s=cfg.gps.max_fix_age_s,
            max_serial_backlog_bytes=cfg.gps.max_serial_backlog_bytes,
            validate_source_time=cfg.gps.validate_source_time,
        )
    )
    gps.start()

    weight_sampler = WeightSampler(_build_weight_reader(cfg))
    weight_sampler.start()

    wifi_cfg = WifiCfgDC(
        enabled=cfg.wifi.enabled,
        hostapd_cli=cfg.wifi.hostapd_cli,
        ap_interface=cfg.wifi.ap_interface,
    )
    wifi_monitor = WifiMonitor(
        lambda: get_wifi_clients(wifi_cfg),
        interval_s=cfg.wifi.scan_interval_s,
        max_snapshot_age_s=cfg.wifi.max_snapshot_age_s,
    )
    wifi_monitor.start()

    events_reader = ModemEventsReader(
        ModemEventsCfg(
            enabled=cfg.lte.events_enabled,
            port=cfg.lte.events_port,
            candidate_ports=cfg.lte.at_ports,
            baud=cfg.lte.at_baud,
            sms_poll_interval_s=cfg.lte.sms_poll_interval_s,
            sms_reboot_enabled=cfg.sms_reboot.enabled,
            sms_reboot_allowed_number=cfg.sms_reboot.allowed_number,
            sms_reboot_command=cfg.sms_reboot.command,
            sim_failure_recovery_enabled=cfg.lte.sim_failure_recovery_enabled,
            sim_failure_poll_interval_s=cfg.lte.sim_failure_poll_interval_s,
            sim_failure_confirm_s=cfg.lte.sim_failure_confirm_s,
            sim_failure_reset_cooldown_s=cfg.lte.sim_failure_reset_cooldown_s,
            sim_failure_reset_window_s=cfg.lte.sim_failure_reset_window_s,
            sim_failure_max_resets=cfg.lte.sim_failure_max_resets,
            sim_failure_reset_settle_s=cfg.lte.sim_failure_reset_settle_s,
        ),
        reboot_action=request_system_reboot,
    )
    events_reader.start()

    recovery_watchdog = RecoveryWatchdog(
        RecoveryWatchdogCfg(
            enabled=cfg.auto_reboot.enabled,
            telemetry_inactive_s=cfg.auto_reboot.telemetry_inactive_s,
            terminal_off_below_raw_kg=cfg.auto_reboot.terminal_off_below_raw_kg,
            terminal_off_confirm_s=cfg.auto_reboot.terminal_off_confirm_s,
            max_weight_age_s=cfg.auto_reboot.max_weight_age_s,
            healthy_success_max_age_s=cfg.auto_reboot.healthy_success_max_age_s,
            healthy_reset_confirm_s=cfg.auto_reboot.healthy_reset_confirm_s,
            state_path=cfg.auto_reboot.state_path,
        ),
        reboot_action=request_system_reboot,
    )

    events_dispatcher = OutboundDispatcher(
        url=cfg.events.url,
        timeout_s=cfg.events.timeout_s,
        sqlite_path=cfg.buffer.sqlite_path,
        table="events",
        max_rows=cfg.buffer.max_rows_events,
    )
    telemetry_sender = TelemetryOutboxWorker(
        device_id=cfg.device.id,
        url=cfg.send.url,
        timeout_s=cfg.send.timeout_s,
        outbox=telemetry_q,
        max_batch=cfg.send.max_batch,
    )
    events_flusher = BufferFlusher(
        url=cfg.events.url,
        timeout_s=cfg.events.timeout_s,
        sqlite_path=cfg.buffer.sqlite_path,
        table="events",
        max_rows=cfg.buffer.max_rows_events,
        max_batch=cfg.events.max_batch,
        telemetry=False,
    )
    for worker in (telemetry_sender, events_dispatcher, events_flusher):
        worker.start()

    seq = 0
    last_log = 0.0
    last_telemetry_send = 0.0
    last_confirmed_movement = time.monotonic()
    movement_candidate_since: float | None = None
    sleep_active = False

    log.info(
        "started device_id=%s url=%s interval_s=%s sample_count=%s wifi_scan_interval_s=%s",
        cfg.device.id,
        cfg.send.url,
        cfg.send.interval_s,
        cfg.weight.sample_count,
        cfg.wifi.scan_interval_s,
    )

    try:
        while True:
            loop_started = time.monotonic()
            seq += 1

            if cfg.lte.events_enabled:
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

            # Take the current device snapshot only after potentially slow LTE
            # work.  The builder immediately timestamps this GPS/weight pair.
            pos = gps.latest()
            weight = weight_sampler.latest()
            wifi_clients, wifi_err = wifi_monitor.latest()
            coordinates_in_range = (
                pos.lat is not None
                and pos.lon is not None
                and -90.0 <= pos.lat <= 90.0
                and -180.0 <= pos.lon <= 180.0
            )
            if pos.lat is not None and pos.lon is not None and not coordinates_in_range:
                log.warning("invalid GPS coordinates ignored: lat=%s lon=%s", pos.lat, pos.lon)
            gps_valid = (
                coordinates_in_range
                and (pos.quality or 0) > 0
                and pos.age_s is not None
                and pos.age_s <= max(0.1, float(cfg.gps.max_fix_age_s))
            )
            events_status = events_reader.status()
            events_reader_ok = (not cfg.lte.events_enabled) or (
                bool(events_status.get("running")) and not events_status.get("last_error")
            )

            telemetry = build_telemetry(
                device_id=cfg.device.id,
                position=pos,
                weight=weight,
                wifi_clients=wifi_clients,
                cpu_temp_c=read_cpu_temp_c(),
                lte=lte,
                gps_valid=gps_valid,
                weight_valid=weight.weight is not None,
                events_reader_ok=events_reader_ok,
            )
            payload = telemetry.model_dump(mode="json")

            now = time.monotonic()
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

            current_send_interval_s = idle_interval_s if sleep_active else max(0.1, float(cfg.send.interval_s))
            should_send = last_telemetry_send <= 0.0 or now - last_telemetry_send >= current_send_interval_s * 0.95
            if should_send:
                live_id = telemetry_q.put(payload)
                telemetry_sender.notify(live_id)
                last_telemetry_send = now

            for event in events_reader.drain(max_items=20):
                event_payload = {"device_id": cfg.device.id, **event}
                if not events_dispatcher.submit(event_payload):
                    events_q.put(event_payload)
                    log.warning("events dispatcher full; event buffered")

            weight_status = weight_sampler.status()
            telemetry_sender_status = telemetry_sender.status()
            recovery_status = recovery_watchdog.observe(
                telemetry_last_success_age_s=telemetry_sender_status.get("last_success_age_s"),
                raw_weight_kg=weight.raw,
                weight_age_s=weight_status.get("age_s"),
            )

            loop_duration_s = time.monotonic() - loop_started
            if now - last_log >= 10.0:
                last_log = now
                module_status = {
                    "gps": gps.status(),
                    "weight": weight_status,
                    "wifi": wifi_monitor.status(),
                    "wifi_scan_error": wifi_err,
                    "telemetry_buffer_rows": telemetry_q.count(),
                    "telemetry_buffer_oldest_age_s": telemetry_q.oldest_age_s(),
                    "events_buffer_rows": events_q.count(),
                    "events_buffer_oldest_age_s": events_q.oldest_age_s(),
                    "events_reader": events_status,
                    "telemetry_outbox_sender": telemetry_sender_status,
                    "auto_reboot": recovery_status,
                    "events_dispatcher": events_dispatcher.status(),
                    "events_flush": events_flusher.status(),
                    "telemetry_send": {
                        "sleep_active": sleep_active,
                        "current_interval_s": current_send_interval_s,
                        "idle_for_s": now - last_confirmed_movement,
                        "current_speed_kmh": speed_kmh,
                    },
                    "loop_duration_s": loop_duration_s,
                }
                log.info(
                    "seq=%s gps_fix=%s weight=%s wifi=%s lte_rssi=%s tbuf=%s ebuf=%s status=%s",
                    seq,
                    pos.quality,
                    weight.weight,
                    len(wifi_clients),
                    lte.rssi_dbm,
                    telemetry_q.count(),
                    events_q.count(),
                    module_status,
                )

            # The scheduler no longer waits for ADC, Wi-Fi commands, HTTP, or
            # backlog replay. With sleep disabled this keeps packet creation at
            # approximately send.interval_s.
            time.sleep(max(0.0, min(current_send_interval_s, cfg.send.interval_s) - loop_duration_s))
    finally:
        for worker in (telemetry_sender, events_dispatcher, events_flusher):
            worker.stop()
        weight_sampler.stop()
        wifi_monitor.stop()
        gps.stop()
        events_reader.stop()
        telemetry_q.close()
        events_q.close()


if __name__ == "__main__":
    main()
