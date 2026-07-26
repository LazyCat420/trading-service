"""
Statistical significance gates for return series.

## Why a plain t-stat lies here

Every return series this service produces is **overlapping**. A 7-session
forward return computed on consecutive days shares 6 of its 7 days with its
neighbour, so consecutive observations are mechanically correlated. The
classical t-stat assumes i.i.d. observations; fed overlapping windows it
overstates significance by roughly sqrt(overlap) — a 7-day overlap can turn a
t of 1.0 into an apparent 2.6.

Newey-West corrects the standard error for exactly that autocorrelation. The
lag length must be at least the overlap; `suggest_lag` uses the standard
floor(4*(n/100)^(2/9)) rule, bumped to the overlap when a horizon is given.

## The three gates

  1. **Newey-West t-stat** — is the mean return distinguishable from zero once
     autocorrelation is accounted for?
  2. **Stationary bootstrap** — a distribution-free confidence interval that
     also preserves serial dependence (fixed-block bootstrap breaks it at the
     block edges; the geometric block length here does not).
  3. **IS/OOS degradation** — split chronologically, compare Sharpe. A strategy
     that only works in-sample is fitted, not found.

All three return structured verdicts rather than bare booleans, because
"failed the gate" and "not enough data to run the gate" are different states
and collapsing them is how a null result gets laundered into a pass.
"""

from __future__ import annotations

import logging
import math

import numpy as np

logger = logging.getLogger(__name__)

# Below this many observations no gate here is meaningful.
MIN_OBSERVATIONS = 20

# Conventional bar for "this is real" in factor research. Deliberately above
# the 1.96 of a single test: we run many factors, so per-test 5% is too loose.
T_STAT_GATE = 2.5

# Bootstrap resamples. 10k is the plan's number and is cheap at these sizes.
BOOTSTRAP_RESAMPLES = 10_000

# Expected block length for the stationary bootstrap, in observations.
_BLOCK_LENGTH = 5

# A strategy keeping less than this share of its in-sample Sharpe out-of-sample
# is treated as fitted.
OOS_RETENTION_GATE = 0.5

TRADING_DAYS_YEAR = 252


def suggest_lag(n: int, horizon: int | None = None) -> int:
    """Newey-West lag length.

    Standard rule floor(4*(n/100)^(2/9)), raised to `horizon - 1` when the
    series is built from overlapping windows of that length — the overlap is
    a known lower bound on the autocorrelation structure, so ignoring it
    would leave exactly the bias this function exists to remove.
    """
    if n <= 1:
        return 0
    rule = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    if horizon and horizon > 1:
        rule = max(rule, int(horizon) - 1)
    return max(0, min(rule, n - 2))


def newey_west_tstat(
    returns: np.ndarray | list[float],
    horizon: int | None = None,
    lag: int | None = None,
) -> dict:
    """Autocorrelation-robust t-stat for the mean of `returns`.

    Uses Bartlett weights w_j = 1 - j/(L+1), which guarantee a non-negative
    variance estimate. If the correction produces a non-positive variance
    (possible with pathological input), falls back to the OLS variance and
    says so in `note` rather than returning a fabricated number.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < MIN_OBSERVATIONS:
        return {"ok": False, "reason": f"need >={MIN_OBSERVATIONS} observations, got {n}",
                "n": n, "t_stat": None, "mean": None}

    mean = float(arr.mean())
    demeaned = arr - mean
    gamma0 = float(np.dot(demeaned, demeaned) / n)

    L = suggest_lag(n, horizon) if lag is None else max(0, int(lag))
    lrv = gamma0
    for j in range(1, L + 1):
        gamma_j = float(np.dot(demeaned[j:], demeaned[:-j]) / n)
        weight = 1.0 - j / (L + 1.0)
        lrv += 2.0 * weight * gamma_j

    note = ""
    if lrv <= 0:
        lrv = gamma0
        note = "NW variance non-positive; fell back to OLS variance"

    se = math.sqrt(lrv / n) if lrv > 0 else 0.0
    if se <= 0:
        return {"ok": False, "reason": "zero standard error (constant series)",
                "n": n, "t_stat": None, "mean": mean}

    t_stat = mean / se
    return {
        "ok": True,
        "n": n,
        "lag": L,
        "mean": round(mean, 6),
        "std_error": round(se, 6),
        "t_stat": round(float(t_stat), 3),
        "passes": bool(abs(t_stat) >= T_STAT_GATE),
        "gate": T_STAT_GATE,
        "note": note,
    }


def stationary_bootstrap_ci(
    returns: np.ndarray | list[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    block_length: int = _BLOCK_LENGTH,
    confidence: float = 0.95,
    seed: int | None = None,
) -> dict:
    """Politis-Romano stationary bootstrap CI for the mean.

    Blocks have geometric length (p = 1/block_length) and wrap around the
    series, which keeps the resampled series stationary — a fixed-block
    bootstrap severs dependence at every block boundary and quietly narrows
    the interval on exactly the autocorrelated data we care about.

    Deterministic by default (seed derived from the data) so repeat runs of
    the same gate agree; pass `seed` to override.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < MIN_OBSERVATIONS:
        return {"ok": False, "reason": f"need >={MIN_OBSERVATIONS} observations, got {n}",
                "n": n}

    if seed is None:
        seed = (n * 7919 + int(abs(float(arr.mean())) * 1e6)) % (2**31)
    rng = np.random.default_rng(seed)

    p = 1.0 / max(1, block_length)
    means = np.empty(resamples, dtype=float)
    for i in range(resamples):
        idx = np.empty(n, dtype=np.int64)
        pos = rng.integers(0, n)
        for k in range(n):
            idx[k] = pos
            if rng.random() < p:
                pos = rng.integers(0, n)      # start a new block
            else:
                pos = (pos + 1) % n           # continue this one (wraps)
        means[i] = arr[idx].mean()

    alpha = (1.0 - confidence) / 2.0
    lo = float(np.quantile(means, alpha))
    hi = float(np.quantile(means, 1.0 - alpha))
    # Share of resamples on the opposite side of zero from the point estimate:
    # a two-sided bootstrap p-value.
    observed = float(arr.mean())
    p_value = float(np.mean(means <= 0.0) if observed > 0 else np.mean(means >= 0.0)) * 2.0
    return {
        "ok": True,
        "n": n,
        "resamples": resamples,
        "mean": round(observed, 6),
        "ci_low": round(lo, 6),
        "ci_high": round(hi, 6),
        "confidence": confidence,
        "p_value": round(min(1.0, p_value), 4),
        # Excludes zero == the sign is stable across resamples.
        "passes": bool(lo > 0 or hi < 0),
    }


def _sharpe(returns: np.ndarray, periods_per_year: int = TRADING_DAYS_YEAR) -> float:
    if returns.size < 2:
        return 0.0
    sd = float(np.std(returns, ddof=1))
    if sd <= 0:
        return 0.0
    return float(np.mean(returns) / sd * math.sqrt(periods_per_year))


def _norm_cdf(x: float) -> float:
    """Standard normal CDF. math.erf avoids a scipy dependency — the container
    has numpy/scipy but this module is deliberately importable without them."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probabilistic_sharpe_ratio(
    returns: np.ndarray | list[float],
    benchmark_sr: float = 0.0,
    periods_per_year: int = TRADING_DAYS_YEAR,
) -> dict:
    """P(true Sharpe > benchmark), correcting for skew, kurtosis and sample size.

    Bailey & Lopez de Prado (2012). A plain Sharpe ratio assumes normal returns
    and says nothing about how confident it is. Trading returns are neither
    normal nor plentiful: negative skew with fat tails is the classic
    "picking up pennies" profile, and it makes a Sharpe look better than the
    strategy deserves.

        PSR = Phi( (SR - SR*) * sqrt(n-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) )

    where g3 is skewness and g4 is kurtosis (non-excess). Note the signs: for a
    POSITIVE Sharpe, negative skew (g3 < 0) INFLATES the denominator and lowers
    the PSR, which is the correction doing its job.

    Returns a structured verdict — INSUFFICIENT_DATA is distinct from a low
    probability, because "could not check" must never read as "checked and bad".
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < MIN_OBSERVATIONS:
        return {"verdict": "INSUFFICIENT_DATA", "n": int(n),
                "reason": f"need >= {MIN_OBSERVATIONS} observations, got {n}"}

    sd = float(np.std(arr, ddof=1))
    if sd <= 0:
        return {"verdict": "INSUFFICIENT_DATA", "n": int(n),
                "reason": "zero variance"}

    # SR here is PER-OBSERVATION, not annualized: the PSR formula's n-1 term
    # assumes the Sharpe and the sample are on the same footing. Annualizing
    # first would overstate significance by sqrt(periods_per_year).
    sr = float(np.mean(arr) / sd)
    m3 = float(np.mean(((arr - np.mean(arr)) / sd) ** 3))
    m4 = float(np.mean(((arr - np.mean(arr)) / sd) ** 4))

    denom_sq = 1.0 - m3 * sr + ((m4 - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0:
        return {"verdict": "INSUFFICIENT_DATA", "n": int(n),
                "reason": "degenerate higher moments"}

    psr = _norm_cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom_sq))
    return {
        "verdict": "PASS" if psr >= 0.95 else "FAIL",
        "psr": round(psr, 4),
        "sharpe_per_obs": round(sr, 4),
        "sharpe_annualized": round(sr * math.sqrt(periods_per_year), 4),
        "skew": round(m3, 4),
        "kurtosis": round(m4, 4),
        "n": int(n),
    }


def deflated_sharpe_ratio(
    returns: np.ndarray | list[float],
    n_trials: int,
    trial_sharpe_variance: float | None = None,
    periods_per_year: int = TRADING_DAYS_YEAR,
) -> dict:
    """PSR against the Sharpe the BEST OF N TRIALS would reach by luck alone.

    Bailey & Lopez de Prado (2014). This is the correction this repo most needs
    and has never applied. Momentum, low-vol, beta, reversal, several sizing
    rules, HMM regimes and many agent configurations have all been tested
    against the same price history. The best of those is selected for reporting,
    and selection alone inflates the winner's Sharpe — even if every candidate
    is worthless.

    The expected maximum of N independent standard normals is approximately

        E[max] ~ (1-g)*Phi^-1(1 - 1/N) + g*Phi^-1(1 - 1/(N*e))     [g = Euler-Mascheroni]

    scaled by the spread of Sharpes across trials. That becomes the benchmark
    the observed Sharpe must beat.

    `trial_sharpe_variance` is the variance of Sharpe ratios ACROSS the trials
    run. When unknown, the observed series' own variance is used as a proxy —
    which is conservative in the wrong direction (it usually understates the
    spread and so understates the deflation), and the returned dict says so.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < MIN_OBSERVATIONS:
        return {"verdict": "INSUFFICIENT_DATA", "n": int(arr.size),
                "reason": f"need >= {MIN_OBSERVATIONS} observations"}
    if n_trials < 1:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "n_trials must be >= 1"}

    estimated_variance = trial_sharpe_variance is None
    if estimated_variance:
        sd = float(np.std(arr, ddof=1))
        # Variance of the Sharpe ESTIMATOR under the null, the standard
        # approximation: Var(SR_hat) ~ 1/(n-1) for per-observation Sharpe.
        trial_sharpe_variance = 1.0 / max(1, arr.size - 1)

    if n_trials == 1:
        expected_max_sr = 0.0
    else:
        euler = 0.5772156649015329
        # Phi^-1 via the inverse-erf relation, again to avoid scipy.
        def _ppf(p: float) -> float:
            # Acklam's rational approximation is overkill here; bisection on the
            # CDF is exact enough and obviously correct, which matters more.
            lo, hi = -10.0, 10.0
            for _ in range(200):
                mid = (lo + hi) / 2.0
                if _norm_cdf(mid) < p:
                    lo = mid
                else:
                    hi = mid
            return (lo + hi) / 2.0

        q1 = _ppf(1.0 - 1.0 / n_trials)
        q2 = _ppf(1.0 - 1.0 / (n_trials * math.e))
        expected_max_sr = math.sqrt(trial_sharpe_variance) * (
            (1.0 - euler) * q1 + euler * q2
        )

    result = probabilistic_sharpe_ratio(arr, benchmark_sr=expected_max_sr,
                                        periods_per_year=periods_per_year)
    if result.get("verdict") == "INSUFFICIENT_DATA":
        return result
    return {
        "verdict": "PASS" if result["psr"] >= 0.95 else "FAIL",
        "dsr": result["psr"],
        "expected_max_sharpe_from_luck": round(expected_max_sr, 4),
        "observed_sharpe_per_obs": result["sharpe_per_obs"],
        "n_trials": int(n_trials),
        "n": result["n"],
        "trial_variance_estimated": estimated_variance,
    }


def min_track_record_length(
    returns: np.ndarray | list[float],
    benchmark_sr: float = 0.0,
    confidence: float = 0.95,
) -> dict:
    """How many observations are needed before a Sharpe beats the benchmark.

    Bailey & Lopez de Prado. Answers "is this track record long enough to mean
    anything", which is usually the honest response to a promising backtest on
    a few months of data.

        MinTRL = 1 + (1 - g3*SR + (g4-1)/4 * SR^2) * (Z_conf / (SR - SR*))^2
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 3:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "need >= 3 observations"}
    sd = float(np.std(arr, ddof=1))
    if sd <= 0:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "zero variance"}

    sr = float(np.mean(arr) / sd)
    if sr <= benchmark_sr:
        return {"verdict": "NEVER", "n": int(n), "sharpe_per_obs": round(sr, 4),
                "reason": "observed Sharpe does not exceed the benchmark; no "
                          "sample size makes a negative edge significant"}

    m3 = float(np.mean(((arr - np.mean(arr)) / sd) ** 3))
    m4 = float(np.mean(((arr - np.mean(arr)) / sd) ** 4))

    # Z for the confidence level, via the same bisection used above.
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < confidence:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2.0

    required = 1.0 + (1.0 - m3 * sr + ((m4 - 1.0) / 4.0) * sr * sr) * (
        z / (sr - benchmark_sr)
    ) ** 2
    return {
        "verdict": "PASS" if n >= required else "FAIL",
        "min_track_record": int(math.ceil(required)),
        "n": int(n),
        "shortfall": max(0, int(math.ceil(required)) - int(n)),
        "sharpe_per_obs": round(sr, 4),
    }


def is_oos_degradation(
    returns: np.ndarray | list[float],
    is_fraction: float = 0.7,
    periods_per_year: int = TRADING_DAYS_YEAR,
) -> dict:
    """Chronological in-sample vs out-of-sample Sharpe comparison.

    Split is by TIME, never shuffled — a random split leaks future information
    into the training half and is the single most common way a backtest lies.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < MIN_OBSERVATIONS * 2:
        return {"ok": False, "reason": f"need >={MIN_OBSERVATIONS * 2} observations, got {n}",
                "n": n}

    split = int(n * is_fraction)
    if split < MIN_OBSERVATIONS or (n - split) < MIN_OBSERVATIONS:
        return {"ok": False, "reason": "split leaves a half under the minimum", "n": n}

    is_ret, oos_ret = arr[:split], arr[split:]
    is_sharpe = _sharpe(is_ret, periods_per_year)
    oos_sharpe = _sharpe(oos_ret, periods_per_year)

    # Retention is only meaningful when the in-sample edge was positive; a
    # negative IS Sharpe makes the ratio meaningless (and can look "good"
    # when both halves lose money).
    if is_sharpe <= 0:
        retention = None
        passes = False
        note = "in-sample Sharpe <= 0 — nothing to retain"
    else:
        retention = oos_sharpe / is_sharpe
        passes = bool(retention >= OOS_RETENTION_GATE)
        note = ""

    return {
        "ok": True,
        "n": n,
        "n_is": int(split),
        "n_oos": int(n - split),
        "is_sharpe": round(is_sharpe, 3),
        "oos_sharpe": round(oos_sharpe, 3),
        "retention": None if retention is None else round(float(retention), 3),
        "gate": OOS_RETENTION_GATE,
        "passes": passes,
        "note": note,
    }


def full_gate(
    returns: np.ndarray | list[float],
    horizon: int | None = None,
    label: str = "",
) -> dict:
    """Run all three gates and combine them.

    `verdict` is PASS only when every gate that COULD run passed. A gate that
    could not run (too little data) yields INSUFFICIENT_DATA, never a silent
    pass — the whole point is that "we couldn't check" must not read the same
    as "we checked and it's fine".
    """
    nw = newey_west_tstat(returns, horizon=horizon)
    bs = stationary_bootstrap_ci(returns)
    oos = is_oos_degradation(returns)

    ran = [g for g in (nw, bs, oos) if g.get("ok")]
    if not ran:
        verdict = "INSUFFICIENT_DATA"
    elif len(ran) < 3:
        verdict = "PARTIAL_PASS" if all(g.get("passes") for g in ran) else "FAIL"
    else:
        verdict = "PASS" if all(g.get("passes") for g in ran) else "FAIL"

    return {
        "label": label,
        "verdict": verdict,
        "newey_west": nw,
        "bootstrap": bs,
        "is_oos": oos,
    }
