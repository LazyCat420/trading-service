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

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False

#: A ticker that reached the pipeline MUST leave these behind. Anything else is
#: a silent loss of work that was already paid for.
KIND_NO_DESK = "TICKER_ANALYSED_BUT_NO_DESK"
KIND_NO_TRADE_ROW = "DESK_PERSISTED_BUT_NO_TRADE_ROW"
KIND_NO_DECISION = "PIPELINE_COMPLETE_BUT_NO_DECISION"
KIND_FIELD_LOST = "ARTIFACT_FIELD_LOST_ON_PERSIST"


def _ensure_table() -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS v3_invariant_violations (
                    id SERIAL PRIMARY KEY,
                    kind TEXT NOT NULL,
                    cycle_id TEXT,
                    ticker TEXT,
                    detail JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_invariant_kind
                ON v3_invariant_violations (kind, created_at)
            """)
        _TABLE_ENSURED = True
    except Exception as e:
        logger.warning("[Invariants] Failed to ensure table: %s", e)


def record_violation(kind: str, *, ticker: str = "", cycle_id: str = "",
                     **detail: Any) -> str:
    """Record one violation and return its kind unchanged.

    Returning the kind keeps every call site a one-liner, so recording can
    never alter control flow — the failure mode that would turn an observer
    into a trading bug.
    """
    try:
        _ensure_table()
        from app.db.connection import get_db

        with get_db() as db:
            mongo_store.insert_docs('v3_invariant_violations', [{'kind': kind, 'cycle_id': cycle_id or None, 'ticker': ticker or None, 'detail': json.dumps(detail, default=str)}])
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
    """Postconditions for one finished ticker. Returns the violations found.

    Called at the END of the per-ticker pipeline, where "what should exist by
    now" is unambiguous. Reads the database rather than trusting in-memory
    state on purpose: the bugs this catches are precisely the ones where the
    in-memory object looked fine and the write never landed.
    """
    violations: list[str] = []
    if not ticker or not cycle_id:
        return violations

    try:
        from app.db.connection import get_db

        with get_db() as db:
            has_desk = bool(db.execute(
                "SELECT 1 FROM shared_desk WHERE cycle_id = %s AND ticker = %s LIMIT 1",
                [cycle_id, ticker],
            ).fetchone())
            has_row = bool(db.execute(
                "SELECT 1 FROM trade_results WHERE cycle_id = %s AND ticker = %s LIMIT 1",
                [cycle_id, ticker],
            ).fetchone())
    except Exception as e:  # noqa: BLE001 — a probe failure is not a violation
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
#: 150k separates the tournament from every other non-researching agent by ~5x.
#: A board or synthesizer SHOULD deliberate without tools; the tournament doing
#: it at 8x their cost is the budget fact worth surfacing.
COST_NO_RESEARCH_TOKENS = 150_000

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
                  _check_desks_reached_terminal):
        try:
            violations.extend(check(cycle_id) or [])
        except Exception as e:  # noqa: BLE001
            logger.debug("[Invariants] %s failed (non-fatal): %s", check.__name__, e)
    return violations


def _check_universe_coverage(cycle_id: str) -> list[str]:
    """Every analysed ticker must have a price bar this week.

    The 07-20 collapse (2,642 -> 509 distinct tickers) ran for nine days while
    the bot reasoned over stale technicals. It was a *stock vs flow* confusion:
    509 was always the S&P-500 daily refresh set, and 2,642 was a one-off
    backfill draining away. A set-difference catches it on the first cycle.
    """
    from app.db.connection import get_db

    with get_db() as db:
        stale = db.execute(
            """
            SELECT a.ticker FROM analysis_results a
            WHERE a.cycle_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM price_history p
                WHERE p.ticker = a.ticker AND p.date > CURRENT_DATE - 7)
            """,
            [cycle_id],
        ).fetchall()
    if not stale:
        return []
    return [record_violation(
        KIND_UNIVERSE_NOT_COVERED, cycle_id=cycle_id,
        stale_tickers=[r[0] for r in stale][:20], count=len(stale),
    )]


def _check_tool_failure_rates(cycle_id: str) -> list[str]:
    """No tool may sit above the failure ceiling.

    Reads `tool_usage_stats` deliberately: tool_name/success/called_at are the
    columns that table gets RIGHT (see the note in app/tools/registry.py). Its
    broken agent attribution is irrelevant here.
    """
    from datetime import datetime, timezone, timedelta
    from app.db import mongo_store
    rows = []

    if mongo_store.reads_mongo("tool_usage_stats"):
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
            mongo_store.handle_mongo_read_failure("tool_usage_stats", "_check_tool_failure_rates", e)

    if not rows and not mongo_store.reads_mongo("tool_usage_stats"):
        from app.db.connection import get_db
        with get_db() as db:
            rows = db.execute(
                """
                SELECT tool_name, COUNT(*) n,
                       ROUND(100.0 * COUNT(*) FILTER (WHERE NOT success) / COUNT(*)) fail_pct
                FROM tool_usage_stats
                WHERE called_at > NOW() - INTERVAL '24 hours'
                GROUP BY 1 HAVING COUNT(*) >= %s
                """,
                [TOOL_FAIL_MIN_CALLS],
            ).fetchall()
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
    """The HOLD share must not lurch between rolling windows.

    Compares the last DRIFT_WINDOW decisions against the DRIFT_WINDOW before
    them. Deliberately NOT per-cycle: a cycle carries 3-6 decisions, so one
    all-HOLD cycle is 100% and perfectly normal — an absolute threshold would
    fire on almost every healthy cycle.

    The 07-25 -> 07-28 ramp (58% -> 100% HOLD) is exactly this shape, and it
    was the most consequential change of the month.
    """
    from app.db.connection import get_db

    with get_db() as db:
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
    """Flag an agent that spent heavily without calling a single tool.

    Not a bug on its own — a synthesizer SHOULD deliberate. It is a budget fact
    that should be chosen rather than discovered: the tournament consumed 31%
    of all tokens at 242k per call with loops=1.0, and nobody knew until
    someone went looking.
    """
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT agent_name, AVG(token_usage) tok, AVG(loops_used) loops
            FROM v3_agent_telemetry
            WHERE cycle_id = %s
            GROUP BY 1
            """,
            [cycle_id],
        ).fetchall()
    out = []
    for name, tok, loops in rows:
        if tok and int(tok) > COST_NO_RESEARCH_TOKENS and (loops or 0) <= 1.0:
            out.append(record_violation(
                KIND_AGENT_COST_NO_RESEARCH, cycle_id=cycle_id,
                agent=name, tokens=int(tok), avg_loops=float(loops or 0),
            ))
    return out


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
        from app.db.connection import get_db

        with get_db() as db:
            row = mongo_query.find_row('shared_desk', {'cycle_id': cycle_id, 'ticker': ticker}, ['phase'])
        if row and row[0]:
            phase = str(row[0])
    except Exception as e:  # noqa: BLE001 — a probe failure must not lose the record
        logger.debug("[Invariants] %s: phase probe failed (%s)", ticker, e)

    return [record_violation(
        KIND_DESK_ABANDONED, ticker=ticker, cycle_id=cycle_id,
        phase_at_crash=phase,
        error_type=type(error).__name__,
        # asyncio.TimeoutError stringifies to "" — the type is the only signal
        # there, which is why it is recorded separately.
        error=str(error)[:500],
    )]


def _terminal_phases() -> tuple[frozenset[str], str]:
    """The phases a finished desk is allowed to sit in, plus the skip phase.

    Derived from the orchestrator's own transition table — a terminal phase is
    one with nowhere left to go — so adding a phase to `DeskPhase` cannot
    silently leave this check asserting an out-of-date vocabulary. Falls back to
    the literals if the import fails, because a check that raises here is a
    check that reports "no stalls" on every cycle.
    """
    try:
        from app.v3.shared_desk import _VALID_TRANSITIONS, DeskPhase

        terminal = frozenset(
            p.value for p, nxt in _VALID_TRANSITIONS.items() if not nxt
        )
        return (terminal or frozenset({"PM_DONE", "ABORTED"})), DeskPhase.INIT.value
    except Exception:  # noqa: BLE001
        return frozenset({"PM_DONE", "ABORTED"}), "INIT"


def _check_desks_reached_terminal(cycle_id: str) -> list[str]:
    """Every desk this cycle built must have reached a terminal phase.

    THE HOLE THIS CLOSES, and why it existed
    ----------------------------------------
    `HOOD` on 07-30 wrote a desk, advanced it to `DEBATE_DONE`, and stopped:
    no `analysis_results` row, no `trade_results` row, the research and debate
    already paid for and thrown away. **6 of 204 desks over 7 days, in 3 of 48
    cycles** — and invisible to all seven of the checks that existed, for one
    reason worth stating plainly:

        `check_ticker_complete` runs at the END of a pipeline these tickers
        never reached, and every cycle-level check above keys off
        `analysis_results` — the table whose ABSENCE IS THE SYMPTOM.

    Keying observability off the same table the bug corrupts builds a blind
    spot exactly the shape of the bug. So this check reads `shared_desk`, the
    one table a stalled desk is guaranteed to appear in: the desk row is
    written on the way IN, so it exists precisely when everything downstream
    does not.

    CALIBRATION (measured, not guessed — 7 days to 2026-07-30)
    ---------------------------------------------------------
        PM_DONE        176   terminal, healthy       -> silent
        INIT            22   triage skip, healthy    -> silent
        DEBATE_DONE      5   abandoned mid-flight    -> FIRES
        RESEARCH_DONE    1   abandoned mid-flight    -> FIRES

    `INIT` is NOT a stall: the Triage Gate legitimately declines a ticker
    before any phase advances, and 22 healthy skips a week would mute this
    check within days. The distinction is that a stall has *already spent* the
    research budget — which is what makes it worth an alert.

    TWO KINDS OF LOSS, and why one number cannot carry both
    -------------------------------------------------------
    The first live firing (NVDA, `cycle-observe-1785396275`, 2026-07-30 07:28)
    was not the shape this was calibrated on:

        HOOD  DEBATE_DONE    no analysis, no telemetry, no decision
        NVDA  RESEARCH_DONE  analysis + 7 telemetry rows, but NO decision

    NVDA also carried a `pipeline_incomplete` stamp ("Invalid transition:
    RESEARCH_DONE → PM_DONE"), which looks like the 2026-07-29 ORCL fix behaving
    as designed. It was not benign: the root cause was a same-day regression
    (`6a9bd82`) where the DEBATE_ENGINE=3 branch returned early and skipped the
    `tournament_result` whiteboard write — and that write is the CHAIN TRIGGER
    that dispatches the Board. Seven agents ran, the Board never did, and no
    decision was produced.

    So a `pipeline_incomplete` stamp **explains the phase, not the outcome**, and
    "the analysis landed" is not "nothing was lost". A desk can lose:

        its research   — nothing persisted, the spend is simply gone
        its decision   — research persisted, but the thing the pipeline
                         EXISTS to produce never happened

    Reporting one `lost` count flattens those, and flattening is what let a live
    regression read as benign for as long as it took to find the real cause. Each
    desk therefore carries `work_landed`, `decided` and `explained`, and the
    violation carries BOTH `lost_research` and `undecided`. `explained` is
    recorded because it is diagnostic, never because it excuses anything.
    """
    from app.db.connection import get_db

    terminal, skip_phase = _terminal_phases()
    allowed = set(terminal) | {skip_phase}

    with get_db() as db:
        rows = db.execute(
            """
            SELECT d.ticker, d.phase,
                   EXISTS (SELECT 1 FROM analysis_results a
                           WHERE a.cycle_id = d.cycle_id AND a.ticker = d.ticker) landed,
                   EXISTS (SELECT 1 FROM trade_results t
                           WHERE t.cycle_id = d.cycle_id AND t.ticker = d.ticker) decided,
                   (d.desk_data #> '{cycle_metadata,pipeline_incomplete}') IS NOT NULL explained
            FROM shared_desk d
            WHERE d.cycle_id = %s AND d.phase <> ALL(%s)
            ORDER BY d.ticker
            """,
            [cycle_id, sorted(allowed)],
        ).fetchall()
    if not rows:
        return []

    stalled = [
        {"ticker": r[0], "phase": r[1], "work_landed": bool(r[2]),
         "decided": bool(r[3]), "explained": bool(r[4])}
        for r in rows
    ]
    # Two distinct losses, never summed into one alarm.
    lost_research = [s for s in stalled if not s["work_landed"]]
    undecided = [s for s in stalled if s["work_landed"] and not s["decided"]]
    return [record_violation(
        KIND_DESK_STALLED, cycle_id=cycle_id,
        # One row per cycle: a deploy mid-cycle strands every live desk at once.
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
    """The telemetry that answers "which agent researches" must keep working.

    Attribution in `agent_tool_telemetry` was 100% in June and decayed to 0.6%
    by late July, unnoticed, because a decaying observability layer produces no
    symptom other than answers quietly becoming wrong.
    """
    from app.db.connection import get_db

    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) n,
                   COUNT(*) FILTER (WHERE agent_name IS NOT NULL
                                      AND agent_name <> 'unknown') named
            FROM agent_tool_telemetry
            WHERE created_at > NOW() - INTERVAL '24 hours'
            """,
        ).fetchone()
    if not row or not row[0] or int(row[0]) < TOOL_FAIL_MIN_CALLS:
        return []
    pct = 100.0 * int(row[1]) / int(row[0])
    if pct >= ATTRIBUTION_MIN_PCT:
        return []
    return [record_violation(
        KIND_ATTRIBUTION_DECAY, cycle_id=cycle_id,
        attributed_pct=round(pct, 1), calls=int(row[0]),
        floor=ATTRIBUTION_MIN_PCT,
    )]
