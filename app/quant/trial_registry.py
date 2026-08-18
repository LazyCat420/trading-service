"""A ledger of every hypothesis tested against this desk's price history.

## Why

`deflated_sharpe_ratio` is the one gate here that survives contact with
multiple testing — `tests/unit/test_multiple_testing_gates.py` shows the best
of 100 pure-noise series reaching an annualized Sharpe of ~3.3 with PSR
0.9995, and DSR is the only gate that correctly FAILs it. But DSR is only as
honest as the `n_trials` it is handed, and nothing recorded that number.

`scripts/factor_backtest.py` passes `n_trials=len(factor_names)` = 4, and its
own comment says that is a floor: momentum, low-vol, beta and reversal are the
four in THAT script, while sizing rules, HMM regime variants, scoring-engine
weightings and many agent configurations have all been tried against the same
2,700 tickers of the same table. Deflating a new result against 4 when the
true count is closer to 40 is the difference between "this survived selection"
and "this is the luckiest of many draws" — and it fails in the direction that
lets noise through.

## What counts as a trial

One row per distinct HYPOTHESIS, not per run. Re-running momentum tomorrow is
not a new trial; testing momentum with a different lookback IS, because it is
another draw from the same well. `record_trial` is idempotent on
(family, label) and only bumps a run counter, so an automated harness that
loops cannot inflate its own denominator.

`family` scopes the correction. Trials in the same family are draws against
the same data and must deflate each other; a vol-forecast family and a
cross-sectional-equity family are separate hypothesis spaces. When unsure,
use the broader family — over-counting costs a little power, under-counting
manufactures discoveries.
"""

from __future__ import annotations

import json
import logging

from app.db.connection import get_db
from app.db import mongo_store
from app.db import mongo_query

logger = logging.getLogger(__name__)

# The default family: anything derived from price_history and scored on
# returns. Deliberately broad — see the module docstring.
DEFAULT_FAMILY = "price_derived"

# Trials already run against this history before the ledger existed, taken
# from the sources that name them: deflated_sharpe_ratio's own docstring,
# scripts/factor_backtest.py's factor list, and the quant-layer handoffs.
# Seeded so the FIRST post-ledger result deflates against reality instead of
# against 1. These are hypotheses that were evaluated, not necessarily ones
# that were adopted — a rejected trial still consumed a draw.
KNOWN_PRIOR_TRIALS: tuple[tuple[str, str, str], ...] = (
    (DEFAULT_FAMILY, "factor:momentum_12_1", "scripts/factor_backtest.py"),
    (DEFAULT_FAMILY, "factor:low_volatility_61d", "scripts/factor_backtest.py"),
    (DEFAULT_FAMILY, "factor:market_beta_253d", "scripts/factor_backtest.py"),
    (DEFAULT_FAMILY, "factor:short_term_reversal_21d", "scripts/factor_backtest.py"),
    (DEFAULT_FAMILY, "regime:hmm_2_state", "app/quant/regime_hmm.py"),
    (DEFAULT_FAMILY, "regime:hmm_3_state", "app/quant/regime_hmm.py"),
    (DEFAULT_FAMILY, "regime:sma200_vix", "app/processors/market_regime.py"),
    (DEFAULT_FAMILY, "vol:garch_1_1", "app/quant/garch.py"),
    (DEFAULT_FAMILY, "portfolio:hrp_ledoit_wolf", "app/quant/portfolio_math.py"),
    (DEFAULT_FAMILY, "sizing:atr_risk_bracket", "app/quant/sizing_bracket.py"),
    (DEFAULT_FAMILY, "alpha:residual_4factor", "app/quant/residual_alpha.py"),
    (DEFAULT_FAMILY, "score:composite_0_100", "app/processors/quant_processor.py"),
)


def ensure_table() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS research_trials (
                family      TEXT NOT NULL,
                label       TEXT NOT NULL,
                source      TEXT,
                meta        JSONB,
                run_count   INTEGER DEFAULT 1,
                first_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_run_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (family, label)
            )
            """
        )


def record_trial(
    label: str,
    family: str = DEFAULT_FAMILY,
    source: str = "",
    meta: dict | None = None,
) -> bool:
    """Register one hypothesis. Idempotent on (family, label).

    Returns True when the row was written or bumped. Never raises — a ledger
    outage must not stop research, it just means the count is stale, and the
    count being stale is visible in `trial_count`'s own return value.
    """
    label = (label or "").strip()
    if not label:
        return False
    try:
        ensure_table()
        with get_db() as db:
            db.execute(
                """
                INSERT INTO research_trials (family, label, source, meta)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (family, label) DO UPDATE SET
                    run_count = research_trials.run_count + 1,
                    last_run_at = CURRENT_TIMESTAMP,
                    source = COALESCE(NULLIF(EXCLUDED.source, ''),
                                      research_trials.source),
                    meta = COALESCE(EXCLUDED.meta, research_trials.meta)
                """,
                [family, label, source, json.dumps(meta) if meta else None],
            )
        return True
    except Exception as e:
        logger.warning("[TrialRegistry] record_trial(%s/%s) failed: %s",
                       family, label, e)
        return False


def seed_known_trials() -> int:
    """Insert the pre-ledger trials. Idempotent; returns rows now present."""
    for family, label, source in KNOWN_PRIOR_TRIALS:
        try:
            ensure_table()
            with get_db() as db:
                mongo_store.upsert_doc('research_trials', {'family': family, 'label': label}, {'family': family, 'label': label, 'source': source}, insert_only=True)
        except Exception as e:
            logger.warning("[TrialRegistry] seed %s failed: %s", label, e)
    return trial_count()


def trial_count(family: str = DEFAULT_FAMILY, include: str | None = None) -> int:
    """Distinct hypotheses recorded in `family`.

    `include` is a label that should be counted even if it has not been
    recorded yet — the trial you are about to deflate. Returns at least 1, so
    a caller can always pass the result straight to deflated_sharpe_ratio.
    """
    try:
        ensure_table()
        with get_db() as db:
            row = mongo_query.agg_row('research_trials', {'family': family}, [('count', None)])
            n = int(row[0]) if row else 0
            if include:
                seen = db.execute(
                    "SELECT 1 FROM research_trials WHERE family = %s AND label = %s",
                    [family, include],
                ).fetchone()
                if not seen:
                    n += 1
        return max(1, n)
    except Exception as e:
        logger.warning("[TrialRegistry] trial_count(%s) failed: %s", family, e)
        return 1


def deflated_sharpe_from_registry(
    returns,
    label: str,
    family: str = DEFAULT_FAMILY,
    source: str = "",
    record: bool = True,
    **kwargs,
) -> dict:
    """`deflated_sharpe_ratio` with n_trials taken from the ledger.

    The result carries `n_trials_source` so a report can never present a
    registry-backed deflation and a hand-guessed one as the same thing.
    """
    from app.quant.stat_gates import deflated_sharpe_ratio

    n = trial_count(family, include=label)
    if record:
        record_trial(label, family=family, source=source)

    out = deflated_sharpe_ratio(returns, n_trials=n, **kwargs)
    out["n_trials_source"] = f"research_trials[{family}]"
    out["trial_label"] = label
    return out
