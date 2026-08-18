"""Map a stated confidence onto the win rate it has actually earned.

The desk states confidence as if it were a probability. Measured 2026-07-31
over resolved directional outcomes, in 5-point buckets with n>=20:

    stated  60 -> 51.5% won  (n= 33)
    stated  65 -> 43.3% won  (n=150)
    stated  70 -> 61.8% won  (n=309)
    stated  75 -> 63.2% won  (n=484)
    stated  80 -> 66.7% won  (n= 30)
    stated  85 -> 67.4% won  (n=307)
    stated  90 -> 68.3% won  (n= 63)
    stated  95 -> 45.5% won  (n= 44)   <-- inverts

The ordering is mostly fine — 70 through 90 rises monotonically and Kendall's
tau against win rate is +0.50 (against |P&L|, +0.64). The two real defects are
narrower than a first, coarser look suggested:

  1. The LEVEL is wrong everywhere: a sample-weighted 15.8 points of
     overstatement. "90" has earned 68%.
  2. The TOP BUCKET inverts. 95 wins 45.5% — below a coin flip, and worse than
     every bucket above 65.

`calibration_map()` fixes both with isotonic regression (pool-adjacent-
violators). PAVA is the right tool here rather than a fitted curve: it assumes
only that the mapping should be non-decreasing, invents no functional form, and
repairs an inversion by POOLING the offending buckets rather than deleting or
smoothing them. The 95 bucket does not vanish — it merges with its neighbours
and the merged group reports their combined realized rate, which is the honest
statement that the data cannot separate them.

What this is NOT: a fix for the desk. Correcting the number after the fact
leaves the agent just as miscalibrated as before; it only stops downstream
consumers reading a claim the record does not support. The source fix is in the
decision prompt.
"""

from __future__ import annotations

import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Buckets thinner than this are noise: a 3-row bucket at 100% would otherwise
# anchor the whole curve.
MIN_BUCKET = 20
BUCKET_WIDTH = 5

# Outcomes that represent a real directional call. DEGRADED_ARTIFACT (pipeline
# crashes scored as trades, confidence 0) and FLAT are excluded by omission —
# see docs/EDGE_MEASUREMENT_2026-07-31.md.
_DIRECTIONAL = ("WIN", "LOSS")


def _pava(points: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Pool adjacent violators: enforce a non-decreasing y, weighted by n.

    Walks left to right; whenever a block's mean falls below the block behind
    it, the two merge and the merged mean replaces both. Repeats until the
    sequence is monotone. O(n) amortised — each merge removes a block.
    """
    blocks: list[list[float]] = []  # [weighted_sum, weight, min_x, max_x]
    for x, y, n in points:
        blocks.append([y * n, float(n), x, x])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            b = blocks.pop()
            prev = blocks[-1]
            prev[0] += b[0]
            prev[1] += b[1]
            prev[3] = b[3]
    out: list[tuple[float, float, int]] = []
    for wsum, w, lo, hi in blocks:
        mean = wsum / w
        # Re-expand a pooled block across the range it now covers, so the
        # caller still sees every bucket it asked about.
        for x, _, n in points:
            if lo <= x <= hi:
                out.append((x, mean, n))
    return out


def calibration_map(min_bucket: int = MIN_BUCKET) -> dict:
    """Stated-vs-realized per bucket, plus the isotonic-corrected mapping."""
    # _DIRECTIONAL is a module constant of bare identifiers, never user input,
    # so inlining it is safe. Parameterising a tuple as `IN %s` does not bind
    # through this db wrapper — it failed to an EMPTY map rather than raising,
    # which would have read as "no calibration data" forever.
    directional = ", ".join(f"'{o}'" for o in _DIRECTIONAL)
    try:
        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT (confidence / {BUCKET_WIDTH})::int * {BUCKET_WIDTH} AS bucket,
                       COUNT(*) n,
                       AVG(confidence) stated,
                       AVG(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0.0 END) * 100 realized
                FROM decision_outcomes
                WHERE outcome IN ({directional})
                  AND confidence IS NOT NULL AND confidence > 0
                GROUP BY 1 ORDER BY 1
                """
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — a missing map must not break a cycle
        logger.warning("[CALIB] calibration map unavailable: %s", e)
        return {"buckets": [], "error": str(e)}

    qualified = [(float(b), float(real), int(n)) for b, n, _stated, real in rows
                 if n >= min_bucket]
    if len(qualified) < 2:
        return {"buckets": [], "note": f"fewer than 2 buckets with n>={min_bucket}"}

    corrected = {x: y for x, y, _ in _pava(qualified)}
    buckets = []
    for b, n, stated, realized in rows:
        if n < min_bucket:
            continue
        buckets.append({
            "bucket": int(b),
            "n": int(n),
            "stated": round(float(stated), 1),
            "realized": round(float(realized), 1),
            "calibrated": round(corrected[float(b)], 1),
            "overstatement": round(float(stated) - float(realized), 1),
        })
    inversions = sum(
        1 for a, b in zip(buckets, buckets[1:]) if b["realized"] < a["realized"]
    )
    return {
        "buckets": buckets,
        "inversions": inversions,
        "mean_overstatement": round(
            sum(x["overstatement"] * x["n"] for x in buckets)
            / sum(x["n"] for x in buckets), 1),
        "method": "isotonic (PAVA), non-decreasing in stated confidence",
    }


def calibrated_confidence(stated: float, cmap: dict | None = None) -> float | None:
    """The win rate a stated confidence has historically earned.

    Returns None rather than a guess when the map cannot be built or the
    value sits outside the range that has evidence — an extrapolated
    calibration is exactly the unearned claim this module exists to stop.
    """
    cmap = cmap if cmap is not None else calibration_map()
    buckets = cmap.get("buckets") or []
    if not buckets:
        return None
    lo, hi = buckets[0]["bucket"], buckets[-1]["bucket"] + BUCKET_WIDTH
    if stated < lo or stated > hi:
        return None
    best = min(buckets, key=lambda b: abs(b["bucket"] - stated))
    return best["calibrated"]
