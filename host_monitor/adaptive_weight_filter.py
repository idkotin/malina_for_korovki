"""Time-based smoothing for the calibrated signal; never changes calibration.

Second differences estimate vibration without treating a constant loading ramp
as noise. A sustained force is indistinguishable from mass: this is not motion
compensation and must not be used as an accuracy guarantee.
"""
from collections import deque
import math
import statistics


class AdaptiveWeightFilter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = deque(maxlen=11)
        self.value = None
        self.time = None
        self.noisy_until = -math.inf

    def update(self, value: float, now: float) -> float:
        if not math.isfinite(value) or not math.isfinite(now):
            self.reset()
            raise ValueError('Non-finite weight or time')
        if self.time is not None and (now <= self.time or now - self.time > 3):
            self.reset()
        dt = .5 if self.time is None else now - self.time
        self.time = now
        self.samples.append(value)
        samples = list(self.samples)
        differences = [samples[i] - 2 * samples[i-1] + samples[i-2]
                       for i in range(2, len(samples))]
        # A single step contributes only two differences; the median rejects it.
        if len(differences) >= 5:
            noise = statistics.median(abs(x) for x in differences) / 1.652
            if noise > 15:
                self.noisy_until = now + 3
        noisy = now < self.noisy_until
        target = statistics.median(samples[-9:] if noisy else samples[-3:])
        # Fast branch only in a quiet signal, including the user's 100 kg threshold.
        delta = 0 if self.value is None else abs(target - self.value)
        tau = 2.5 if noisy else (.45 if delta >= 100 else .65)
        alpha = -math.expm1(-dt / tau)
        self.value = target if self.value is None else self.value + alpha * (target-self.value)
        return float(self.value)
