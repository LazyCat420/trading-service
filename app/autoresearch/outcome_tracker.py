"""
Outcome Tracker — Records pipeline decisions and resolves them against actual prices.

This closes the feedback loop for Decision Quality scoring:
1. record_cycle_decisions()  — called after each cycle, captures BUY/SELL/HOLD + entry price
2. resolve_pending_outcomes() — called before scoring, checks unresolved decisions against current prices

HOLD decisions ARE tracked (since 2026-07-19). They were skipped before, which
threw away ~75% of the fleet's verdicts (249 of 332 in a typical week) and
starved every outcome-based metric of samples. A HOLD is a checkable claim —
"no meaningful move over the horizon" — and it resolves against the same ±1%
band the directional calls use. HOLDs get their own outcome labels
(HOLD_CORRECT / HOLD_MISS) so the directional win-rate cohort (WIN/LOSS) is
untouched: folding "price stayed flat" into win rate would let low volatility
masquerade as directional skill. HOLD outcomes feed calibration and a separate
hold-accuracy metric in decision_audit instead.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# How many days to wait before resolving a decision outcome
RESOLVE_AFTER_DAYS = 7
# PnL thresholds for WIN/LOSS/FLAT classification
WIN_THRESHOLD_PCT = 1.0
LOSS_THRESHOLD_PCT = -1.0


def _classify(action: str, pnl_pct: float) -> str:
    """Map a signed pnl move to an outcome label for the given action.

    Directional calls keep the historical WIN/LOSS/FLAT taxonomy. HOLD claims
    get distinct labels on purpose: every existing consumer filters on
    WIN/LOSS, so HOLD rows are invisible to them unless they opt in.
    """
    if action == "HOLD":
        return "HOLD_CORRECT" if abs(pnl_pct) < WIN_THRESHOLD_PCT else "HOLD_MISS"
    if pnl_pct >= WIN_THRESHOLD_PCT:
        return "WIN"
    if pnl_pct <= LOSS_THRESHOLD_PCT:
        return "LOSS"
    return "FLAT"


def _is_unscoreable(confidence, result: dict) -> bool:
    """True when this artifact records a FAILURE to decide, not a decision.

    Three independent tells, because each alone has been wrong before:

    1. `confidence == 0` — the sentinel every degraded path writes. Real scores
       bottom out at 15 in the recorded history, so 0 is unambiguous. Checked
       first because it catches artifacts whose text has been lost.
    2. The orchestrator's own `_is_degraded_decision` — degraded provenance or
       a null action. Reused rather than reimplemented so the two definitions
       cannot drift; a decision the policy gate refuses to execute is exactly
       a decision the scorer must refuse to grade.
    3. The failure text itself, for rows whose provenance was never stamped.

    Deliberately NOT keyed on low confidence generally: a confident-but-wrong
    call is real signal and the calibration depends on keeping it.
    """
    if confidence is None:
        return True
    try:
        if float(confidence) == 0.0:
            return True
    except (TypeError, ValueError):
        return True

    if not isinstance(result, dict):
        return True

    try:
        from app.v3.orchestrator import _is_degraded_decision

        if _is_degraded_decision(result):
            return True
    except Exception:  # noqa: BLE001 — never block recording on an import
        pass

    thesis = str(result.get("thesis_summary") or result.get("reasoning") or "")
    return "PIPELINE FAILURE" in thesis or "Failed to parse thesis" in thesis


def resolve_overridden_from(db, cycle_id: str, ticker: str, action: str):
    """Label a decision that something overruled, or None if nothing did.

    Two mechanisms overrule a decision, and they leave different traces.

    1. The SYNTHESIZER downgrade. Execution reads `trade_decision or
       final_decision`, so the synthesizer wins — over 7 days it turned 21 of
       41 Board BUYs into HOLDs. Read from shared_desk rather than
       analysis_results, which carries only the surviving action.

    2. The POLICY GATE refusal. A blocked BUY leaves shared_desk.final_decision
       AND analysis_results.action both reading 'BUY', so check 1 is false and
       the row lands unlabelled. It is then graded WIN or LOSS as though the
       trade had been taken, and override_scorecard() counts it in `kept_buys`:
       the desk gets credit for keeping a trade the gate actually refused.
       Measured 2026-07-31: 17 BUY + 2 SELL blocks, all NULL.

    The row deliberately keeps `action='BUY'`. Its P&L is the counterfactual —
    what the declined trade would have returned — and that is exactly how the
    floor gets back-tested. What the label changes is that it is distinguishable.

    The gate is read from TWO tables because `trade_results` is not written for
    every blocked decision. Measured 2026-08-06 over the 25 blocks on record:
    8 had no `trade_results` row at all, so `policy_action` came back NULL and
    the block was recorded as a trade the desk kept — 5 of them already graded
    (ASML x2, COF, ASIC WIN; CRH LOSS). `v3_guardrail_firings` is written by
    the guardrail itself on the same path that refuses the trade, and carried
    a row for all 25. Either source naming a block is a block: a missing row in
    one table must not read as permission.

    Never raises. An unlabelled row is a gap; a lost row is a lost outcome.
    """
    try:
        desk_row = db.execute(
            "SELECT desk_data->'final_decision'->>'action' "
            "FROM shared_desk WHERE cycle_id = %s AND ticker = %s LIMIT 1",
            [cycle_id, ticker],
        ).fetchone()
        board_action = desk_row[0] if desk_row else None
        if board_action and board_action != action:
            return board_action
    except Exception as e:  # noqa: BLE001 — provenance, never blocks
        logger.debug("[OUTCOME] %s: override lookup failed: %s", ticker, e)

    if _was_blocked_by_policy(db, cycle_id, ticker):
        # Matches the synthesizer path's meaning: the action that did NOT
        # survive.
        return action
    return None


def _was_blocked_by_policy(db, cycle_id: str, ticker: str) -> bool:
    """True when either record of the policy gate says it refused this trade.

    Each lookup is independently fault-tolerant: one table being unreadable
    must not suppress the other's evidence.
    """
    try:
        gate_row = db.execute(
            "SELECT policy_action FROM trade_results "
            "WHERE cycle_id = %s AND ticker = %s LIMIT 1",
            [cycle_id, ticker],
        ).fetchone()
        policy_action = gate_row[0] if gate_row else None
        if policy_action and policy_action.startswith("HOLD_POLICY_BLOCKED"):
            return True
    except Exception as e:  # noqa: BLE001 — provenance, never blocks
        logger.debug("[OUTCOME] %s: policy-gate lookup failed: %s", ticker, e)

    try:
        fired = db.execute(
            "SELECT 1 FROM v3_guardrail_firings "
            "WHERE cycle_id = %s AND ticker = %s "
            "AND guardrail LIKE 'HOLD_POLICY_BLOCKED%%' LIMIT 1",
            [cycle_id, ticker],
        ).fetchone()
        if fired:
            return True
    except Exception as e:  # noqa: BLE001 — provenance, never blocks
        logger.debug("[OUTCOME] %s: guardrail lookup failed: %s", ticker, e)

    return False


def record_cycle_decisions(cycle_id: str, cycle_summary: dict) -> int:
    """
    After a cycle completes, read analysis_results for that cycle and insert
    unresolved decision_outcomes for every BUY/SELL/HOLD decision. HOLDs are
    tracked as "no meaningful move" claims (see module docstring).
    """
    recorded = 0
    skipped_degraded = 0

    # Which skill docs governed this cycle's decisions. Captured ONCE per
    # cycle, before the row loop, so every ticker in the cycle carries the same
    # snapshot — the docs did not change mid-cycle, and re-reading per row would
    # invite a TTL refresh to split one cycle across two versions.
    #
    # Serialized here rather than passed as a dict: psycopg adapts dict -> hstore
    # by default, not JSONB, which fails on a JSONB column.
    skill_versions = None
    try:
        import json as _json

        from app.autoresearch.skill_loader import active_skill_versions

        snapshot = active_skill_versions()
        skill_versions = _json.dumps(snapshot) if snapshot else None
    except Exception as e:  # noqa: BLE001 — provenance, never blocks recording
        logger.debug("[OUTCOME] skill version snapshot failed: %s", e)

    try:
        with get_db() as db:
            # Which model served each agent on each desk this cycle — the
            # per-model leaderboard's join key. Same snapshot rationale as
            # skill_versions, but per TICKER rather than per cycle: routing is
            # per-agent and a cycle can straddle a gateway-side model swap, so
            # stamping one cycle-wide model would average over the boundary.
            # ORDER BY created_at makes the dict's last-write the latest run
            # (retries overwrite their first attempt).
            models_by_ticker: dict = {}
            import json as _mjson
            try:
                m_rows = db.execute(
                    """
                    SELECT ticker, agent_name, model_used
                    FROM v3_agent_telemetry
                    WHERE cycle_id = %s AND model_used IS NOT NULL
                    ORDER BY created_at
                    """,
                    [cycle_id],
                ).fetchall()
                for _t, _agent, _model in m_rows:
                    models_by_ticker.setdefault(_t, {})[_agent] = _model
            except Exception as e:  # noqa: BLE001 — provenance, never blocks
                logger.debug("[OUTCOME] model snapshot failed: %s", e)

            # The entry price is read through the same one-vendor path as the
            # exit price below. Reading them independently is what let a vendor
            # SPREAD become P&L: price_history carries both a yfinance and a
            # polygon print for ~19% of scored tickers, the two disagree by a
            # mean 20.05% (ALLY 1.11%, DRIP 718%), and an unfiltered
            # `ORDER BY date DESC LIMIT 1` picks between them non-
            # deterministically. See app/quant/returns.py.
            rows = db.execute(
                """
                SELECT ar.ticker, ar.confidence, NULL::double precision AS entry_price,
                       ar.result_json
                FROM analysis_results ar
                WHERE ar.cycle_id = %s AND ar.confidence IS NOT NULL
                """,
                [cycle_id],
            ).fetchall()
            from app.quant.returns import latest_close

            rows = [
                (ticker, confidence, latest_close(ticker), result_json)
                for ticker, confidence, _entry, result_json in rows
            ]

            for ticker, confidence, entry_price, result_json in rows:
                # Extract action from result_json
                import json
                try:
                    result = json.loads(result_json) if isinstance(result_json, str) else (result_json or {})
                except (json.JSONDecodeError, TypeError):
                    result = {}

                action = result.get("action", "HOLD")

                # A pipeline failure is not a decision, and must never be
                # scored as a trade. 2026-07-27: 363 of 2,215 rows here were
                # confidence=0 artifacts — 145 whose thesis text reads
                # "PIPELINE FAILURE (EMPTY_SIGNAL): Thesis returned
                # confidence=0 with 0 claims" and 198 "Failed to parse thesis.
                # Invalid JSON format" — recorded, resolved against price, and
                # labelled WIN/LOSS like any real call.
                #
                # They are not a random sample: they win 55.1% at -5.61% mean
                # versus 61.1% / +1.94% for real decisions, and they all land
                # at confidence 0. So they poison every consumer that reads
                # this table — SkillOpt's baseline, the scorecard, and the
                # confidence calibration, where they manufactured a fake
                # "low confidence loses money" band that was really the crash
                # rate wearing a calibration costume (see calibration_report's
                # fetch()).
                #
                # Confidence is a clean discriminator here: the distribution
                # jumps 0 -> 15 with nothing in between, so 0 is a sentinel
                # rather than a real (if dismal) score.
                if _is_unscoreable(confidence, result):
                    logger.info(
                        "[OUTCOME] Skipping %s — degraded artifact, not a "
                        "decision (confidence=%s, provenance=%s)",
                        ticker, confidence, result.get("decision_provenance"),
                    )
                    skipped_degraded += 1
                    continue

                if entry_price is None:
                    logger.debug("[OUTCOME] Skipping %s — no price_history available", ticker)
                    continue

                # Check if we already recorded this cycle+ticker combo
                existing = db.execute(
                    "SELECT id FROM decision_outcomes WHERE cycle_id = %s AND ticker = %s",
                    [cycle_id, ticker],
                ).fetchone()
                if existing:
                    continue

                overridden_from = resolve_overridden_from(
                    db, cycle_id, ticker, action
                )

                outcome_id = f"do-{uuid.uuid4().hex[:12]}"
                # Serialized like skill_versions: psycopg adapts dict->hstore,
                # not JSONB.
                _models = models_by_ticker.get(ticker)
                models_used = _mjson.dumps(_models) if _models else None
                db.execute(
                    """INSERT INTO decision_outcomes
                    (id, cycle_id, ticker, action, confidence, entry_price,
                     created_at, skill_versions, overridden_from, models_used)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [outcome_id, cycle_id, ticker, action, confidence,
                     round(entry_price, 4), datetime.now(timezone.utc),
                     skill_versions, overridden_from, models_used],
                )
                recorded += 1

        if recorded > 0 or skipped_degraded:
            # Report the skip count rather than silently dropping rows: a
            # cycle where most artifacts were degraded looks identical to a
            # quiet cycle if only `recorded` is logged, and that is precisely
            # the failure this filter exists to surface.
            logger.info(
                "[OUTCOME] Recorded %d decision outcomes for cycle %s%s",
                recorded, cycle_id[:12],
                f" ({skipped_degraded} degraded artifact(s) skipped)"
                if skipped_degraded else "",
            )
    except Exception as e:
        logger.error("[OUTCOME] Failed to record decisions: %s", e)

    return recorded


def resolve_pending_outcomes() -> dict:
    """
    Find unresolved decision_outcomes older than RESOLVE_AFTER_DAYS,
    look up current price, compute PnL, and classify: WIN/LOSS/FLAT for
    directional calls, HOLD_CORRECT/HOLD_MISS for hold claims.

    Returns summary stats.
    """
    resolved = 0
    errors = 0
    stats = {"wins": 0, "losses": 0, "flats": 0, "holds_correct": 0, "holds_miss": 0}

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVE_AFTER_DAYS)
        with get_db() as db:
            pending = db.execute(
                """
                SELECT id, ticker, action, entry_price, created_at
                FROM decision_outcomes
                WHERE resolved_at IS NULL AND created_at < %s
                ORDER BY created_at ASC
                LIMIT 50
                """,
                [cutoff],
            ).fetchall()

            for outcome_id, ticker, action, entry_price, created_at in pending:
                try:
                    # Same one-vendor path as the entry price, or the P&L is a
                    # vendor spread rather than a return (see app/quant/returns.py).
                    from app.quant.returns import latest_close

                    _px = latest_close(ticker)
                    price_row = (_px,) if _px is not None else None

                    if not price_row or price_row[0] is None:
                        logger.debug("[OUTCOME] Cannot resolve %s — no current price for %s", outcome_id, ticker)
                        continue

                    exit_price = price_row[0]

                    if entry_price is None or entry_price == 0:
                        logger.debug("[OUTCOME] Cannot resolve %s — invalid entry_price", outcome_id)
                        continue

                    # Compute PnL based on action direction. A HOLD claim is
                    # evaluated on the raw signed move — direction is
                    # irrelevant to "nothing meaningful happened".
                    if action == "SELL":
                        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                    else:  # BUY and HOLD both measure the long-side move
                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

                    outcome = _classify(action, pnl_pct)
                    key = {
                        "WIN": "wins", "LOSS": "losses", "FLAT": "flats",
                        "HOLD_CORRECT": "holds_correct", "HOLD_MISS": "holds_miss",
                    }[outcome]
                    stats[key] += 1

                    db.execute(
                        """UPDATE decision_outcomes
                        SET exit_price = %s, pnl_pct = %s, outcome = %s, resolved_at = %s
                        WHERE id = %s""",
                        [round(exit_price, 4), round(pnl_pct, 2), outcome,
                         datetime.now(timezone.utc), outcome_id],
                    )
                    resolved += 1

                except Exception as row_err:
                    errors += 1
                    logger.warning("[OUTCOME] Failed to resolve %s: %s", outcome_id, row_err)

        if resolved > 0:
            logger.info(
                "[OUTCOME] Resolved %d outcomes: %dW / %dL / %dF / %dHC / %dHM (errors: %d)",
                resolved, stats["wins"], stats["losses"], stats["flats"],
                stats["holds_correct"], stats["holds_miss"], errors,
            )
    except Exception as e:
        logger.error("[OUTCOME] Batch resolution failed: %s", e)

    return {"resolved": resolved, "errors": errors, **stats}


def resolve_outcome_for_exit(ticker: str, exit_price: float, realized_pnl: float | None = None) -> int:
    """Immediately resolve pending decision_outcomes for a ticker when a
    position exits (stop-loss / take-profit), instead of waiting for the
    time-based batch resolver.

    Returns the number of rows resolved.
    """
    resolved = 0
    try:
        with get_db() as db:
            pending = db.execute(
                "SELECT id, action, entry_price FROM decision_outcomes "
                "WHERE ticker = %s AND resolved_at IS NULL",
                [ticker],
            ).fetchall()
            for outcome_id, action, entry_price in pending:
                if not entry_price or not exit_price:
                    continue
                if action == "BUY":
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                elif action == "SELL":
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:
                    # HOLD claims resolve on their 7-day timer, never on a
                    # position exit — the claim is about the horizon, and an
                    # exit at day 2 says nothing about it.
                    continue
                outcome = _classify(action, pnl_pct)
                db.execute(
                    """UPDATE decision_outcomes
                    SET exit_price = %s, pnl_pct = %s, outcome = %s, resolved_at = %s
                    WHERE id = %s""",
                    [round(exit_price, 4), round(pnl_pct, 2), outcome,
                     datetime.now(timezone.utc), outcome_id],
                )
                resolved += 1
        if resolved:
            logger.info("[OUTCOME] Resolved %d outcome(s) for %s on position exit", resolved, ticker)
    except Exception as e:
        logger.error("[OUTCOME] Exit resolution failed for %s: %s", ticker, e)
    return resolved


def override_scorecard(days: int = 30) -> dict:
    """Did the synthesizer's veto add or destroy alpha?

    Execution reads `trade_decision or final_decision`, so when the decision
    synthesizer disagrees with the Board it wins. Measured over 7 days it
    downgraded 21 of 41 Board BUYs to HOLD — a 51% veto rate on the pipeline's
    entire trade flow, with no evidence behind it. The confidence floor of 70
    earned its place with numbers (conf <70: n=130, mean -1.91%; >=70: n=698,
    mean +3.76%); the veto sitting in FRONT of that floor had none, because
    `decision_outcomes` recorded only the surviving action.

    A HOLD carries the long-side move regardless of direction (see
    resolve_pending_outcomes), so an overridden BUY's pnl_pct IS the
    counterfactual: what the declined trade would have returned.

    Compare `overridden_buys.mean_pnl` against `kept_buys.mean_pnl`. If the
    overrides are systematically WORSE than the BUYs that survived, the veto is
    finding something real; if they are better, it is costing money.

    `blocked_by_gate` is the same counterfactual for the POLICY gate rather
    than the synthesizer, and it exists because those rows used to be counted
    as `kept_buys` — the desk was credited with keeping trades the confidence
    floor had refused. Comparing its mean against `kept_buys` back-tests the
    floor itself. No verdict is printed for it yet: at 19 rows it is under the
    20-row bar the veto comparison uses, and the same "a small mean is noise"
    rule has to apply to both or the bar means nothing.
    """
    out: dict = {"days": days, "note": None}
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT
                    CASE
                        -- Order matters: a policy-blocked BUY keeps
                        -- action='BUY' (so its counterfactual stays
                        -- scoreable) and now carries overridden_from='BUY'.
                        -- It must be caught BEFORE kept_buys, which it used
                        -- to fall into while overridden_from was NULL —
                        -- crediting the desk with keeping a trade the gate
                        -- refused.
                        WHEN action = overridden_from
                            THEN 'blocked_by_gate'
                        WHEN action = 'BUY' AND overridden_from IS NULL
                            THEN 'kept_buys'
                        WHEN action = 'HOLD' AND overridden_from = 'BUY'
                            THEN 'overridden_buys'
                        ELSE 'other'
                    END AS bucket,
                    count(*),
                    count(pnl_pct),
                    avg(pnl_pct)
                FROM decision_outcomes
                WHERE created_at > now() - (%s || ' days')::interval
                GROUP BY 1
                """,
                [str(days)],
            ).fetchall()
        for bucket, n, scored, mean in rows:
            if bucket == "other":
                continue
            out[bucket] = {
                "n": n,
                "scored": scored,
                "mean_pnl": round(float(mean), 3) if mean is not None else None,
            }
    except Exception as e:
        logger.warning("[OUTCOME] override scorecard failed: %s", e)
        return out

    kept = out.get("kept_buys", {})
    over = out.get("overridden_buys", {})
    # State the verdict only when both sides have enough resolved rows to
    # support one. A 4-row mean is noise, and printing it next to a 900-row
    # mean invites reading it as a finding — the failure this whole audit has
    # been about.
    if (kept.get("scored") or 0) >= 20 and (over.get("scored") or 0) >= 20:
        delta = (over["mean_pnl"] or 0) - (kept["mean_pnl"] or 0)
        out["veto_edge_pct"] = round(delta, 3)
        out["verdict"] = (
            "the veto AVOIDED worse-than-average trades"
            if delta < 0 else
            "the veto DECLINED better-than-average trades"
        )
    else:
        out["note"] = (
            f"not enough resolved rows for a verdict "
            f"(kept={kept.get('scored', 0)}, overridden={over.get('scored', 0)}; "
            f"need 20 each)"
        )
    return out
