"""Fixed-gain state observer for room temperatures."""

import math
from typing import Optional, Tuple


def correct_temperature(
    predicted_c: Optional[float],
    measured_c: Optional[float],
    quality: str,
    gain: float = 0.3,
) -> Tuple[Optional[float], str]:
    """Apply a measurement correction or return the model state classification."""
    if not 0.0 <= gain <= 1.0:
        raise ValueError("Observer gain must be between 0 and 1")
    if predicted_c is not None and not math.isfinite(predicted_c):
        raise ValueError("Predicted temperature must be finite")
    if measured_c is not None and not math.isfinite(measured_c):
        raise ValueError("Measured temperature must be finite")
    if quality not in {"good", "suspect", "stale", "bad"}:
        raise ValueError("Unknown measurement quality: %s" % quality)
    if measured_c is not None and quality == "good":
        if predicted_c is None:
            return measured_c, "measured"
        return predicted_c + gain * (measured_c - predicted_c), "estimated"
    if predicted_c is None:
        return None, "unknown"
    return predicted_c, "estimated"
