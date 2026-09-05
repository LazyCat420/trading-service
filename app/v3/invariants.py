"""Pipeline postconditions — assert that what should have happened, did.

WHY THIS EXISTS
---------------
Every significant defect found in the 2026-07-29 harness audit had the same
shape: **something silently did not happen.** Not a crash, not a wrong answer —
an absence, invisible to every query because the missing thing was the thing
that would have been queried.

    ORCL ran the full ~200s panel, hit an illegal INIT -> PM_DONE transition,
    and lost its entire desk. No shared_desk row, no trade_results row, no
    error surfaced. 5 tickers in 2 days, a rate climbing 0% -> 5% -> 15%.

    The delta and glance triage tiers computed an enforced policy_action and
    wrote it to ZERO rows, so ~13% of analysed tickers were absent from every
    funnel query.

    The tournament's persisted `survivors` dropped `direction`, so 506/506
    stored rows read back as "?" and the bull/bear skew — the strongest
    predictor of board action in the system — was unauditable.

Each was found by hand, weeks late, by someone who happened to run the right
query. Each is a one-line postcondition.

DESIGN
------
Violations are RECORDED, never raised. A postcondition that can abort a cycle
is a new failure mode, and the whole point is to observe the ones that already
exist. `check_*` returns the violations it found so callers may log them; the
row is written either way.

Fails open twice over, matching `_record_gate` in orchestrator.py: the DB write
swallows its own errors, and every check is wrapped, because these run in unit
tests with no database at all.

WHAT THIS IS NOT
----------------
Not a gate, not a guardrail, not a retry. It cannot change a decision. If a
check here starts firing constantly, that is a bug to fix upstream — do not
"resolve" it by loosening the check.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from app.db import mongo_query
from datetime import datetime, timedelta, timezone
from app.db import mongo_store

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False

#: A ticker that reached the pipeline MUST leave these behind. Anything else is
#: a silent loss of work that was already paid for.
KIND_NO_DESK = "TICKER_ANALYSED_BUT_NO_DESK"
KIND_NO_TRADE_ROW = "DESK_PERSISTED_BUT_NO_TRADE_ROW"
KIND_NO_DECISION = "PIPELINE_COMPLETE_BUT_NO_DECISION"
KIND_FIELD_LOST = "ARTIFACT_FIELD_LOST_ON_PERSIST"


def _ensure_table() -> None:
    pass


def record_violation(kind: str, *, ticker: str = "", cycle_id: str = "",
                     **detail: Any) -> str:
    """Record one violation and return its kind unchanged."""
    try:
        from datetime import datetime, timezone
        from app.db import mongo_store

        mongo_store.insert_docs('v3_invariant_violations', [{
            'kind': kind,
            'cycle_id': cycle_id or None,
            'ticker': ticker or None,
            'detail': detail,
            'created_at': datetime.now(timezone.utc),
        }])
        logger.error(
            "[Invariants] %s VIOLATED for %s (cycle=%s): %s",
            kind, ticker or "?", cycle_id or "?", detail,
        )
    except Exception as e:  # noqa: BLE001 — an observer must never break a cycle
        logger.warning("[Invariants] could not record %s (non-fatal): %s", kind, e)
    return kind


def check_ticker_complete(
    *, ticker: str, cycle_id: str, desk: Any = None, result: dict | None = None,
) -> list[str]:
    """Postconditions for one finished ticker. Returns the violations found."""
    violations: list[str] = []
    if not ticker or not cycle_id:
        return violations

    try:
        from app.db import mongo_store

        has_desk = mongo_store.count_docs("shared_desk", {"cycle_id": cycle_id, "ticker": ticker}) > 0
        has_row = mongo_store.count_docs("trade_results", {"cycle_id": cycle_id, "ticker": ticker}) > 0
    except Exception as e:  # noqa: BLE001 — a probe failure is not a violation
        logger.debug("[Invariants] %s complete probe failed: %s", ticker, e)
        return violations
        logger.debug("[Invariants] %s: probe failed (%s) — skipping", ticker, e)
        return violations

    # 1. The pipeline ran; the desk must have survived it. This is the ORCL
    #    case: an illegal phase transition threw while save_desk sat inside the
    #    same try, so 215s of work left nothing behind.
    if not has_desk:
        violations.append(record_violation(
            KIND_NO_DESK, ticker=ticker, cycle_id=cycle_id,
            phase=str(getattr(desk, "phase", None)),
        ))

    # 2. A desk that produced a decision must have a trade_results row. This is
    #    the delta/glance case: policy_action was computed and UPDATEd against
    #    zero rows, so the tier was absent from every funnel query.
    #
    #    Skipped when no decision was made — a Triage-Gate skip is a legitimate
    #    non-decision, and demanding a trade row for it would make this check
    #    fire constantly on healthy cycles, which is how observers get muted.
    elif not has_row and _made_a_decision(desk, result):
        violations.append(record_violation(
            KIND_NO_TRADE_ROW, ticker=ticker, cycle_id=cycle_id,
            action=(result or {}).get("action"),
        ))

    # 3. The pipeline completed but nothing decided anything. Distinct from a
    #    HOLD: a HOLD is a decision. This is the "HOLD @ 0%, persona: unknown"
    #    shape that ORCL exhibited before it lost its desk entirely.
    if has_desk and not _made_a_decision(desk, result):
        violations.append(record_violation(
            KIND_NO_DECISION, ticker=ticker, cycle_id=cycle_id,
            phase=str(getattr(desk, "phase", None)),
            provenance=((result or {}).get("decision_provenance")),
        ))

    return violations


def _made_a_decision(desk: Any, result: dict | None) -> bool:
    """True when some agent actually chose an action.

    `action` present but None is the degraded sentinel — the pipeline tried to
    decide and failed — so it counts as NO decision, which is the distinction
    this whole module exists to make visible.
    """
    for src in (result or {}, getattr(desk, "trade_decision", None) or {},
                getattr(desk, "final_decision", None) or {}):
        if isinstance(src, dict):
            action = str(src.get("action") or "").strip().upper()
            if action in ("BUY", "SELL", "HOLD"):
                return True
    return False


def check_persisted_fields(
    *, artifact_name: str, before: dict, after: dict,
    ticker: str = "", cycle_id: str = "", fields: tuple[str, ...] = (),
) -> list[str]:
    """Assert a round-trip kept the fields that matter.

    The tournament's `survivors` projection dropped `direction` on the way to
    the database — the live dict had it, the stored copy did not, and 506/506
    rows read back as "?" for weeks. A round-trip comparison is the only thing
    that catches a lossy projection, because both halves look correct alone.
    """
    violations: list[str] = []
    for f in fields:
        if f in (before or {}) and (before or {}).get(f) not in (None, "") \
                and (after or {}).get(f) in (None, ""):
            violations.append(record_violation(
                KIND_FIELD_LOST, ticker=ticker, cycle_id=cycle_id,
                artifact=artifact_name, field=f,
            ))
    return violations


# ═══════════════════════════════════════════════════════════════════════════
# CYCLE-LEVEL CHECKS
#
# The per-ticker checks above catch work that vanished. These catch the slower
# failures — the ones that degrade a system over days while every individual
# cycle still looks fine. Detection lag measured during the 2026-07-29 audit,
# all found by hand:
#
#   price universe 2,642 -> 509 tickers          9 days
#   get_sec_filings rejecting 27% of calls       7+ days
#   board HOLD share 58% -> 100%                 4 days
#   tool attribution 100% -> 0.6%                ~3 weeks
#   tournament: 31% of tokens, zero tool calls   never noticed
#   desks stranded mid-pipeline (HOOD, 6/204)    never noticed
#
# THRESHOLDS ARE CALIBRATED AGAINST REAL DATA, not guessed. Each constant below
# records the observed healthy range it was set against, because a check that
# fires on a healthy cycle gets muted, then ignored, then deleted — and the
# muting is silent.
# ═══════════════════════════════════════════════════════════════════════════

KIND_UNIVERSE_NOT_COVERED = "ANALYSED_UNIVERSE_NOT_REFRESHED"
KIND_TOOL_FAILURE_RATE = "TOOL_FAILURE_RATE_CEILING"
KIND_DECISION_DRIFT = "DECISION_DISTRIBUTION_DRIFT"
KIND_AGENT_COST_NO_RESEARCH = "AGENT_BURNS_TOKENS_WITHOUT_RESEARCH"
KIND_CYCLE_NO_RESEARCH = "CYCLE_MADE_NO_TOOL_CALLS"
KIND_ATTRIBUTION_DECAY = "TELEMETRY_ATTRIBUTION_DECAY"
KIND_DESK_STALLED = "DESK_STALLED_MID_PIPELINE"

#: A whole cycle's worth of desks can stall at once — a deploy restarts the
#: container and kills every in-flight desk (see the 3-stall cycle on 07-27).
#: One violation row per cycle, carrying the roster in `detail`, keeps a bad
#: deploy from writing hundreds of rows and burying everything else.
STALL_ROSTER_CAP = 25

#: Healthy tools measured 0-3% failure over 14 days (n>=200 each); the broken
#: one sat at 27%. 15% separates them with room to spare.
TOOL_FAIL_PCT_CEILING = 15
TOOL_FAIL_MIN_CALLS = 20

#: Per-cycle HOLD share is unusable as a signal — cycles carry 3-6 decisions, so
#: one all-HOLD cycle of 4 tickers is 100% and entirely normal.
#:
#: The BASELINE must be long. The 07-25 -> 07-28 ramp was GRADUAL
#: (58 -> 66 -> 73 -> 93 -> 100%), so adjacent 20-vs-20 windows differed by only
#: +5pp and a naive comparison misses the most consequential change of the
#: month. Against a 150-decision trailing baseline it is +28pp.
#:
#: Calibrated by replaying six dates: fires on 07-10 (+29pp, HOLD 36->65%) and
#: 07-29 (+28pp, the ramp); silent on 07-15 (-2), 07-20 (+1), 07-24 (+7) and
#: 07-30 (+1, once the ramp is absorbed into the baseline). Both fires are real
#: regime changes, and every stable period is quiet.
DRIFT_WINDOW = 20
DRIFT_BASELINE = 150
DRIFT_MIN_SHIFT_PCT = 25

#: PER-CALL, never per-cycle: a cycle-wide SUM scales with ticker count, so a
#: threshold on it flags a normal 4-ticker cycle and says nothing about the
#: agent. Measured per call over 7 days:
#:
#:     v3_tournament_debate     242k   loops 1.0   <- 31% of ALL spend
#:     v3_fundamental_analyst   158k   loops 5.8   (researches)
#:     v3_junior_analyst        153k   loops 5.4   (researches)
#:     v3_board_of_directors     31k   loops 1.0
#:     v3_decision_synthesizer   29k   loops 1.0
#:
#: 20k catches ~30k-36k token single-turn runs where transport drops tools
#: (e.g. SGLang emitting DSML as text). Deliberation agents (synthesizer, judge)
#: are exempted up to DELIBERATION_NO_RESEARCH_TOKENS (150k).
COST_NO_RESEARCH_TOKENS = 20_000
DELIBERATION_NO_RESEARCH_TOKENS = 150_000
DELIBERATION_AGENTS = {
    "v3_decision_synthesizer",
    "v3_board_summary",
    "v3_board_consensus",
    "v3_debate_judge",
}

#: A WHOLE CYCLE that called no tool at all.
#:
#: THIS OVERLAPS `COST_NO_RESEARCH_TOKENS` ABOVE, DELIBERATELY. When this check
#: was written that threshold was 150k, so it was blind to the ~40k/1-loop runs
#: below; the same day it was lowered to 20k, which now catches them. Neither
#: check subsumes the other and both are kept:
#:
#:   * per-agent fires when ONE agent loses its tools while the rest of the
#:     cycle researches normally — a partial failure this check cannot see,
#:     because its trigger is zero tool calls across the WHOLE cycle.
#:   * this one fires when the box, not the agent, is broken. It is the only
#:     signal that separates "an outage" from "N independent agent anomalies".
#:
#: A totally broken cycle therefore records both: ~N per-agent rows and one
#: cycle row. That costs rows, not pages — `record_violation` writes to
#: `v3_invariant_violations` and never alerts — and the cycle row is the one
#: that names the outage.
#:
#: MEASURED 2026-09-05 over the 11 days to that date, tool rows per LLM agent
#: run, by cycle:
#:
#:     cycle-v3-1788565070  sglang   117 runs      0 rows   ratio 0.00  <- broken
#:     cycle-v3-1788486930  GLM       20 runs     69 rows   ratio 3.45
#:     cycle-v3-1788198857  ds0731    56 runs    196 rows   ratio 3.50
#:     cycle-v3-1788384492  nemotron  38 runs    131 rows   ratio 3.45
#:     cycle-v3-1788553248  nemotron  12 runs     35 rows   ratio 2.92
#:     cycle-v3-1788532947  nemotron  10 runs     24 rows   ratio 2.40
#:
#: Zero against a healthy floor of 2.40 on the SMALLEST healthy cycle, across
#: three different models on two boxes. There is no threshold to tune: the
#: check is `== 0`.
#:
#: PER-AGENT was measured too and DELIBERATELY NOT USED as the trigger. Zero
#: tool calls is normal for several roles — `v3_regime_engine` is 72-100%
#: zero-tool on every model (its result is cached), `v3_bull_defense` 15% on
#: deepseek, `v3_junior_analyst` 54% on nemotron (that model answers from the
#: briefing, which is a model fact, not an outage). A per-agent rule would fire
#: on healthy cycles, and a check that fires on healthy cycles gets muted, then
#: ignored, then deleted. The per-agent roster rides in `detail` instead.
#:
#: The floor exists only to skip the 3-run aborted cycles: the smallest healthy
#: cycle observed carries 10 LLM runs.
NO_RESEARCH_MIN_RUNS = 8

#: Attribution was 100% in June and decayed to 0.6% by 07-27 without anyone
#: noticing. Below half, the telemetry cannot answer "which agent researches".
ATTRIBUTION_MIN_PCT = 50


def check_cycle_complete(*, cycle_id: str) -> list[str]:
    """Cycle-level postconditions. Returns the violations found.

    Every check is independently wrapped: one failing probe must not suppress
    the other four, which is how a single schema change silently disables a
    whole observability layer.
    """
    violations: list[str] = []
    if not cycle_id:
        return violations
    for check in (_check_universe_coverage, _check_tool_failure_rates,
                  _check_decision_drift, _check_agent_cost, _check_attribution,
                  _check_desks_reached_terminal, _check_cycle_did_research):
        try:
            violations.extend(check(cycle_id) or [])
        except Exception as e:  # noqa: BLE001
            logger.debug("[Invariants] %s failed (non-fatal): %s", check.__name__, e)
    return violations


def _check_universe_coverage(cycle_id: str) -> list[str]:
    """Every analysed ticker must have a price bar this week."""
    from datetime import date, timedelta
    from app.db import mongo_store

    cutoff = date.today() - timedelta(days=7)
    docs = mongo_store.find_docs("analysis_results", {"cycle_id": cycle_id}, projection={"ticker": 1})
    tickers = [d.get("ticker") for d in docs if d.get("ticker")]
    stale = []
    for t in tickers:
        has_recent = mongo_store.count_docs("price_history", {"ticker": t, "date": {"$gt": cutoff}}) > 0
        if not has_recent:
            stale.append(t)

    if not stale:
        return []
    return [record_violation(
        KIND_UNIVERSE_NOT_COVERED, cycle_id=cycle_id,
        stale_tickers=stale[:20], count=len(stale),
    )]


def _check_tool_failure_rates(cycle_id: str) -> list[str]:
    """No tool may sit above the failure ceiling."""
    from datetime import datetime, timezone, timedelta
    from app.db import mongo_store
    rows = []

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        pipeline = [
            {"$match": {"called_at": {"$gte": since}}},
            {"$group": {
                "_id": "$tool_name",
                "n": {"$sum": 1},
                "fails": {"$sum": {"$cond": ["$success", 0, 1]}},
            }},
            {"$match": {"n": {"$gte": TOOL_FAIL_MIN_CALLS}}},
            {"$project": {
                "tool_name": "$_id",
                "n": 1,
                "fail_pct": {"$round": [{"$multiply": [100.0, {"$divide": ["$fails", "$n"]}]}]},
            }}
        ]
        mongo_rows = mongo_store.aggregate("tool_usage_stats", pipeline)
        rows = [(r["tool_name"], r["n"], r["fail_pct"]) for r in mongo_rows]
    except Exception as e:
        logger.warning("[Invariants] tool failure check failed: %s", e)

    out = []
    for name, n, pct in rows:
        if pct is not None and float(pct) > TOOL_FAIL_PCT_CEILING:
            out.append(record_violation(
                KIND_TOOL_FAILURE_RATE, cycle_id=cycle_id,
                tool=name, calls=int(n), failure_pct=float(pct),
                ceiling=TOOL_FAIL_PCT_CEILING,
            ))
    return out


def _check_decision_drift(cycle_id: str) -> list[str]:
    """The HOLD share must not lurch between rolling windows."""
    from app.db import mongo_query

    rows = mongo_query.find_rows('trade_results', {'action': {'$ne': None}}, ['action'], sort=[('created_at', -1)], limit=DRIFT_WINDOW + DRIFT_BASELINE)
    if len(rows) < DRIFT_WINDOW + DRIFT_BASELINE:
        return []  # not enough history to compare — silence, not a guess
    recent = [r[0] for r in rows[:DRIFT_WINDOW]]
    prior = [r[0] for r in rows[DRIFT_WINDOW:DRIFT_WINDOW + DRIFT_BASELINE]]
    r_pct = 100.0 * sum(1 for a in recent if a == "HOLD") / len(recent)
    p_pct = 100.0 * sum(1 for a in prior if a == "HOLD") / len(prior)
    if abs(r_pct - p_pct) < DRIFT_MIN_SHIFT_PCT:
        return []
    return [record_violation(
        KIND_DECISION_DRIFT, cycle_id=cycle_id,
        hold_pct_recent=round(r_pct, 1), hold_pct_prior=round(p_pct, 1),
        shift=round(r_pct - p_pct, 1), window=DRIFT_WINDOW,
        baseline=DRIFT_BASELINE,
    )]


def _check_agent_cost(cycle_id: str) -> list[str]:
    """Flag an agent that spent heavily without calling a single tool."""
    from app.db import mongo_store

    pipeline = [
        {"$match": {"cycle_id": cycle_id}},
        {"$group": {
            "_id": "$agent_name",
            "tok": {"$avg": "$token_usage"},
            "loops": {"$avg": "$loops_used"},
        }}
    ]
    docs = mongo_store.aggregate("v3_agent_telemetry", pipeline)
    out = []
    for d in docs:
        name = d.get("_id")
        tok = d.get("tok")
        loops = d.get("loops")
        threshold = DELIBERATION_NO_RESEARCH_TOKENS if name in DELIBERATION_AGENTS else COST_NO_RESEARCH_TOKENS
        if tok and int(tok) > threshold and (loops or 0) <= 1.0:
            out.append(record_violation(
                KIND_AGENT_COST_NO_RESEARCH, cycle_id=cycle_id,
                agent=name, tokens=int(tok), avg_loops=float(loops or 0),
            ))
    return out


#: Agents whose result is cached or whose role does not require a tool. Counted
#: in the roster but never the reason the cycle is flagged.
_NON_RESEARCHING_AGENTS = frozenset({
    "contradiction_shadow",
    "v3_regime_engine",
})


def _check_cycle_did_research(cycle_id: str) -> list[str]:
    """The whole cycle called no tool. The desk decided on the briefing alone.

    THE FAILURE THIS EXISTS FOR (2026-09-04/05, cycle-v3-1788565070). The Gold
    Spark was serving DeepSeek V4 through SGLang launched without
    `--tool-call-parser deepseekv4`, so every agent's tool call came back as
    TEXT inside the message content. Nothing executed, nothing was written to
    `agent_tool_telemetry`, and the tool-less repair pass then produced an
    artifact out of the pre-collected briefing — which scored 80-88 on
    `quality_scorer` and was recorded SUCCESS. 117 agent runs, 12 HOLD
    decisions, and every existing instrument read healthy:

      * `_check_agent_cost` needed >150k tokens at the time; these runs were
        ~40k. (It is 20k as of 2026-09-04 and would now fire per agent — but
        only on the total-outage shape, not on one agent losing its tools.)
      * `cycle_audit.check_tool_failures` grades the rows it finds and returns
        INFO "no tool calls yet" when it finds none.
      * `llm_audit`'s availability counts a repaired run as a success.

    An absence cannot be graded by a check that only grades what is present.
    This one asks for the absence directly.
    """
    from app.db import mongo_store

    runs = mongo_store.find_docs(
        "v3_agent_telemetry",
        {"cycle_id": cycle_id},
        projection={"agent_name": 1, "ticker": 1, "loops_used": 1, "outcome": 1},
    )
    llm_runs = [
        r for r in runs
        if (r.get("agent_name") or "") not in _NON_RESEARCHING_AGENTS
    ]
    if len(llm_runs) < NO_RESEARCH_MIN_RUNS:
        return []

    tool_calls = mongo_store.count_docs("agent_tool_telemetry", {"cycle_id": cycle_id})
    if tool_calls:
        return []

    # The roster names WHO went without, so the report does not send a reader
    # back to the database to find out whether this was one agent or all of
    # them. Sorted and capped for the same reason STALL_ROSTER_CAP exists.
    roster = sorted({str(r.get("agent_name") or "?") for r in llm_runs})
    return [record_violation(
        KIND_CYCLE_NO_RESEARCH, cycle_id=cycle_id,
        llm_runs=len(llm_runs), tool_calls=0,
        agents=roster[:STALL_ROSTER_CAP],
        tickers=len({str(r.get("ticker") or "") for r in llm_runs if r.get("ticker")}),
    )]


KIND_DESK_ABANDONED = "DESK_ABANDONED_MID_PIPELINE"


def record_ticker_crash(*, ticker: str, cycle_id: str, error: BaseException) -> list[str]:
    """The per-ticker pipeline RAISED. Name the exception while we still have it.

    `pipeline_service` gathers ticker tasks with `return_exceptions=True` so one
    bad ticker cannot kill the batch — correct, and the reason a stall is
    per-ticker rather than per-cycle (HOOD died at `DEBATE_DONE` while EXLS and
    CRH finished 12 minutes later in the same cycle). But the exception was only
    ever *logged*: no table recorded it, so "why did this desk stop?" was
    answerable only from container logs that rotate.

    Deliberately does NOT stamp the desk terminal. Setting `ABORTED` would make
    `DESK_STALLED_MID_PIPELINE` go silent — the loss would vanish behind a
    detector reporting health, which is the exact failure this module exists to
    prevent. Leaving the phase alone keeps two independent observers: this one
    fires at the moment of failure and names the cause, and the cycle-level
    stall check still fires at cycle end as a backstop. Neither mutes the other,
    and the surviving phase is the only record of where the pipeline stopped.

    ALSO deliberately does NOT call `check_ticker_complete` here, though the
    crash path is the one place those four per-ticker checks never run (they sit
    in the straight-line flow near the end of `run_v3_pipeline`, so a raise skips
    them). Measured what it would actually emit, rather than assuming a gap:

        desk exists, crashed  -> PIPELINE_COMPLETE_BUT_NO_DECISION
        crashed before desk   -> TICKER_ANALYSED_BUT_NO_DESK

    The first is a row whose NAME asserts something false — the pipeline did not
    complete, it died — and the second is strictly less informative than the
    `phase_at_crash="NO_DESK"` this function already records alongside the
    exception type. Both would be duplicates that read as independent
    corroboration. Two observers are only worth having when they can disagree.
    """
    if not ticker or not cycle_id:
        return []

    phase = "NO_DESK"
    try:
        from app.db import mongo_query

        row = mongo_query.find_row('shared_desk', {'cycle_id': cycle_id, 'ticker': ticker}, ['phase'])
        if row and row[0]:
            phase = str(row[0])
    except Exception as e:  # noqa: BLE001 — a probe failure must not lose the record
        logger.debug("[Invariants] %s: phase probe failed (%s)", ticker, e)

    return [record_violation(
        KIND_DESK_ABANDONED, ticker=ticker, cycle_id=cycle_id,
        phase_at_crash=phase,
        error_type=type(error).__name__,
        error=str(error)[:500],
    )]


def _terminal_phases() -> tuple[frozenset[str], str]:
    """The phases a finished desk is allowed to sit in, plus the skip phase."""
    try:
        from app.v3.shared_desk import _VALID_TRANSITIONS, DeskPhase

        terminal = frozenset(
            p.value for p, nxt in _VALID_TRANSITIONS.items() if not nxt
        )
        return (terminal or frozenset({"PM_DONE", "ABORTED"})), DeskPhase.INIT.value
    except Exception:  # noqa: BLE001
        return frozenset({"PM_DONE", "ABORTED"}), "INIT"


def _check_desks_reached_terminal(cycle_id: str) -> list[str]:
    """Every desk this cycle built must have reached a terminal phase."""
    from app.db import mongo_store

    terminal, skip_phase = _terminal_phases()
    allowed = set(terminal) | {skip_phase}

    docs = mongo_store.find_docs(
        "shared_desk",
        {"cycle_id": cycle_id, "phase": {"$nin": list(allowed)}},
        sort=[("ticker", 1)],
    )
    if not docs:
        return []

    stalled = []
    for d in docs:
        tk = d.get("ticker")
        ph = d.get("phase")
        landed = mongo_store.count_docs("analysis_results", {"cycle_id": cycle_id, "ticker": tk}) > 0
        decided = mongo_store.count_docs("trade_results", {"cycle_id": cycle_id, "ticker": tk}) > 0
        desk_data = d.get("desk_data") or {}
        explained = (desk_data.get("cycle_metadata") or {}).get("pipeline_incomplete") is not None
        stalled.append({
            "ticker": tk, "phase": ph, "work_landed": landed,
            "decided": decided, "explained": explained,
        })

    lost_research = [s for s in stalled if not s["work_landed"]]
    undecided = [s for s in stalled if s["work_landed"] and not s["decided"]]
    return [record_violation(
        KIND_DESK_STALLED, cycle_id=cycle_id,
        ticker=(stalled[0]["ticker"] if len(stalled) == 1 else ""),
        count=len(stalled),
        lost_research=len(lost_research),
        lost_research_tickers=[s["ticker"] for s in lost_research][:STALL_ROSTER_CAP],
        undecided=len(undecided),
        undecided_tickers=[s["ticker"] for s in undecided][:STALL_ROSTER_CAP],
        stalled=stalled[:STALL_ROSTER_CAP],
        truncated=max(0, len(stalled) - STALL_ROSTER_CAP),
        terminal_phases=sorted(terminal),
    )]


def _check_attribution(cycle_id: str) -> list[str]:
    """The telemetry that answers "which agent researches" must keep working."""
    from datetime import datetime, timezone, timedelta
    from app.db import mongo_store

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total_calls = mongo_store.count_docs("agent_tool_telemetry", {"created_at": {"$gt": since}})
    if total_calls < TOOL_FAIL_MIN_CALLS:
        return []

    named_calls = mongo_store.count_docs(
        "agent_tool_telemetry",
        {
            "created_at": {"$gt": since},
            "agent_name": {"$nin": [None, "", "unknown"]},
        },
    )
    pct = 100.0 * named_calls / total_calls
    if pct >= ATTRIBUTION_MIN_PCT:
        return []
    return [record_violation(
        KIND_ATTRIBUTION_DECAY, cycle_id=cycle_id,
        attributed_pct=round(pct, 1), calls=total_calls,
        floor=ATTRIBUTION_MIN_PCT,
    )]
