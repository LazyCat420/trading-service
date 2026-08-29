"""Numeric coercion guards shared by the quant and context-block layers."""
import math
from typing import Any


def finite(value: Any) -> float | None:
    """Coerce to a real, finite float, or None.

    NaN is the reason this exists. It survives a NOT NULL check, it compares
    false against every threshold, and it propagates through arithmetic -- so
    an unguarded one does not fail a band, it silently declines to score while
    looking scored. asset_prices carries NaN for symbols a vendor returned
    empty, so this is a live input shape, not a hypothetical.

    Booleans are rejected rather than coerced. `float(True)` is 1.0, which is a
    perfectly plausible ratio, so a bool arriving where a metric belongs would
    otherwise be scored as data instead of caught as the defect it is. Two of
    the three copies this replaces accepted bools; the strictest wins, since
    the alternative is a fabricated 1.0 nobody can trace.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None
