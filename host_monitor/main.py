from __future__ import annotations

import json
import logging
import time

from host_monitor.buffer import BufferCfg as BufferCfgDC
from host_monitor.buffer import SqliteBuffer
from host_monitor.config import ensure_dirs, load_config, parse_args
from host_monitor.gps_reader import GpsCfg as GpsCfgDC
from host_monitor.gps_reader import GpsReader
from host_monitor.log_setup import setup_logging
from host_monitor.lte_info import LteCfg as LteCfgDC
from host_monitor.lte_info import get_lte_info
from host_monitor.sender import Sender
from host_monitor.system_info import read_cpu_temp_c
from host_monitor.telemetry_builder import build_telemetry
from host_monitor.weight_reader import WeightCfg as WeightCfgDC
from host_monitor.weight_reader import WeightReader
from host_monitor.wifi_clients import WifiCfg as WifiCfgDC
from host_monitor.wifi_clients import get_wifi_clients


log = logging.getLogger("host_monitor")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    setup_logging(cfg.logging)

    buffer = SqliteBuffer(BufferCfgDC(sqlite_path=cfg.buffer.sqlite_path, max_rows=cfg.buffer.max_rows))

    gps = GpsReader(
        GpsCfgDC(
            enabled=cfg.gps.enabled,
            port=cfg.gps.port,
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
        )
    )

    sender = Sender(cfg.send.url, timeout_s=cfg.send.timeout_s)

    seq = 0
    last_log = 0.0
    last_flush_attempt = 0.0

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
            lte = get_lte_info(
                LteCfgDC(enabled=cfg.lte.enabled, mmcli=cfg.lte.mmcli, at_ports=cfg.lte.at_ports, at_baud=cfg.lte.at_baud)
            )
            cpu_temp = read_cpu_temp_c()

            module_status = {
                "gps": gps.status(),
                "wifi_error": wifi_err,
                "buffer_rows": buffer.count(),
            }

            telemetry = build_telemetry(
                device_id=cfg.device.id,
                seq=seq,
                position=pos,
                weight=w,
                wifi_clients=wifi_clients,
                cpu_temp_c=cpu_temp,
                lte=lte,
                module_status=module_status,
            )
            payload = telemetry.model_dump(mode="json")

            # Send current payload (if fails -> buffer)
            try:
                sender.send_one(payload)
            except Exception as e:
                module_status["send_error"] = str(e)
                buffer.put(payload)

            # Flush buffer periodically (every ~1s) in FIFO order
            now = time.time()
            if now - last_flush_attempt >= 1.0:
                last_flush_attempt = now
                try:
                    batch = buffer.peek_batch(cfg.send.max_batch)
                    if batch:
                        ids = [rid for rid, _ in batch]
                        sender.send_batch([p for _, p in batch])
                        buffer.delete_ids(ids)
                except Exception as e:
                    module_status["flush_error"] = str(e)

            # Periodic status log
            if now - last_log >= 10.0:
                last_log = now
                log.info(
                    "seq=%s gps_ok=%s weight_ok=%s wifi=%s lte_ok=%s buffer=%s",
                    seq,
                    pos.ok,
                    w.ok,
                    len(wifi_clients),
                    lte.ok,
                    buffer.count(),
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

