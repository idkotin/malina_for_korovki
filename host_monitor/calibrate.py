from __future__ import annotations

import argparse

from host_monitor.config import ensure_dirs, load_config
from host_monitor.weight_reader import WeightCfg, WeightReader


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="./config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tare", help="Set zero offset (tare).")

    c = sub.add_parser("calibrate", help="Calibrate scale using known kg.")
    c.add_argument("--known-kg", type=float, required=True)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    cfg = load_config(args.config)
    ensure_dirs(cfg)

    wcfg = WeightCfg(
        enabled=True,
        driver=cfg.weight.driver,
        calibration_path=cfg.weight.calibration_path,
        simulate=cfg.weight.simulate,
        waveshare_path=cfg.weight.waveshare_path,
        channel_pos=cfg.weight.channel_pos,
        channel_neg=cfg.weight.channel_neg,
        sample_count=cfg.weight.sample_count,
        adc_rate=cfg.weight.adc_rate,
    )
    wr = WeightReader(wcfg)

    if args.cmd == "tare":
        off = wr.tare()
        print(f"tare offset saved: {off}")
        return

    if args.cmd == "calibrate":
        sc = wr.calibrate_with_known(float(args.known_kg))
        print(f"scale saved: {sc} kg/raw_unit")
        return

