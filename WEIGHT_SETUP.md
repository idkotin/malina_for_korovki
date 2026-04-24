# Weight Setup

This repository now defaults to the working passive parallel mode validated on the bench:

```text
Factory terminal keeps powering the bridge.
ADS1263 only listens.
```

## Wiring

```text
SIG+  -> HAT IN0
SIG-  -> HAT IN1
E-    -> HAT AVSS/GND
AVDD  -> not connected
```

## Config defaults

```yaml
weight:
  frontend: "adc2"
  reference_mode: "internal"
  channel_pos: 0
  channel_neg: 1
  sample_count: 24
  adc2_rate: "ADS1263_ADC2_400SPS"
  smoothing_alpha: 0.12
  median_window: 7
```

## Notes

- `adc1` is kept only as a legacy fallback.
- `ref_pos/ref_neg` are ignored in the default passive `adc2` path.
- After switching from the old adc1/external-reference scheme, recalibration is required.
