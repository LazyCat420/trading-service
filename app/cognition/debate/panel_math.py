"""Pooling and scoring for the probabilistic panel — no I/O, no LLM, no DB.

Kept separate from ``probabilistic_panel.py`` on purpose: this is the part that
decides what the panel actually *says*, and it must be testable without a model
in the loop. The tournament's aggregation was a majority vote buried inside a
790-line async function and was therefore never unit-tested against a known
answer.

Why these specific choices (measured, not preferred):

* **Probabilities, not a winner.** The tournament emitted
  ``bull``/``bear``/``split`` and a confidence of ``avg_jury_score * 10``. A
  3-value label cannot be Brier-scored, cannot be calibration-checked, and is a
  weak regressor against continuous P&L. Plurality voting also discards correct
  minority views — the documented "consensus collapse", oracle gaps up to
  32.3pp.

* **Confidence-weighted pooling in logit space.** Averaging probabilities
  directly under-weights confident dissent: one agent at 0.95 and three at 0.5
  should not land at 0.61. Logit pooling with ``w = |logit(p)|`` amplifies
  agents holding strong private evidence and was measured worth ~0.018 Brier
  over uniform weighting.

* **Two rounds.** Three converge and degrade — the third round buys agreement,
  not accuracy.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

#: Probabilities are clamped before any logit. p=0 or p=1 maps to +-inf, which
#: would let a single overconfident agent dominate the pool outright and would
#: make the Brier decomposition undefined. 0.5% is tighter than any forecast a
#: 7-day equity call can honestly justify.
_EPS = 0.005

#: A pooled probability this close to 0.5 carries no directional information.
#: Reported so the caller can say "no view" rather than dress a coin flip as a
#: verdict.
_NEUTRAL_BAND = 0.02


def clamp_probability(p: float) -> float:
    """Clamp to [_EPS, 1-_EPS]. Non-numeric or NaN resolves to 0.5 (no view).

    NaN is handled explicitly: every comparison against NaN is False, so a naive
    ``min/max`` clamp passes it straight through and it then poisons the pool.
    That exact shape — NaN sailing through a ``<`` gate — has bitten this
    codebase before, in the confidence floor.
    """
    try:
        v = float(p)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(v) or math.isinf(v):
        return 0.5
    return max(_EPS, min(1.0 - _EPS, v))


def logit(p: float) -> float:
    p = clamp_probability(p)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def pool_probabilities(
    probs: Sequence[float],
    *,
    confidence_weighted: bool = True,
) -> float:
    """Combine independent forecasts into one probability.

    ``confidence_weighted=False`` gives the uniform-logit control, which is the
    comparison that shows whether the weighting earned anything.

    An all-neutral panel (every agent at 0.5) returns exactly 0.5 rather than
    dividing by a zero weight sum — "nobody has a view" is a real answer.
    """
    vals = [clamp_probability(p) for p in probs]
    if not vals:
        return 0.5

    logits = [logit(p) for p in vals]
    if not confidence_weighted:
        return sigmoid(sum(logits) / len(logits))

    # w = 1 + |logit(p)|, not |logit(p)|.
    #
    # The bare form gives a neutral agent weight ZERO, so [0.95, 0.5, 0.5, 0.5]
    # pools to exactly 0.95 — one agent silently becomes the panel and three
    # abstentions are indistinguishable from three agreements. Verified against
    # the naive version before changing it.
    #
    # The +1 floor keeps every agent in the average while still letting a
    # confident view outweigh a hesitant one. An agent saying "I don't know" is
    # evidence that the call is hard; it is not an endorsement.
    weights = [1.0 + abs(l) for l in logits]
    total = sum(weights)
    if total <= 0:  # unreachable with the +1 floor; kept so a future edit cannot divide by zero
        return 0.5
    return sigmoid(sum(w * l for w, l in zip(weights, logits)) / total)


def disagreement(probs: Sequence[float]) -> float:
    """Spread of the panel, 0..1. Max |p_i - p_j| over the panel.

    This is the number the tournament threw away. A pooled 0.55 built from
    [0.54, 0.55, 0.56] and one built from [0.15, 0.95, 0.55] mean completely
    different things, and only the second is worth a human's attention.
    """
    vals = [clamp_probability(p) for p in probs]
    if len(vals) < 2:
        return 0.0
    return max(vals) - min(vals)


def probability_to_action(p: float, *, band: float = 0.10) -> str:
    """Map a pooled probability to the BUY/SELL/HOLD vocabulary downstream
    consumers still speak.

    The band is deliberately wide: ``p`` is P(up over ~7 sessions), and a base
    rate near 0.5 means small deviations are noise. Nothing here decides a
    trade — the Board does — this only keeps the artifact contract intact.
    """
    p = clamp_probability(p)
    if p >= 0.5 + band:
        return "BUY"
    if p <= 0.5 - band:
        return "SELL"
    return "HOLD"


def probability_to_confidence(p: float) -> int:
    """Map a probability to the 0-100 ``confidence`` every consumer reads.

    ``|p - 0.5| * 200`` — a 0.5 forecast is confidence 0 (no view), 0.95 or 0.05
    is 90. Distance from the coin flip, not the probability itself, because
    downstream ``confidence`` has always meant "how sure", not "how bullish".
    """
    return int(round(abs(clamp_probability(p) - 0.5) * 200))


def is_neutral(p: float, *, band: float = _NEUTRAL_BAND) -> bool:
    return abs(clamp_probability(p) - 0.5) < band


def brier_score(pairs: Iterable[tuple[float, int]]) -> float | None:
    """Mean (p - y)^2 over (probability, outcome) pairs. None if empty.

    Reference points: 0.25 is what a constant 0.5 forecaster scores, and the
    honest null is the base rate on the same rows, ``p̄(1-p̄)`` — not 0.25.
    Beating 0.25 is table stakes; beating the base rate is a result.
    """
    pairs = [(clamp_probability(p), int(y)) for p, y in pairs]
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def brier_decomposition(pairs: Sequence[tuple[float, int]], *, bins: int = 10) -> dict:
    """Murphy decomposition: Brier = reliability - resolution + uncertainty.

    **Resolution is the number that matters here.** This system's standing
    finding is that it can identify its own bad decisions but cannot pick
    winners — i.e. it has reliability and no resolution. A panel that only
    improves reliability has bought nothing, and reporting the total Brier alone
    would hide that.
    """
    pairs = [(clamp_probability(p), int(y)) for p, y in pairs]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "brier": None, "reliability": None,
                "resolution": None, "uncertainty": None}

    base = sum(y for _, y in pairs) / n
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, y in pairs:
        idx = min(bins - 1, int(p * bins))
        buckets.setdefault(idx, []).append((p, y))

    reliability = resolution = 0.0
    for rows in buckets.values():
        k = len(rows)
        mean_p = sum(p for p, _ in rows) / k
        obs = sum(y for _, y in rows) / k
        reliability += k * (mean_p - obs) ** 2
        resolution += k * (obs - base) ** 2

    return {
        "n": n,
        "base_rate": round(base, 4),
        "brier": round(brier_score(pairs), 4),
        "reliability": round(reliability / n, 4),   # lower is better
        "resolution": round(resolution / n, 4),     # HIGHER is better
        "uncertainty": round(base * (1 - base), 4),  # the base-rate null
    }
