"""Per-version skill scorecard — the measured answer to "did this edit help?"

SkillOpt could not ask that question. Its promotion gate was
``_simulated_score = _compute_baseline_score() + prose_delta``, and the gate
compares ``simulated - baseline``, so the realized term **cancels exactly**:
100% of every accept/reject came from heuristics like "contains a digit" and
"has an imperative verb". Its own docstring says as much — "NOT a measure of
trading accuracy, and it never was".

The data to answer it properly was already being collected and read by nothing:
``decision_outcomes.skill_versions`` stamps every agent's active version onto
every decision. This module turns that into a score.

TWO TIERS, AND THEY DO DIFFERENT JOBS
-------------------------------------
The obvious design — realized outcomes as a slow signal, eval scores as a fast
one — does not survive measurement. Both tiers were measured on 21 days of real
data before this was written:

*Lagging (realized outcomes) is the score, and it is small.* Bootstrapped over
1500 resolved decisions, two IDENTICAL versions differ by ±0.207 at n=25, ±0.148
at n=50, ±0.104 at n=100. The old ``MIN_DECISIONS_BEFORE_REEDIT = 25`` therefore
sat entirely inside the noise: a gate at n=25 fires essentially at random.

*Leading (eval scores) is NOT a second score — it is an admissibility filter.*
Excluding infra, genuinely agent-attributable eval failures run at 0.5–1.3%, or
about 13 events in three weeks across the fleet. That cannot detect anything. But
the same data separates cleanly on a different question: the share of runs that
never completed sits at a 1–4% median with a worst normal day of 18%, and hit
**100%** on the day DuckDuckGo began refusing our egress. So it makes an
excellent detector of "the agents were flying blind in this window", and a
version whose decisions were made during an outage must be neither promoted nor
rolled back on that evidence. Scoring a degraded window is the scoring-path
version of the mistake the analyst path already fixed: a broken tool is not an
absence of information.

HOLD IS SCORED SEPARATELY, ON PURPOSE
-------------------------------------
HOLD is 45% of decisions and was excluded from the old baseline entirely
(``action IN ('BUY','SELL')``). It is included here — but as its own component,
never folded into the directional number. ``outcome_tracker`` already made this
call and the reasoning holds: folding "price stayed flat" into win rate lets low
volatility masquerade as directional skill.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

from app.db.connection import get_db
from app.db import mongo_query

logger = logging.getLogger(__name__)

# ── Measured constants. Each carries the measurement that set it. ────────────

# Resolved decisions a version must govern before its score means anything.
# Bootstrap over 1500 real resolved decisions, 20k resamples per point:
#   n=25  → 95% noise band ±0.207     (the old threshold: pure noise)
#   n=50  → ±0.148
#   n=100 → ±0.104
# 100 costs ~2-3 weeks per version. That is the price of a falsifiable loop;
# 25 was fast and unfalsifiable, which is how the loop got here.
MATURITY_N = 100

# A version must be worse than its predecessor by more than the noise band
# before anything is rolled back. Same bootstrap, n=100.
REGRESSION_MARGIN = 0.104

# Share of runs in a window that never completed (completion_score == 0), above
# which the window is treated as contaminated and its score is inadmissible.
# Measured per agent per day over 21 days: median 1.2–4.4%, worst normal day
# 18.2%, outage day 100%. 25% sits clear of the worst normal day and far below
# an outage.
CONTAMINATION_INCOMPLETE_RATE = 0.25
MIN_TRACES_FOR_CONTAMINATION = 30

# Confidence-weighted outcome values. FLAT is a half-win: the call was not wrong,
# it just did not pay.
_DIRECTIONAL_WEIGHTS = {"WIN": 1.0, "FLAT": 0.5, "LOSS": 0.0}
# HOLD_AVOIDED_DECLINE scores 1.0: on a long-only book a hold through a fall is
# a hold that was RIGHT (see outcome_tracker._classify). Adding the key is not
# optional cosmetics — `_weighted` SKIPS any outcome it has no weight for, so
# an unlisted label would silently leave the hold component's n, and every
# maturity check that reads it, short by those rows.
_HOLD_WEIGHTS = {"HOLD_CORRECT": 1.0, "HOLD_AVOIDED_DECLINE": 1.0, "HOLD_MISS": 0.0}

# How much the hold component contributes to the combined score. Below 0.5
# because a HOLD is a weaker claim than a directional call — "nothing much
# happens" is right more often by default — but not zero, because HOLD is the
# plurality of what this system actually decides.
_HOLD_WEIGHT = 0.35

VERDICT_UNCOVERED = "UNCOVERED"        # no version recorded
VERDICT_IMMATURE = "IMMATURE"          # too few governed decisions to say
VERDICT_CONTAMINATED = "CONTAMINATED"  # the window's tools were broken
VERDICT_HEALTHY = "HEALTHY"            # no measurable regression
VERDICT_REGRESSED = "REGRESSED"        # measurably worse than its predecessor


@dataclass
class Component:
    """One scored population."""
    score: float | None = None
    n: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VersionScorecard:
    agent_name: str
    version: int | None
    directional: Component = field(default_factory=Component)
    hold: Component = field(default_factory=Component)
    combined: float | None = None
    n_governed: int = 0
    incomplete_rate: float | None = None
    n_traces: int = 0
    contaminated: bool = False
    verdict: str = VERDICT_UNCOVERED
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["directional"] = self.directional.to_dict()
        d["hold"] = self.hold.to_dict()
        return d

    def summary(self) -> str:
        bits = [f"{self.agent_name} v{self.version}"]
        if self.combined is not None:
            bits.append(f"score={self.combined:.3f}")
        bits.append(f"n={self.n_governed}/{MATURITY_N}")
        if self.directional.n:
            bits.append(f"dir={self.directional.score:.2f}({self.directional.n})")
        if self.hold.n:
            bits.append(f"hold={self.hold.score:.2f}({self.hold.n})")
        if self.incomplete_rate is not None:
            bits.append(f"incomplete={self.incomplete_rate:.0%}")
        bits.append(self.verdict)
        return "  ".join(bits)


# ── Scoring ─────────────────────────────────────────────────────────────────


def _weighted(rows: list[tuple], weights: dict[str, float]) -> Component:
    """Confidence-weighted mean of ``weights[outcome]`` over ``rows``.

    A missing/zero confidence becomes 50 (neutral conviction) rather than 0,
    which would silently drop the row from the denominator.
    """
    num = den = 0.0
    n = 0
    for outcome, confidence in rows:
        w = weights.get(outcome)
        if w is None:
            continue
        conf = float(confidence or 0) or 50.0
        num += conf * w
        den += conf
        n += 1
    return Component(score=(num / den) if den else None, n=n)


def _governed_outcomes(agent_name: str, version: int) -> list[tuple]:
    """Resolved decisions that ran under this exact version of this agent's doc.

    Rows predating the ``skill_versions`` stamp carry NULL and are excluded:
    they were governed by *some* version nobody recorded, and counting them here
    would manufacture a sample that never existed.
    """
    with get_db() as db:
        return db.execute(
            "SELECT outcome, confidence FROM decision_outcomes "
            "WHERE resolved_at IS NOT NULL "
            "AND skill_versions IS NOT NULL "
            "AND (skill_versions ->> %s)::int = %s",
            [agent_name, int(version)],
        ).fetchall()


def _version_window(agent_name: str, version: int) -> tuple:
    """``(started_at, ended_at|None)`` for a version — when it was serving.

    Used to attribute traces, which carry no version stamp of their own. The
    end is the next version's creation, or open if this is the active one.
    """
    with get_db() as db:
        row = mongo_query.find_row('agent_skills', {'agent_name': agent_name, 'version': int(version)}, ['created_at'])
        if not row:
            return None, None
        started = row[0]
        nxt = db.execute(
            "SELECT min(created_at) FROM agent_skills "
            "WHERE agent_name = %s AND version > %s",
            [agent_name, int(version)],
        ).fetchone()
    return started, (nxt[0] if nxt else None)


def _incomplete_rate(agent_name: str, started, ended) -> tuple[float | None, int]:
    """Share of this agent's runs in the window that never completed.

    ``completion_score == 0`` is the tell an outage leaves: the agent could not
    finish because what it needed was unreachable. Returns ``(rate, n)``, with
    rate None when there are too few traces to judge.
    """
    if started is None:
        return None, 0
    clause = "AND e.created_at < %s" if ended else ""
    params = [agent_name, started] + ([ended] if ended else [])
    with get_db() as db:
        row = db.execute(
            "SELECT count(*), "
            "       sum(CASE WHEN e.completion_score = 0 THEN 1 ELSE 0 END) "
            "FROM eval_scores e JOIN agent_traces t ON t.id = e.run_id "
            f"WHERE t.agent_name = %s AND e.created_at >= %s {clause}",
            params,
        ).fetchone()
    n = int(row[0] or 0)
    if n < MIN_TRACES_FOR_CONTAMINATION:
        return None, n
    return float(row[1] or 0) / n, n


def blend(directional: float | None, hold: float | None) -> float | None:
    """Combine the two components. Either alone stands in when the other is empty."""
    if directional is not None and hold is not None:
        return (1 - _HOLD_WEIGHT) * directional + _HOLD_WEIGHT * hold
    return directional if directional is not None else hold


def classify(
    *,
    n_governed: int,
    combined: float | None,
    incomplete_rate: float | None,
    n_traces: int,
) -> tuple[str, str]:
    """``(verdict, detail)`` from already-gathered numbers.

    Pure on purpose: the DB cannot exercise this path yet — version stamping
    began 2026-07-25 and nothing stamped has passed the 7-day resolve lag — so
    the decision logic has to be testable without it.

    Order matters. Contamination is checked FIRST: a window whose tools were
    broken is inadmissible however many decisions it governed, and calling it
    IMMATURE instead would let it mature into a verdict it must never reach.
    """
    if (
        incomplete_rate is not None
        and n_traces >= MIN_TRACES_FOR_CONTAMINATION
        and incomplete_rate >= CONTAMINATION_INCOMPLETE_RATE
    ):
        return VERDICT_CONTAMINATED, (
            f"{incomplete_rate:.0%} of {n_traces} runs never completed — the "
            f"tools were broken in this window, so its decisions say nothing "
            f"about the skill doc"
        )
    if n_governed < MATURITY_N or combined is None:
        return VERDICT_IMMATURE, (
            f"{n_governed}/{MATURITY_N} resolved decisions — below this the "
            f"noise band (±{REGRESSION_MARGIN:.2f}) swallows any real difference"
        )
    return VERDICT_HEALTHY, f"{n_governed} resolved decisions, score {combined:.3f}"


def build_scorecard(agent_name: str, version: int | None) -> VersionScorecard:
    """Score one version of one agent's skill doc. Never raises."""
    card = VersionScorecard(agent_name=agent_name, version=version)
    if not version:
        card.detail = "no skill version recorded"
        return card

    try:
        rows = _governed_outcomes(agent_name, version)
    except Exception as e:  # noqa: BLE001 — a scorecard must never break a cycle
        logger.warning("[SCORECARD] %s v%s outcome query failed: %s",
                       agent_name, version, e)
        card.detail = f"outcome query failed: {e}"
        return card

    card.directional = _weighted(rows, _DIRECTIONAL_WEIGHTS)
    card.hold = _weighted(rows, _HOLD_WEIGHTS)
    card.n_governed = card.directional.n + card.hold.n

    card.combined = blend(card.directional.score, card.hold.score)

    try:
        started, ended = _version_window(agent_name, version)
        card.incomplete_rate, card.n_traces = _incomplete_rate(
            agent_name, started, ended
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[SCORECARD] %s v%s trace window unavailable: %s",
                     agent_name, version, e)

    card.verdict, card.detail = classify(
        n_governed=card.n_governed,
        combined=card.combined,
        incomplete_rate=card.incomplete_rate,
        n_traces=card.n_traces,
    )
    card.contaminated = card.verdict == VERDICT_CONTAMINATED
    return card


def compare_to_predecessor(agent_name: str, version: int) -> tuple[VersionScorecard, VersionScorecard | None]:
    """``(current, predecessor)`` scorecards. Predecessor is None at v1."""
    current = build_scorecard(agent_name, version)
    if version and version > 1:
        return current, build_scorecard(agent_name, version - 1)
    return current, None


def regression_verdict(agent_name: str, version: int) -> VersionScorecard:
    """Score a version against its predecessor and set REGRESSED if it is worse.

    Only ever downgrades a HEALTHY verdict. IMMATURE and CONTAMINATED are
    terminal: a version we cannot measure is not a version we may revert.
    """
    current, prev = compare_to_predecessor(agent_name, version)
    if current.verdict != VERDICT_HEALTHY or prev is None:
        return current

    if prev.combined is None or prev.verdict in (VERDICT_CONTAMINATED,):
        current.detail += " — predecessor not comparable, no regression test"
        return current

    delta = (current.combined or 0.0) - prev.combined
    if delta < -REGRESSION_MARGIN:
        current.verdict = VERDICT_REGRESSED
        current.detail = (
            f"v{version} scored {current.combined:.3f} against v{version - 1}'s "
            f"{prev.combined:.3f} ({delta:+.3f}), beyond the ±{REGRESSION_MARGIN:.2f} "
            f"noise band at n={MATURITY_N}"
        )
    else:
        current.detail = (
            f"v{version} {current.combined:.3f} vs v{version - 1} "
            f"{prev.combined:.3f} ({delta:+.3f}) — within noise"
        )
    return current
