"""The working scale value has one shared 5 kg resolution."""
import math


def round_weight(value: float) -> float:
    return math.copysign(math.floor(abs(value) / 5 + .5) * 5, value)


class WeightDisplay:
    """One kilogram of hysteresis prevents flicker at a rounding boundary."""
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def update(self, value: float) -> float:
        if self.value is None or abs(value - self.value) >= 3.5:
            self.value = round_weight(value)
        return self.value
