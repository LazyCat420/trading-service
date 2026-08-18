"""Component efficacy monitor — is the expensive component still earning its keep?

Autoresearch grades DECISIONS (outcome_tracker), AGENTS (evaluator, SkillOpt's
scorecard) and DATA (data_audit) — but no quant component had scheduled
grading: the HMM regime shadow was graded exactly once, by hand
(scripts/grade_hmm_regime.py, 2026-08-03), and there was no off-switch for it
at all. This module closes that gap for the HMM, with a shape that a second
component can join later.

What it does, once per trading day (scheduler: component_health_evaluation):

  1. Grades the stored daily posteriors (regime_hmm_posteriors) on the same
     definitions the offline scripts use (app/quant/regime_grading.py):
       * band coverage — Kupiec proportion-of-failures on the 95% one-step
         predictive band. Too NARROW = the model understates the risk it
         shows the desks. That is the harmful direction.
       * the free-baseline race — Diebold-Mariano (Newey-West on the paired
         loss differential) of the HMM's one-step variance vs a trailing
         20-day sigma, on QLIKE and MSE. The bar is the FREE signal, not
         zero: a component that cannot beat the free alternative has no
         measurable value whatever its own effect size.
       * operational health — snapshot gaps and consecutive stale-tape fits.
  2. Writes one row per evaluation to component_health_reports (served by
     /api/v1/component-health).
  3. On 3 CONSECUTIVE 'failing' verdicts, withholds the component from the
     desks by proposing HMM_REGIME_MODE 0 -> 1 through the parameter
     governor (the only writer), and notifies the user via an agent note.

What it deliberately does NOT do:

  * 'redundant' (no better than free — the verdict the 2026-08-03 experiments
    already returned for the vol number) does NOT auto-disable. The state
    LABEL, duration and switching odds are outputs nothing else produces, and
    whether they are worth ~22-32s/cycle was explicitly left as a human call.
    The monitor SURFACES that verdict; it does not make the call.
  * It never re-enables. Flapping a prompt component on measurement noise is
    worse than either steady state; recovery is a human decision (set
    HMM_REGIME_MODE back to 0 via chat).
  * It never edits code. The auto-repair loop that scored patches by 'the
    tests pass' was deleted 2026-07-31 on its own measured record; this
    monitor only flips a bounded, reversible, code-owned parameter.

P&L is deliberately absent from the verdict: scripts/power_report.py puts the
desk's honest MDE at 8.84pp, so 'did the prompt line move P&L' is unanswerable
for ~a year. Everything graded here is self-validating at daily n — the
model's own claims against realized returns.

Observer contract (app/v3/invariants.py): run_component_health_evaluation
never raises. A monitor must not be the reason a scheduler tick or a cycle
reports failure.
"""

from __future__ import annotations

import json
import logging

import numpy as np
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

COMPONENT_HMM = "hmm_regime"

# Rolling evaluation window, in stored posterior rows (~6 months of sessions).
WINDOW_SESSIONS = 120
# Below this many scoreable days the statistical checks stay silent
# (insufficient_data) rather than verdicting on noise.
MIN_OBSERVATIONS = 40
# |DM t| above this on a loss differential = a significant difference.
DM_SIGNIFICANT_T = 2.0
# Trailing window for the free baseline sigma, matching the offline race.
TRAILING_WINDOW = 20
# Floor on a daily variance forecast, in percent^2 (vol_forecast_race.py).
_VAR_FLOOR = 0.01
# Operational failure: no posterior stored for this many trading days
# (the snapshot job runs every weekday, so 4 misses = the job is broken),
# or the latest N fits all ran on a tape >= 2 sessions stale (the SPY
# blocklist incident put a week-stale shadow in every desk for 6 sessions).
GAP_TRADING_DAYS_TO_FAIL = 4
STALE_RUN_TO_FAIL = 3
# Consecutive 'failing' evaluations before the monitor withholds the
# component from prompts. Evaluations are daily, so this is 3 trading days.
CONSECUTIVE_FAILING_TO_DISABLE = 3

VERDICT_INSUFFICIENT = "insufficient_data"
VERDICT_HEALTHY = "healthy"
VERDICT_REDUNDANT = "redundant"
VERDICT_FAILING = "failing"

# What a verdict means, served to the client so it renders the real
# definitions instead of hardcoding them (eval_trust_router convention).
VERDICT_DEFINITIONS = {
    VERDICT_INSUFFICIENT: "Too few scoreable days to say anything.",
    VERDICT_HEALTHY: "Calibrated AND significantly better than the free "
                     "baseline on at least one loss.",
    VERDICT_REDUNDANT: "Not failing, but not measurably better than the free "
                       "signal. Whether the label alone is worth the cost is "
                       "a human call — the monitor only surfaces it.",
    VERDICT_FAILING: "Actively harmful or broken: band understates risk "
                     "(Kupiec too_narrow), significantly worse than free on "
                     "BOTH losses, snapshot gap, or a stale-tape run. "
                     f"{CONSECUTIVE_FAILING_TO_DISABLE} in a row -> the prompt "
                     "line is withheld (HMM_REGIME_MODE=1).",
}


# ── persistence ──────────────────────────────────────────────────────

def ensure_health_table() -> None:
    from app.db.connection import get_db

    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS component_health_reports (
                id                  BIGSERIAL PRIMARY KEY,
                component           TEXT NOT NULL,
                evaluated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                window_start        DATE,
                window_end          DATE,
                observations        INTEGER,
                verdict             TEXT NOT NULL,
                failure_kinds       JSONB,
                consecutive_failing INTEGER,
                metrics             JSONB,
                action              TEXT,
                note                TEXT
            )
            """
        )


# ── metric computation (reads DB, pure otherwise) ────────────────────

def compute_hmm_metrics(ticker: str = "SPY") -> dict:
    """Grade the stored posteriors. Returns a metrics dict; never raises to
    the caller with a live exception — errors come back as {'error': ...}."""
    from app.quant.regime_grading import (
        EXPECTED_BREACH_RATE, Z_95, load_posteriors, market_closes,
        next_return_pct, predictive_band,
    )
    from app.quant.stat_gates import coverage_gate, newey_west_tstat

    try:
        rows = load_posteriors(ticker)[-WINDOW_SESSIONS:]
        closes = market_closes(ticker)
    except Exception as e:  # noqa: BLE001
        return {"error": f"load failed: {e}"}
    if not rows or not closes:
        return {"error": "no posteriors or no price history"}

    dates = [d for d, _ in closes]
    px = np.array([c for _, c in closes], dtype=float)
    ret_pct = np.concatenate([[np.nan], np.diff(np.log(px)) * 100.0])
    index = {d: i for i, d in enumerate(dates)}

    realized, bands = [], []
    loss_diff_qlike, loss_diff_mse = [], []
    for row in rows:
        band = predictive_band(row)
        nxt = next_return_pct(closes, row["as_of"])
        if band is None or nxt is None:
            continue
        realized.append(nxt)
        bands.append(band)

        # The free-baseline race: HMM one-step variance vs the trailing
        # 20-day sigma ENDING at as_of, both scored on the next session's
        # squared return. Same definitions as scripts/vol_forecast_race.py.
        i = index.get(row["as_of"])
        if i is None:
            continue
        window = ret_pct[max(1, i - TRAILING_WINDOW + 1): i + 1]
        window = window[np.isfinite(window)]
        if window.size < TRAILING_WINDOW:
            continue
        trail_var = max(float(np.std(window, ddof=1)) ** 2, _VAR_FLOOR)
        hmm_var = max((band / Z_95) ** 2, _VAR_FLOOR)
        r2 = nxt * nxt
        # Positive differential = the HMM's loss is HIGHER = worse than free.
        loss_diff_qlike.append(
            (np.log(hmm_var) + r2 / hmm_var) - (np.log(trail_var) + r2 / trail_var)
        )
        loss_diff_mse.append((hmm_var - r2) ** 2 - (trail_var - r2) ** 2)

    cov = coverage_gate(realized, bands, EXPECTED_BREACH_RATE,
                        label=f"{ticker} 1-day 95% band")
    # DM IS a HAC t-test on the paired loss differential; reusing the audited
    # Newey-West helper keeps one definition of the correction in this repo.
    dm_qlike = newey_west_tstat(loss_diff_qlike, horizon=1)
    dm_mse = newey_west_tstat(loss_diff_mse, horizon=1)

    # Operational health.
    last_as_of = rows[-1]["as_of"]
    gap_days = sum(1 for d in dates if d > last_as_of)
    stale_run = 0
    for row in reversed(rows):
        if (row.get("stale_sessions") or 0) >= 2:
            stale_run += 1
        else:
            break

    return {
        "ticker": ticker,
        "window_start": str(rows[0]["as_of"]),
        "window_end": str(last_as_of),
        "observations": len(realized),
        "coverage": {k: cov.get(k) for k in
                     ("ok", "observations", "breaches", "observed_rate",
                      "expected_rate", "lr_statistic", "p_value", "passes",
                      "direction", "reason")},
        "vs_free": {
            "qlike_t": dm_qlike.get("t_stat"),
            "qlike_mean_diff": dm_qlike.get("mean"),
            "mse_t": dm_mse.get("t_stat"),
            "mse_mean_diff": dm_mse.get("mean"),
            "n": len(loss_diff_qlike),
            "baseline": f"trailing {TRAILING_WINDOW}-day sigma (free)",
        },
        "operational": {
            "gap_trading_days": gap_days,
            "stale_run": stale_run,
            "last_as_of": str(last_as_of),
        },
    }


# ── verdict (pure) ───────────────────────────────────────────────────

def decide_verdict(metrics: dict) -> tuple[str, list[str]]:
    """Metrics -> (verdict, failure_kinds). Pure, so the thresholds are
    unit-testable without a database.

    The default verdict is the KEEP-shaped one (gate_ablation.py's bar):
    'failing' needs a specific demonstrated harm, never a missing benefit.
    """
    if metrics.get("error"):
        # A monitor that cannot read its inputs knows nothing about the
        # component; an unreachable table must not accumulate strikes.
        return VERDICT_INSUFFICIENT, [f"error: {metrics['error']}"]

    failures: list[str] = []
    op = metrics.get("operational") or {}
    if (op.get("gap_trading_days") or 0) >= GAP_TRADING_DAYS_TO_FAIL:
        failures.append(
            f"snapshot gap: {op['gap_trading_days']} trading days without a "
            f"stored posterior (job or data broken)"
        )
    if (op.get("stale_run") or 0) >= STALE_RUN_TO_FAIL:
        failures.append(
            f"stale tape: last {op['stale_run']} fits ran >=2 sessions behind "
            f"— the label is historical, not current"
        )

    n = metrics.get("observations") or 0
    cov = metrics.get("coverage") or {}
    vs = metrics.get("vs_free") or {}
    if n >= MIN_OBSERVATIONS:
        if cov.get("ok") and not cov.get("passes") and cov.get("direction") == "too_narrow":
            failures.append(
                f"band too NARROW (Kupiec p={cov.get('p_value'):.4f}): the "
                f"model understates the one-day risk it shows the desks"
            )
        qt, mt = vs.get("qlike_t"), vs.get("mse_t")
        if (qt is not None and mt is not None
                and qt > DM_SIGNIFICANT_T and mt > DM_SIGNIFICANT_T):
            failures.append(
                f"significantly WORSE than the free baseline on both losses "
                f"(QLIKE t={qt:+.2f}, MSE t={mt:+.2f})"
            )

    if failures:
        return VERDICT_FAILING, failures
    if n < MIN_OBSERVATIONS:
        return VERDICT_INSUFFICIENT, []

    # Better than free on at least one loss, with a calibrated band -> healthy.
    qt, mt = vs.get("qlike_t"), vs.get("mse_t")
    beats_free = any(
        t is not None and t < -DM_SIGNIFICANT_T for t in (qt, mt)
    )
    if beats_free and cov.get("ok") and cov.get("passes"):
        return VERDICT_HEALTHY, []
    return VERDICT_REDUNDANT, []


def decide_action(verdict: str, streak: int, current_mode: int) -> str:
    """(verdict, consecutive-failing streak, HMM_REGIME_MODE) -> action.
    Pure. 'auto_disable' means: propose mode 1 through the governor."""
    if verdict != VERDICT_FAILING:
        return "none"
    if current_mode != 0:
        return "already_disabled"
    if streak >= CONSECUTIVE_FAILING_TO_DISABLE:
        return "auto_disable"
    return "none"


# ── the evaluation entrypoint ────────────────────────────────────────

def _failing_streak(component: str) -> int:
    """Consecutive 'failing' verdicts at the head of the report history,
    BEFORE this evaluation. 0 on any read problem — a broken history read
    must not manufacture a disable."""
    from app.db.connection import get_db

    try:
        rows = mongo_query.find_rows('component_health_reports', {'component': component}, ['verdict'], sort=[('evaluated_at', -1)], limit=CONSECUTIVE_FAILING_TO_DISABLE)
        streak = 0
        for (verdict,) in rows:
            if verdict == VERDICT_FAILING:
                streak += 1
            else:
                break
        return streak
    except Exception as e:  # noqa: BLE001
        logger.warning("[ComponentHealth] streak read failed: %s", e)
        return 0


def _auto_disable(component: str, failures: list[str]) -> str:
    """Withhold the component from prompts via the governor. Returns the
    action actually taken ('auto_disabled' or 'auto_disable_rejected')."""
    from app.services.parameter_governor import propose_parameter_change

    reason = (
        f"component_health: {component} failing "
        f"{CONSECUTIVE_FAILING_TO_DISABLE} consecutive daily evaluations — "
        + "; ".join(failures)[:400]
    )
    result = propose_parameter_change(
        "HMM_REGIME_MODE", 1, reason=reason, agent="component_health_monitor",
    )
    if result.get("status") != "applied":
        logger.warning("[ComponentHealth] auto-disable REJECTED by governor: %s",
                       result.get("reason") or result.get("message"))
        return "auto_disable_rejected"

    logger.warning("[ComponentHealth] %s AUTO-DISABLED (HMM_REGIME_MODE 0->1): %s",
                   component, "; ".join(failures))
    try:
        from app.services.session_profile import profile_memory
        profile_memory.add_agent_note(
            f"⚠️ COMPONENT AUTO-DISABLED: {component} prompt line withheld "
            f"(HMM_REGIME_MODE=1) after {CONSECUTIVE_FAILING_TO_DISABLE} "
            f"consecutive failing evaluations: {'; '.join(failures)[:300]}. "
            f"Daily grading continues; re-enable is a human call "
            f"(set HMM_REGIME_MODE back to 0). See /api/v1/component-health."
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[ComponentHealth] agent note failed: %s", e)
    return "auto_disabled"


def run_component_health_evaluation() -> dict:
    """Evaluate, persist, and (only on a confirmed failing streak) act.
    Never raises — observer contract."""
    try:
        from app.services.parameter_store import get_param

        try:
            mode = int(get_param("HMM_REGIME_MODE"))
        except Exception:  # noqa: BLE001
            mode = 0

        metrics = compute_hmm_metrics()
        verdict, failures = decide_verdict(metrics)
        streak = (_failing_streak(COMPONENT_HMM) + 1
                  if verdict == VERDICT_FAILING else 0)
        action = decide_action(verdict, streak, mode)
        if action == "auto_disable":
            action = _auto_disable(COMPONENT_HMM, failures)

        note = ""
        if verdict == VERDICT_REDUNDANT:
            note = ("Not better than the free baseline — known state since "
                    "the 2026-08-03 experiments. Whether the state label is "
                    "worth ~22-32s/cycle is an open HUMAN call.")

        try:
            from app.db.connection import get_db

            ensure_health_table()
            mongo_store.insert_docs('component_health_reports', [{'component': COMPONENT_HMM, 'window_start': metrics.get("window_start"), 'window_end': metrics.get("window_end"), 'observations': metrics.get("observations"), 'verdict': verdict, 'failure_kinds': json.dumps(failures), 'consecutive_failing': streak, 'metrics': json.dumps(metrics, default=str), 'action': action, 'note': note}])
        except Exception as e:  # noqa: BLE001
            logger.warning("[ComponentHealth] report write failed: %s", e)

        logger.info(
            "[ComponentHealth] %s: %s (n=%s, streak=%s, mode=%s, action=%s)%s",
            COMPONENT_HMM, verdict, metrics.get("observations"), streak, mode,
            action, f" — {'; '.join(failures)}" if failures else "",
        )
        return {"component": COMPONENT_HMM, "verdict": verdict,
                "failures": failures, "streak": streak, "action": action,
                "metrics": metrics}
    except Exception as e:  # noqa: BLE001
        logger.error("[ComponentHealth] evaluation failed (non-fatal): %s", e)
        return {"error": str(e)}


# ── read surfaces for the router ─────────────────────────────────────

def report_history(component: str = COMPONENT_HMM, limit: int = 30) -> list[dict]:
    from app.db.connection import get_db

    ensure_health_table()
    rows = mongo_query.find_rows('component_health_reports', {'component': component}, ['evaluated_at', 'window_start', 'window_end', 'observations', 'verdict', 'failure_kinds', 'consecutive_failing', 'metrics', 'action', 'note'], sort=[('evaluated_at', -1)], limit=max(1, min(int(limit), 200)))

    def _j(v):
        return json.loads(v) if isinstance(v, (str, bytes)) else v

    return [
        {
            "evaluated_at": str(r[0]), "window_start": str(r[1]),
            "window_end": str(r[2]), "observations": r[3], "verdict": r[4],
            "failure_kinds": _j(r[5]), "consecutive_failing": r[6],
            "metrics": _j(r[7]), "action": r[8], "note": r[9],
        }
        for r in rows
    ]
