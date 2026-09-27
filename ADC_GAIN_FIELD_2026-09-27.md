# ADS1263 ADC2 field gain comparison, 2026-09-27

Empty stationary mixer reported by operator. Internal reference, IN0/IN1,
8 conversions per trimmed mean. Tests performed remotely with host-monitor
stopped, timeout and independent systemd recovery timer. Each test restored
original registers and restarted host-monitor. No calibration files changed.

All figures below are input-referred counts (hardware counts divided by gain).

| Gain, 100 SPS | Bridge median | Bridge SD | IN0-IN0 median | IN0-IN0 SD |
|---|---:|---:|---:|---:|
| 1 | 2738.167 | 68.381 | 814.667 | 73.958 |
| 8 | 2083.250 | 15.201 | 316.760 | 12.524 |
| 32 | 1850.581 | 4.444 | 91.576 | 4.221 |
| 128 | 1788.968 | 1.742 | 30.956 | 1.135 |

40 averaged samples per condition, about 10 seconds each. Gain 128 repeat:
100 SPS: 60 means in 15.04 s, median 1788.742, SD 1.989, range 11.138.
10 SPS: 30 means in 29.12 s, median 1789.236, SD 1.225, range 6.987.
100 SPS is preferred for response time: each mean takes about 0.25 s vs 0.97 s.
Do not equate the advertised conversion rate with application update frequency.

Gain 128 substantially reduces the observed input-referred noise. This does not
verify weighing accuracy or loaded response. Offset changes substantially;
existing gain-1 calibration must NOT be used directly with gain 128.
The old provisional slope 2.0209731543624163 kg/count is also unverified.
Confirm the empty stationary state before capturing a new offset and then
validate response with an independently known load. No automatic zero capture.

Implementation: optional weight.adc2_gain (default 1), raw means normalized by
gain, gain recorded with calibration, mismatched calibration blocks weights.
Old calibration files without gain are interpreted as gain 1. Gain 1 preserves
the previous calibration identity; higher gains participate in that identity.
Register readback must confirm the requested configuration.

Validation: 78 unit tests, compileall, both shipped configuration files loaded.
Source: TI ADS1263 datasheet, ADC2 PGA and noise tables:
https://files.waveshare.com/upload/2/2a/Ads1262.pdf
