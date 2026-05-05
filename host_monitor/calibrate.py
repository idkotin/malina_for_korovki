from __future__ import annotations

import argparse
import statistics
import time

from host_monitor.config import ensure_dirs, load_config
from host_monitor.weight_reader import ScaleCalibration, WeightCfg, WeightReader, save_calibration


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="./config.yaml")
    return p


def _read_known_weight(prompt: str) -> float:
    raw = input(prompt).strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError as e:
        raise ValueError(f"invalid weight value: {raw!r}") from e
    if value < 0:
        raise ValueError("known weight must be >= 0")
    return value


def _build_weight_reader(config_path: str) -> tuple[WeightReader, str]:
    cfg = load_config(config_path)
    ensure_dirs(cfg)

    wcfg = WeightCfg(
        enabled=True,
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
        median_window=cfg.weight.median_window,
        min_ref_abs=cfg.weight.min_ref_abs,
    )
    return WeightReader(wcfg), cfg.weight.calibration_path


def _capture_stable_raw(reader: WeightReader, rounds: int = 9, delay_s: float = 0.2) -> tuple[float, float]:
    values: list[float] = []
    for idx in range(max(1, rounds)):
        values.append(float(reader.read_raw()))
        if idx + 1 < rounds:
            time.sleep(delay_s)
    noise = statistics.pstdev(values) if len(values) > 1 else 0.0
    return float(statistics.median(values)), float(noise)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    wr, calibration_path = _build_weight_reader(args.config)

    print("Two-point interactive calibration.")
    if wr.uses_passive_parallel_mode():
        print("Passive parallel mode detected.")
        print("Reader starts first, then you can power on the factory terminal.")
        wr.prepare()
        input(
            "Turn on the factory terminal, wait until its own display reaches zero and the first load is ready, then press Enter..."
        )
    else:
        print("Step 1: put the first known load on the scale and wait until it stabilizes.")

    known_kg_1 = _read_known_weight("Enter the current known total weight in kg: ")
    raw_1, noise_1 = _capture_stable_raw(wr)
    print(f"Captured point 1: known_kg={known_kg_1} raw={raw_1} noise={noise_1}")

    input("Add or change the load, wait for stabilization, then press Enter...")
    known_kg_2 = _read_known_weight("Enter the new known total weight in kg: ")
    if known_kg_2 == known_kg_1:
        raise ValueError("the two known weights must be different")
    raw_2, noise_2 = _capture_stable_raw(wr)
    print(f"Captured point 2: known_kg={known_kg_2} raw={raw_2} noise={noise_2}")

    if abs(raw_2 - raw_1) < 1e-9:
        raise RuntimeError("raw calibration points are too close; check loads and wiring")

    span = abs(raw_2 - raw_1)
    noise = max(noise_1, noise_2, 1e-9)
    snr = span / noise
    print(f"Calibration raw span={span} max_noise={noise} span/noise={snr:.1f}")
    if snr < 10.0:
        print(
            "WARNING: calibration span is close to noise. Use a larger known load if possible; "
            "otherwise readings can jump a lot."
        )

    scale = float((known_kg_2 - known_kg_1) / (raw_2 - raw_1))
    offset = float(raw_1 - (known_kg_1 / scale))
    cal = ScaleCalibration(offset=offset, scale=scale)
    save_calibration(calibration_path, cal)
    print(f"Calibration saved: offset={cal.offset} scale={cal.scale}")

