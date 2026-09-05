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
(HOLD_CORRECT / HOLD_AVOIDED_DECLINE / HOLD_MISS) so the directional win-rate
cohort (WIN/LOSS) is untouched: folding "price stayed flat" into win rate would
let low volatility masquerade as directional skill. HOLD outcomes feed
calibration and a separate hold-accuracy metric in decision_audit instead.

The HOLD labels are direction-aware (see ``_classify``): on a long-only book
only an upside move is forgone, so a decline the desk sat out is
HOLD_AVOIDED_DECLINE and counts as correct.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from app.db import mongo_query
from app.db import mongo_store

logger = logging.getLogger(__name__)

# How many days to wait before resolving a decision outcome
RESOLVE_AFTER_DAYS = 7
# PnL thresholds for WIN/LOSS/FLAT classification
WIN_THRESHOLD_PCT = 1.0
LOSS_THRESHOLD_PCT = -1.0

# The move that saturates an episode's -1.0..1.0 outcome_score. 10% over a
# 7-day horizon against a 28.76pp sd of resolved pnl: well inside the
# distribution, so ordinary outcomes stay rankable and only the tails clamp.
EPISODE_SCORE_ANCHOR_PCT = 10.0

# Actions that are not decisions. DEGRADED is what the orchestrator writes
# when a desk produced no decision at all (see _is_unscoreable); it can never
# carry an outcome, and recording one would teach the memory system a call
# that was never made.
_NON_DECISIONS = {"DEGRADED", "NONE", ""}

# Outcome labels that are NOT outcomes. Same exclusion the rest of the service
# already applies to this column — decision_audit.py:124/176/442,
# confidence_calibration.py:51 and power_report.py:147 all drop these before
# measuring anything. DEGRADED_ARTIFACT is a pipeline crash scored against a
# price (358 rows), and it is the single largest label after WIN/LOSS: feeding
# it to the memory system would teach the desk that crashing is a strategy
# with a 3.2% mean return.
_NON_OUTCOMES = {"DEGRADED_ARTIFACT", "CANCELED"}


def _memory_store():
    """The mandated write path for episodic observations.

    Indirected through a function so the resolvers' memory writeback can be
    substituted in tests without a database — and so the "no inline SQL for
    memory outside the DAL" rule in app/db/README_memory_contracts.md holds
    here too.
    """
    from app.services.memory.store import MemoryStore

    return MemoryStore()


def _episode_store():
    from app.services.memory.episodic_memory import episodic_memory_store

    return episodic_memory_store


def write_outcome_to_memory(
    *,
    cycle_id: str,
    ticker: str,
    action: str,
    outcome: str | None,
    pnl_pct: float | None,
    confidence: float | None = None,
) -> None:
    """Feed a RESOLVED decision back into the two memory tiers agents read.

    Until this existed the loop was open at exactly this point. Both tiers are
    written at decision time and neither was ever revised, so:

    - the consolidator (which distils canonical memories) was handed
      ``Outcome: BUY (None)`` — the ACTION under the label "outcome"; and
    - `episodic_memory.outcome` sat at ``pending`` forever.

    Two writes, deliberately different in kind:

    1. A NEW episodic observation with ``source_type="outcome"`` — the
       documented contract, and the shape of the four such rows already on the
       table from June. It enters the consolidator's inbox as evidence, so a
       resolved outcome can become a canonical memory. It does not overwrite
       the decision-time row: what the desk believed and what happened are two
       facts, and collapsing them loses the calibration.
    2. An in-place resolution of the working-memory episode, whose ``pending``
       marker exists for exactly this.

    Never raises, and each sink is independent: this runs INSIDE the resolvers,
    after the ``decision_outcomes`` UPDATE, and a lost outcome is worse than a
    lost memory row. One broken sink must not silence the other.
    """
    # Fail closed on every input that would make the row a fabrication.
    if not outcome or not cycle_id or not ticker:
        return
    if (action or "").upper() in _NON_DECISIONS:
        return
    if outcome.upper() in _NON_OUTCOMES:
        return

    pnl = float(pnl_pct) if pnl_pct is not None else 0.0

    try:
        _memory_store().add_episodic_observation({
            "cycle_id": cycle_id,
            "ticker": ticker,
            "source_type": "outcome",
            "observation_text": (
                f"Resolved outcome for {ticker}: the desk said {action} at "
                f"{confidence if confidence is not None else '?'}% confidence "
                f"and the {RESOLVE_AFTER_DAYS}-day move was {pnl:+.2f}% "
                f"({outcome})."
            ),
            "confidence_at_creation": (
                float(confidence) / 100.0 if confidence else 0.0
            ),
            "outcome_label": outcome,
            # Raw pnl, matching the rows already on the table and what the
            # consolidator renders. The normalised form belongs to the episode
            # tier below, whose column is documented as -1.0..1.0.
            "outcome_score": pnl,
        })
    except Exception as e:  # noqa: BLE001 — never blocks resolution
        logger.warning(
            "[OUTCOME] %s/%s: outcome observation write failed: %s",
            cycle_id[:12], ticker, e,
        )

    try:
        score = max(-1.0, min(1.0, pnl / EPISODE_SCORE_ANCHOR_PCT))
        _episode_store().record_outcome(cycle_id, ticker, outcome, score)
    except Exception as e:  # noqa: BLE001 — never blocks resolution
        logger.warning(
            "[OUTCOME] %s/%s: episode resolution failed: %s",
            cycle_id[:12], ticker, e,
        )


def _classify(action: str, pnl_pct: float) -> str:
    """Map a signed pnl move to an outcome label for the given action.

    Directional calls keep the historical WIN/LOSS/FLAT taxonomy. HOLD claims
    get distinct labels on purpose: every existing consumer filters on
    WIN/LOSS, so HOLD rows are invisible to them unless they opt in.

    HOLD IS GRADED WITH DIRECTION (2026-08-08). The rule here used to be
    ``abs(pnl_pct) >= 1% -> HOLD_MISS``, which is direction-BLIND, and this
    book is long-only: the desk can buy, so the only thing a HOLD forgoes is
    an UPSIDE move. A name the desk held through a *decline* was held
    correctly — there was no short to place.

    Measured over the 154 graded HOLD_MISS rows on record: 85 rose (genuine
    missed buys) and 69 fell (declines correctly avoided). Grading those 69 as
    misses put hold accuracy at 28% when it is really 60% — and it scored an
    agent that dodged a drawdown identically to one that slept through a
    rally.

    The third label is deliberate: ``HOLD_MISS`` is NOT redefined to mean
    something new mid-history. It keeps meaning "a move the desk should have
    caught", and the rows that never met that description are relabelled by
    the backfill in ``app/db/migrations.py`` from the ``pnl_pct`` already
    stored on each row. Both halves must ship together — a forward-only change
    would leave ``HOLD_MISS`` meaning one thing before the deploy and another
    after, with nothing in the row to say which.
    """
    if action == "HOLD":
        if abs(pnl_pct) < WIN_THRESHOLD_PCT:
            return "HOLD_CORRECT"
        # Long-only: a decline the desk sat out is a hold that was RIGHT.
        return "HOLD_MISS" if pnl_pct > 0 else "HOLD_AVOIDED_DECLINE"
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


def resolve_overridden_from(db_unused, cycle_id: str, ticker: str, action: str):
    """Label a decision that something overruled, or None if nothing did."""
    try:
        desk_row = mongo_query.find_row('shared_desk', {'cycle_id': cycle_id, 'ticker': ticker}, ['desk_data'])
        if desk_row and desk_row[0]:
            desk_data = desk_row[0] if isinstance(desk_row[0], dict) else {}
            board_action = desk_data.get('final_decision', {}).get('action')
            if board_action and board_action != action:
                return board_action
    except Exception as e:  # noqa: BLE001 — provenance, never blocks
        logger.debug("[OUTCOME] %s: override lookup failed: %s", ticker, e)

    if _was_blocked_by_policy(db_unused, cycle_id, ticker):
        return action
    return None


def _was_blocked_by_policy(db_unused, cycle_id: str, ticker: str) -> bool:
    """True when either record of the policy gate says it refused this trade."""
    try:
        gate_row = mongo_query.find_row('trade_results', {'cycle_id': cycle_id, 'ticker': ticker}, ['policy_action'])
        policy_action = gate_row[0] if gate_row else None
        if policy_action and policy_action.startswith("HOLD_POLICY_BLOCKED"):
            return True
    except Exception as e:  # noqa: BLE001 — provenance, never blocks
        logger.debug("[OUTCOME] %s: policy-gate lookup failed: %s", ticker, e)

    try:
        fired = mongo_query.find_row('v3_guardrail_firings', {'cycle_id': cycle_id, 'ticker': ticker, 'guardrail': {'$regex': '^HOLD_POLICY_BLOCKED'}}, ['id'])
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
    from app.services.cycle_scope import is_synthetic_cycle

    # A test run's decisions are not claims about the market. These rows are
    # picked up by resolve_pending_outcomes 7 days later and land in the
    # 30-day resolved cohort behind hold-accuracy and calibration ECE, and
    # write_outcome_to_memory copies them back into episodic memory. The
    # 2026-08-31 observe ladder put 13 such HOLDs in the table before anyone
    # noticed the recorder had no gate.
    if is_synthetic_cycle(cycle_id):
        logger.info(
            "[OUTCOME] %s is a synthetic cycle — recording no decision outcomes",
            cycle_id,
        )
        return 0

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
        models_by_ticker: dict = {}
        import json as _mjson
        try:
            docs = mongo_store.find_docs(
                "v3_agent_telemetry",
                {"cycle_id": cycle_id, "model_used": {"$ne": None}},
                sort=[("created_at", 1)],
                projection={"ticker": 1, "agent_name": 1, "model_used": 1},
            )
            for d in docs:
                models_by_ticker.setdefault(d.get("ticker"), {})[d.get("agent_name")] = d.get("model_used")
        except Exception as e:  # noqa: BLE001 — provenance, never blocks
            logger.debug("[OUTCOME] model snapshot failed: %s", e)

        raw_rows = mongo_query.find_rows(
            'analysis_results',
            {'cycle_id': cycle_id, 'confidence': {'$ne': None}},
            ['ticker', 'confidence', 'result_json'],
        )
        from app.quant.returns import latest_close

        rows = [
            (ticker, confidence, latest_close(ticker), result_json)
            for ticker, confidence, result_json in raw_rows
        ]

        for ticker, confidence, entry_price, result_json in rows:
            # Extract action from result_json
            import json
            try:
                result = json.loads(result_json) if isinstance(result_json, str) else (result_json or {})
            except (json.JSONDecodeError, TypeError):
                result = {}

            action = result.get("action", "HOLD")

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
            existing = mongo_query.find_row('decision_outcomes', {'cycle_id': cycle_id, 'ticker': ticker}, ['id'])
            if existing:
                continue

            overridden_from = resolve_overridden_from(
                None, cycle_id, ticker, action
            )

            outcome_id = f"do-{uuid.uuid4().hex[:12]}"
            _models = models_by_ticker.get(ticker)
            models_used = _mjson.dumps(_models) if _models else None
            now_utc = datetime.now(timezone.utc)
            mongo_store.insert_docs('decision_outcomes', [{
                'id': outcome_id,
                'cycle_id': cycle_id,
                'ticker': ticker,
                'action': action,
                'confidence': confidence,
                'entry_price': round(entry_price, 4),
                'created_at': now_utc,
                'skill_versions': skill_versions,
                'overridden_from': overridden_from,
                'models_used': models_used,
            }])
            recorded += 1

        if recorded > 0 or skipped_degraded:
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
    directional calls, HOLD_CORRECT/HOLD_AVOIDED_DECLINE/HOLD_MISS for hold
    claims.

    Returns summary stats.
    """
    resolved = 0
    errors = 0
    stats = {"wins": 0, "losses": 0, "flats": 0, "holds_correct": 0,
             "holds_miss": 0, "holds_avoided_decline": 0}

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVE_AFTER_DAYS)
        # `exclude_synthetic` also covers rows written BEFORE the recorder
        # gained its gate above — the 13 observe HOLDs from 2026-08-31 stay
        # unresolved forever rather than being deleted, so the evidence of
        # what happened survives while the calibration cohort stays clean.
        from app.services.cycle_scope import exclude_synthetic

        pending = mongo_query.find_rows(
            'decision_outcomes',
            {'resolved_at': None, 'created_at': {'$lt': cutoff},
             **exclude_synthetic()},
            ['id', 'ticker', 'action', 'entry_price', 'created_at', 'cycle_id', 'confidence'],
            sort=[('created_at', 1)],
            limit=50,
        )

        for (outcome_id, ticker, action, entry_price, created_at,
             cycle_id, confidence) in pending:
            try:
                # THE HORIZON IS entry + RESOLVE_AFTER_DAYS, NOT "today".
                #
                # This read used `latest_close(ticker)` — whatever the price
                # happened to be on the day the sweep reached the row. The
                # cutoff above only decides WHEN a row becomes eligible; it
                # never bounded how late the price could be.
                #
                # MEASURED 2026-09-05 over 2,694 resolved rows
                # (`resolved_at - created_at`): median 43.0 days against a
                # stated 7, with 1,932 (71.7%) resolving beyond 30 days and
                # only 699 (25.9%) inside the 7-day contract the panel prints
                # on every card. Every win rate and decision score built on
                # this cohort measured a six-week horizon labelled one week.
                #
                # `close_on_or_after` walks forward past weekends/holidays but
                # is grace-bounded, so a genuinely missing stretch of price
                # data leaves the row UNRESOLVED rather than resolving it
                # against a price weeks past the horizon.
                from app.quant.returns import close_on_or_after

                horizon = created_at + timedelta(days=RESOLVE_AFTER_DAYS)
                exit_price, exit_date = close_on_or_after(ticker, horizon)

                if exit_price is None:
                    logger.debug(
                        "[OUTCOME] Cannot resolve %s — no %s close within the "
                        "grace window after the %s horizon",
                        outcome_id, ticker, horizon.date(),
                    )
                    continue

                if entry_price is None or entry_price == 0:
                    logger.debug("[OUTCOME] Cannot resolve %s — invalid entry_price", outcome_id)
                    continue

                if action == "SELL":
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:  # BUY and HOLD both measure the long-side move
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100

                outcome = _classify(action, pnl_pct)
                key = {
                    "WIN": "wins", "LOSS": "losses", "FLAT": "flats",
                    "HOLD_CORRECT": "holds_correct", "HOLD_MISS": "holds_miss",
                    "HOLD_AVOIDED_DECLINE": "holds_avoided_decline",
                }.get(outcome)
                if key:
                    stats[key] += 1
                else:
                    logger.warning(
                        "[OUTCOME] %s: unmapped outcome label %r — resolving "
                        "the row anyway, but it is uncounted", outcome_id, outcome,
                    )

                now_res = datetime.now(timezone.utc)
                # `exit_date` is the bar the price came from and
                # `horizon_days` the contract it was resolved under. Without
                # them the row records only WHEN THE SWEEP RAN (`resolved_at`),
                # which is exactly the ambiguity that let a 43-day median hide
                # behind a "7-day" label — an auditor could not tell a
                # contract-honouring row from a late one.
                mongo_store.update_docs('decision_outcomes', {'id': outcome_id}, {'$set': {'exit_price': round(exit_price, 4), 'pnl_pct': round(pnl_pct, 2), 'outcome': outcome, 'resolved_at': now_res, 'exit_date': exit_date, 'horizon_days': RESOLVE_AFTER_DAYS}})
                resolved += 1

                write_outcome_to_memory(
                    cycle_id=cycle_id, ticker=ticker, action=action,
                    outcome=outcome, pnl_pct=round(pnl_pct, 2),
                    confidence=confidence,
                )

            except Exception as row_err:
                errors += 1
                logger.warning("[OUTCOME] Failed to resolve %s: %s", outcome_id, row_err)

        if resolved > 0:
            logger.info(
                "[OUTCOME] Resolved %d outcomes: %dW / %dL / %dF / %dHC / "
                "%dHAD / %dHM (errors: %d)",
                resolved, stats["wins"], stats["losses"], stats["flats"],
                stats["holds_correct"], stats["holds_avoided_decline"],
                stats["holds_miss"], errors,
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
        pending = mongo_query.find_rows('decision_outcomes', {'ticker': ticker, 'resolved_at': None}, ['id', 'action', 'entry_price', 'cycle_id', 'confidence'])
        for outcome_id, action, entry_price, cycle_id, confidence in pending:
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
            mongo_store.update_docs('decision_outcomes', {'id': outcome_id}, {'$set': {'exit_price': round(exit_price, 4), 'pnl_pct': round(pnl_pct, 2), 'outcome': outcome, 'resolved_at': datetime.now(timezone.utc)}})
            resolved += 1
            write_outcome_to_memory(
                cycle_id=cycle_id, ticker=ticker, action=action,
                outcome=outcome, pnl_pct=round(pnl_pct, 2),
                confidence=confidence,
            )
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
        since = datetime.now(timezone.utc) - timedelta(days=days)
        docs = mongo_store.find_docs('decision_outcomes', {'created_at': {'$gt': since}})
        buckets: dict[str, list[float]] = {'blocked_by_gate': [], 'kept_buys': [], 'overridden_buys': []}
        counts: dict[str, int] = {'blocked_by_gate': 0, 'kept_buys': 0, 'overridden_buys': 0}
        for d in docs:
            act = d.get('action')
            ovr = d.get('overridden_from')
            pnl = d.get('pnl_pct')
            b = None
            if act == ovr and act is not None:
                b = 'blocked_by_gate'
            elif act == 'BUY' and ovr is None:
                b = 'kept_buys'
            elif act == 'HOLD' and ovr == 'BUY':
                b = 'overridden_buys'
            if b:
                counts[b] += 1
                if pnl is not None:
                    buckets[b].append(float(pnl))
        for b in ['blocked_by_gate', 'kept_buys', 'overridden_buys']:
            pnls = buckets[b]
            out[b] = {
                "n": counts[b],
                "scored": len(pnls),
                "mean_pnl": round(sum(pnls) / len(pnls), 3) if pnls else None,
            }
    except Exception as e:
        logger.warning("[OUTCOME] override scorecard failed: %s", e)
        return out

    kept = out.get("kept_buys", {})
    over = out.get("overridden_buys", {})
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
