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
            db.execute(
                "INSERT INTO v3_invariant_violations (kind, cycle_id, ticker, detail) "
                "VALUES (%s, %s, %s, %s)",
                [kind, cycle_id or None, ticker or None, json.dumps(detail, default=str)],
            )
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
