"""Small numeric normalizer for external API values."""
import math


def as_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None
