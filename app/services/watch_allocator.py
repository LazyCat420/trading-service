"""watch_allocator — the I/O half. Assembles live state, runs triage, logs.

`watch_triage` and `watch_policy` are pure by design so they can be replayed
against frozen fixtures. This module is the part that touches the world: it
reads positions, prior analyses, calendars and the event ledger, builds the
`TriageInputs`, and writes what happened to `watch_triage_log`.

SHADOW IS THE DEFAULT AND IT MUST BE FREE
-----------------------------------------
In shadow mode the desk fires exactly as it did before and this module only
observes. That is worth stating twice, because a "shadow" layer that can raise,
block, or slow the path it shadows is not a shadow — it is a deploy. Every
public function here is wrapped so that a failure degrades to "no shadow record
written", never to a changed decision.

WHAT GETS LOGGED
----------------
One document per CANDIDATE, not per wake — including the candidates the current
`break`-after-one-wake path discards without trace. That is a strict superset of
what fires today, which is what makes the shadow data able to answer "what would
the allocator have chosen" rather than only "did it agree with what happened".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.db import mongo_query, mongo_store
from app.services import watch_schema
from app.services.watch_catalysts import catalyst_calendar
from app.services.watch_policy import PolicyInputs, apply_policy
from app.services.watch_triage import Evidence, TriageInputs, triage
from app.utils.tz import ensure_aware

logger = logging.getLogger(__name__)

TRIAGE_LOG = "watch_triage_log"

MODE_OFF, MODE_SHADOW, MODE_ENFORCE = 0, 1, 2


def get_mode() -> int:
    """0 off / 1 shadow / 2 enforce. An unreadable switch means SHADOW.

    Not "enforce": a parameter store that cannot be read is not evidence that
    gating is safe. Not "off" either — shadow costs nothing and losing the
    telemetry is how a saturated desk goes unnoticed for 21 days.
    """
    try:
        from app.services.parameter_store import get_param

        return max(MODE_OFF, min(MODE_ENFORCE, int(get_param("WATCH_TRIAGE_MODE"))))
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchAllocator] WATCH_TRIAGE_MODE unreadable (%s) — shadow", e)
        return MODE_SHADOW


def _knobs() -> dict:
    from app.services.parameter_store import get_param

    def _i(name, default):
        try:
            return int(get_param(name))
        except Exception:  # noqa: BLE001
            return default

    return {
        "evidence_max_age_h": _i("WATCH_EVIDENCE_MAX_AGE_H", 36),
        "min_reanalysis_h": _i("WATCH_MIN_REANALYSIS_H", 12),
        "max_analyses_per_week": _i("WATCH_MAX_ANALYSES_PER_WEEK", 3),
    }


# ─── Live state readers. Every one fails soft to a NEUTRAL value. ───────────
def _last_analysis_at(ticker: str) -> datetime | None:
    """When this ticker was last actually analysed.

    Reads `analysis_results`, not `watch_events`: a wake that produced no
    analysis is not a look at the ticker. Measured 2026-09-06, 8 of the last 30
    wakes produced zero `analysis_results` rows (7 cycles errored, 1 stopped) —
    anchoring the cadence on the wake would have counted those as looks and
    suppressed the retry that was actually warranted.
    """
    try:
        row = mongo_query.find_row(
            "analysis_results", {"ticker": ticker}, ["created_at"],
            sort=[("created_at", -1)],
        )
        return ensure_aware(row[0]) if row and row[0] else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[WatchAllocator] last-analysis read failed for %s: %s", ticker, e)
        return None


def _analyses_this_week(ticker: str, now: datetime) -> int:
    try:
        rows = mongo_query.find_rows(
            "analysis_results",
            {"ticker": ticker, "created_at": {"$gte": now - timedelta(days=7)}},
            ["id"], limit=50,
        )
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.debug("[WatchAllocator] weekly-count read failed for %s: %s", ticker, e)
        return 0


def _position(ticker: str) -> tuple[float, float, float | None]:
    """(qty, portfolio_weight, unrealized_pct). Zeros when unknown.

    A failed read returns "no position", which UNDER-states urgency. That is the
    deliberate direction: over-stating it on a read failure would make a broken
    instrument look like a risk event and wake cycles on nothing.
    """
    try:
        from app.services.bot_manager import get_active_bot_id

        bot_id = get_active_bot_id()
        row = mongo_query.find_row(
            "positions", {"bot_id": bot_id, "ticker": ticker, "qty": {"$gt": 0}},
            ["qty", "avg_price", "current_price", "market_value"],
        )
        if not row:
            return 0.0, 0.0, None
        qty = float(row[0] or 0)
        avg = float(row[1] or 0) or None
        cur = float(row[2] or 0) or None
        mv = float(row[3] or 0)
        unrl = ((cur - avg) / avg) if (avg and cur) else None

        total = 0.0
        for r in mongo_query.find_rows(
                "positions", {"bot_id": bot_id, "qty": {"$gt": 0}}, ["market_value"]):
            try:
                total += float(r[0] or 0)
            except (TypeError, ValueError):
                continue
        weight = (mv / total) if total > 0 else 0.0
        return qty, weight, unrl
    except Exception as e:  # noqa: BLE001
        logger.debug("[WatchAllocator] position read failed for %s: %s", ticker, e)
        return 0.0, 0.0, None


def _seen_event_keys(ticker: str, now: datetime, days: int = 14) -> frozenset:
    try:
        rows = mongo_query.find_rows(
            TRIAGE_LOG,
            {"ticker": ticker, "created_at": {"$gte": now - timedelta(days=days)},
             "fired": True},
            ["event_key"], limit=200,
        )
        return frozenset(r[0] for r in rows if r and r[0])
    except Exception:  # noqa: BLE001
        return frozenset()


def evidence_from_trip(watch: dict, trig: dict, detail: str, value, ctx: dict | None,
                       now: datetime) -> Evidence:
    """Turn the desk's existing (trigger, detail, value) trip into typed Evidence.

    `observed_at` is the WORLD's timestamp wherever one exists. For a news trip
    that is the article's `collected_at`, recovered from the ctx the desk
    already fetched; for a price trip the observation is now. Falling back to
    `now` for a headline would make every article permanently fresh and the
    staleness gate a decoration.
    """
    typ = (trig or {}).get("type", "")
    if typ == "news":
        event = (ctx or {}).get("news_event")
        if isinstance(event, dict):
            return Evidence(kind="news", text=event.get("title") or detail or "",
                            observed_at=ensure_aware(event.get("observed_at")),
                            source=event.get("source") or "news_store", trigger_type=typ)
        observed, source = None, "detected"
        # The desk's detail string embeds the headline in curly quotes. Prefer
        # the ctx row, which carries the real collection time.
        title = ""
        m = (detail or "")
        if "“" in m and "”" in m:
            title = m[m.index("“") + 1:m.rindex("”")]
        for t, ca in (ctx or {}).get("news", []) or []:
            if t and title and t.startswith(title[:60]):
                observed = ensure_aware(ca)
                break
        return Evidence(kind="news", text=title or (detail or ""),
                        observed_at=observed, source=source, trigger_type=typ)
    if typ == "staleness":
        return Evidence(kind="clock", text=detail or "", observed_at=now,
                        source="clock", value=value, trigger_type=typ)
    return Evidence(kind="price", text=detail or "", observed_at=now,
                    source="price", value=value, trigger_type=typ)


def build_inputs(*, watch: dict, trig: dict, detail: str, value, ctx: dict | None,
                 now: datetime, market_open: bool, budget_left: int,
                 budget_total: int) -> TriageInputs:
    ticker = watch.get("ticker", "")
    k = _knobs()
    qty, weight, unrl = _position(ticker)
    return TriageInputs(
        ticker=ticker,
        watch=watch,
        evidence=evidence_from_trip(watch, trig, detail, value, ctx, now),
        now=now,
        market_open=market_open,
        calendar=catalyst_calendar(ticker, now),
        held_qty=qty, position_weight=weight, unrealized_pct=unrl,
        last_analysis_at=_last_analysis_at(ticker),
        analyses_this_week=_analyses_this_week(ticker, now),
        seen_event_keys=_seen_event_keys(ticker, now),
        budget_left=budget_left, budget_total=budget_total,
        evidence_max_age_h=k["evidence_max_age_h"],
        min_reanalysis_h=k["min_reanalysis_h"],
        max_analyses_per_week=k["max_analyses_per_week"],
    )


def assess(*, watch: dict, trig: dict, detail: str, value, ctx: dict | None,
           now: datetime, market_open: bool, budget_left: int,
           budget_total: int) -> tuple:
    """(verdict, decision, inputs) for one candidate. Never raises.

    Returns (None, None, None) on any failure, and every caller treats that as
    "no opinion" — so a broken allocator falls back to the behaviour that
    shipped rather than to no behaviour.
    """
    try:
        inp = build_inputs(watch=watch, trig=trig, detail=detail, value=value,
                           ctx=ctx, now=now, market_open=market_open,
                           budget_left=budget_left, budget_total=budget_total)
        verdict = triage(inp)
        decision = apply_policy(PolicyInputs(
            now=now, verdict=verdict, planner_result=None,
            budget_left=budget_left, budget_total=budget_total,
            min_reanalysis_h=inp.min_reanalysis_h,
            analyses_this_week=inp.analyses_this_week,
            max_analyses_per_week=inp.max_analyses_per_week,
            last_analysis_at=inp.last_analysis_at,
            watch_expires_at=ensure_aware(watch.get("expiry_at")),
            market_open=market_open,
            seen_event_keys=inp.seen_event_keys,
        ))
        return verdict, decision, inp
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchAllocator] assess failed for %s: %s",
                       (watch or {}).get("ticker"), e)
        return None, None, None


def log_candidate(*, watch: dict, trig: dict, verdict, decision, inputs,
                  now: datetime, fired: bool, cycle_id: str | None,
                  live_mode: int) -> None:
    """Write one shadow/enforce record. Never raises.

    `fired` is what the desk ACTUALLY did; `decision.action` is what the
    allocator WOULD do. Keeping both on the same row is the whole point — a log
    that recorded only the allocator's opinion could not be scored against
    reality, and a log that recorded only reality would say nothing new.
    """
    if verdict is None or decision is None:
        return
    try:
        rc = watch_schema.get_resolution_condition(watch)
        doc = {
            "id": f"wtl-{uuid.uuid4().hex[:12]}",
            "created_at": now,
            "mode": live_mode,
            "ticker": watch.get("ticker"),
            "watch_id": watch.get("id"),
            "schema_version": int(watch.get("schema_version") or 0),
            "has_resolution_condition": rc is not None,
            "open_question": (rc or {}).get("open_question", "")[:500] or None,
            "trigger_type": (trig or {}).get("type"),
            "event_key": verdict.event_key,
            # ── why it woke ──
            "evidence": (verdict.detail or {}).get("evidence"),
            "catalyst": (verdict.detail or {}).get("catalyst"),
            # ── why it ran or deferred ──
            "eligible": bool(verdict.eligible),
            "reject_reason": verdict.reject_reason,
            "score": verdict.score,
            "score_floor": (verdict.detail or {}).get("score_floor"),
            "components": verdict.components,
            "would_action": decision.action,
            "would_reason": decision.reason,
            "clamps": decision.clamps,
            # ── what actually happened ──
            "fired": bool(fired),
            "cycle_id": cycle_id,
            # Filled in later by scripts/watch_allocator_outcomes.py once the
            # cycle has finished. Absent, not zero: a field with no producer
            # that defaults to 0 reports a confident "nothing changed".
            "outcome": None,
            "budget_left": getattr(inputs, "budget_left", None),
            "analyses_this_week": getattr(inputs, "analyses_this_week", None),
            "detail": json.dumps(verdict.detail, default=str)[:4000],
        }
        mongo_store.insert_docs(TRIAGE_LOG, [doc])
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchAllocator] triage log write failed: %s", e)
