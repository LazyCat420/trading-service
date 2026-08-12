"""Stated confidence -> empirical win rate. SHADOW ONLY: gates nothing.

WHY THIS SHIPS INERT. `documentation/chapters/08-confidence-rebuild.md`
(approved 2026-08-07) makes stage 1 — recalibration — the bridge, and attaches
two conditions that a live cutover in THIS change would break: it must sit
behind a parameter defaulting off, and it must not ship in the same measurement
window as other decision-layer work. The 2026-08-11 change that carries this
module also retries the bull defense and carries the bear's substitute forward,
so switching the scale on here would confound all three and reproduce exactly
the unreadable window the 08-05 anchor produced.

So this computes and records; nothing reads it back into a decision. What it
buys now is the number the cutover argument needs: **how many decisions would
have cleared the floor on a recalibrated scale**, measured on live rows rather
than argued from the fit.

THE FIT. `scripts/confidence_audit.py --calibrate --horizon 10`, run
2026-08-11 against the live database at service `2a80e8f`: isotonic regression
trained on the chronological first half (n=156) and scored out of sample on the
second (n=156). Out-of-sample Brier **0.2433 recalibrated** vs **0.2609 raw**
vs **0.2549 base rate** — i.e. the raw scale is worse than knowing nothing and
the map beats both.

REFIT, DO NOT EDIT BY HAND. Re-run that command and replace `_FIT`. A map that
is edited to taste is a second drifting scale, which is the defect this exists
to answer. The fit is dated because it rots: it was measured over a window in
which the desk produced ZERO decisions at or above 80, so the top of the map
rests on pre-collapse rows and must be re-derived once the desk can reach that
band again.
"""

from __future__ import annotations

#: When `_FIT` was measured, and against what. Carried into the recorded
#: artifact so a stored shadow value can never be read as current.
FIT_PROVENANCE: dict = {
    "fitted_on": "2026-08-11",
    "service_sha": "2a80e8f",
    "method": "isotonic, train first half / score second",
    "n_train": 156,
    "n_test": 156,
    "brier_raw": 0.2609,
    "brier_recalibrated": 0.2433,
    "brier_base_rate": 0.2549,
    "horizon_sessions": 10,
}

#: stated confidence -> empirical probability of a winning call. Monotone by
#: construction (isotonic); the flat runs are real, not rounding.
_FIT: tuple[tuple[int, float], ...] = (
    (50, 0.516),
    (55, 0.556),
    (60, 0.556),
    (65, 0.605),
    (70, 0.650),
    (75, 0.650),
    (80, 0.917),
    (85, 0.917),
    (90, 0.917),
)


def empirical_win_rate(confidence: float | int | None) -> float | None:
    """The fitted win rate for a stated confidence, linearly interpolated.

    Returns None for a missing or non-numeric input rather than a default: a
    confidence that was never stated must not become a number that looks
    stated. Clamps outside the fitted range instead of extrapolating — there is
    no evidence out there and inventing some is how a map earns false authority.
    """
    if confidence is None or isinstance(confidence, bool):
        return None
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return None

    if c <= _FIT[0][0]:
        return _FIT[0][1]
    if c >= _FIT[-1][0]:
        return _FIT[-1][1]

    for (x0, y0), (x1, y1) in zip(_FIT, _FIT[1:]):
        if x0 <= c <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (c - x0) / (x1 - x0)
    return None


def shadow_record(confidence: float | int | None, floor: float | int) -> dict | None:
    """What the recalibrated scale would have said about this decision.

    `would_clear_recalibrated` answers the cutover question directly: the floor
    of 70 is calibrated on the RAW scale (total P&L kept), so its equivalent on
    the fitted scale is the fitted value AT 70 — not 0.70, which would be a
    different and much stricter bar arrived at by reading a probability as a
    percentage.
    """
    p = empirical_win_rate(confidence)
    if p is None:
        return None
    floor_p = empirical_win_rate(floor)
    return {
        "stated": float(confidence),
        "empirical_win_rate": round(p, 4),
        "floor_equivalent": round(floor_p, 4) if floor_p is not None else None,
        "would_clear_recalibrated": (
            bool(floor_p is not None and p >= floor_p)
        ),
        "fit": FIT_PROVENANCE["fitted_on"],
        "shadow_only": True,
    }
