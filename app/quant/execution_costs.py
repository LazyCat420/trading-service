"""
Transaction cost model — what a trade actually costs to put on.

## Why this exists

Every performance number this service produced before 2026-07-26 was **gross of
all trading costs**. Audited on the live book: `fees` was 0 on all 44 fills, and
`paper_trader` filled at exactly the reference close price. No spread, no
slippage, no commission, no impact.

That is not a rounding error. Chen et al. 2026 ("Toward Reliable Evaluation of
LLM-Based Financial Multi-Agent Systems", arXiv:2603.27539) re-evaluated FinMem's
published **+23% return and got -22%** once costs were applied — a sign reversal
from costs alone. They define five minimum standards for this class of system;
net-of-cost returns is the one this module addresses.

The live measurement already shows the pipeline trailing an always-long baseline
(+1.53% vs +2.14%) *gross*. Costs can only widen that. The purpose here is to be
able to say by how much.

## The three components

1. **Spread** — half the bid-ask, paid on entry AND exit. No quote data is
   stored, so this is estimated from **ADV liquidity tiers**, calibrated to
   published US equity effective spreads.

   Corwin-Schultz (2012) was implemented first and rejected on evidence. On
   synthetic data with a known 50bp half-spread it recovers 50.0bp exactly at
   zero volatility — the implementation is correct — but only 29.9bp once
   realistic 1.5% daily volatility is added. Run against the live book it
   returned **~30bp for AAPL, whose true half-spread is under 1bp**: a 60x
   overcharge, because on liquid names the estimator is dominated by intraday
   range rather than spread. A cost model that overcharges by 60x would kill
   every strategy on contact and prove nothing about any of them.

   Tiers are cruder but honest about being crude, and they are right to within
   a factor of ~2 rather than ~60. `corwin_schultz_half_spread_bps` is kept and
   tested because it is genuinely useful for illiquid names where the range IS
   mostly spread — it is simply not the default.

2. **Commission** — per-share and/or per-trade. Defaults to zero (this is a
   paper book) but exists so a real venue can be modeled without a code change.

3. **Market impact** — the square-root law, `k · σ · sqrt(value / ADV)`. The
   standard practitioner form (Almgren et al. 2005). Impact is paid on the way
   in and again on the way out, and it is the component that actually punishes
   size — the one a frictionless backtest most obviously misses.

## What this is NOT

A paper book has no real fills, so this is a defensible **estimate**, not ground
truth. Real ground truth needs a real broker and realized implementation
shortfall (see `scripts/execution_quality.py`, which measures the residual once
these costs are live). Treating a modeled cost as a measured one would be the
same laundering this codebase keeps finding elsewhere.

Every component is separately overridable and separately testable, so a
disagreement about (say) the impact coefficient does not require rejecting the
spread estimate too.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
#
# Chosen to be defensible for a US large-cap paper book and deliberately NOT
# optimistic. Each is a keyword argument, so a caller who disagrees can say so
# at the call site rather than editing this file.

# Floor on the half-spread, in basis points. Even the most liquid US large caps
# do not trade at zero spread; 1bp round-trip is roughly the observed floor for
# mega-cap names. Without a floor, the Corwin-Schultz estimator returns 0 or
# negative on quiet days and hands back a free trade.
MIN_HALF_SPREAD_BPS = 0.5

# Ceiling on the half-spread, in basis points. The high-low estimator degenerates
# on gappy or illiquid series and can imply a 40% spread, which would dominate
# every other term and silently turn the model into a spread-only model.
MAX_HALF_SPREAD_BPS = 50.0

# Fallback half-spread when there is not enough price history to estimate one.
# Deliberately ABOVE the large-cap floor: an unknown name is more likely to be
# illiquid than not, and the conservative direction here is to charge more.
DEFAULT_HALF_SPREAD_BPS = 2.5

# Square-root impact coefficient. Almgren et al. (2005) estimate ~0.3-1.0
# depending on venue and horizon; 0.5 is the common practitioner midpoint.
IMPACT_COEFFICIENT = 0.5

# Commissions default to zero — this is a paper book. Present so a real venue is
# a parameter change, not a code change.
DEFAULT_COMMISSION_PER_SHARE = 0.0
DEFAULT_COMMISSION_PER_TRADE = 0.0

# Below this many price observations the Corwin-Schultz estimate is noise.
MIN_BARS_FOR_SPREAD = 20

# ── Liquidity tiers: ADV (dollars) -> half-spread (bps) ─────────────────────
#
# Calibrated to published US equity effective spreads. Mega caps (>$5B ADV)
# quote at or near the $0.01 minimum tick, which on a $200 stock is ~0.25bp
# half-spread; small caps routinely run 25-50bp+.
#
# These are ESTIMATES, and deliberately rounded rather than precise-looking:
# a value like 0.5 says "about half a basis point", which is the actual state of
# knowledge here. Spurious precision (0.47) would imply a measurement nobody
# made. Every one is overridable at the call site.
#
# Ordered most-liquid first; the first tier whose threshold the ADV clears wins.
_LIQUIDITY_TIERS: tuple[tuple[float, float], ...] = (
    (5_000_000_000.0, 0.5),    # mega cap (AAPL, MSFT, SPY) — tick-constrained
    (1_000_000_000.0, 1.0),    # large cap
    (200_000_000.0, 2.5),      # mid cap
    (50_000_000.0, 5.0),       # small cap
    (10_000_000.0, 15.0),      # micro cap
    (0.0, 40.0),               # illiquid — wide, and it should hurt
)


def half_spread_bps_from_adv(adv_value: float | None) -> float | None:
    """Half-spread in bps from average daily dollar volume.

    Returns None when ADV is unknown — NOT a default. "Could not estimate" and
    "estimated as cheap" are different claims, and the caller decides which
    fallback is appropriate rather than having one silently applied here.
    """
    if adv_value is None or adv_value <= 0:
        return None
    for threshold, bps in _LIQUIDITY_TIERS:
        if adv_value >= threshold:
            return bps
    return _LIQUIDITY_TIERS[-1][1]

# Guard for the impact term: an order larger than this share of ADV is outside
# the square-root law's calibrated range, and the honest response is to flag it
# rather than extrapolate a number that looks precise.
MAX_SANE_ADV_PARTICIPATION = 0.25


def corwin_schultz_half_spread_bps(
    highs: list[float], lows: list[float],
) -> float | None:
    """Estimate the half-spread in basis points from daily high/low ranges.

    Corwin & Schultz (2012), "A Simple Way to Estimate Bid-Ask Spreads from
    Daily High and Low Prices", Journal of Finance. The insight: the high-low
    range over two consecutive days reflects both volatility and the spread, but
    volatility scales with time while the spread does not — so two-day and
    one-day ranges can be differenced to isolate the spread.

    Returns None when there is not enough data, NEVER 0. "Could not estimate"
    and "estimated as free" are different claims, and collapsing them is how a
    cost model silently becomes a no-op.
    """
    if len(highs) != len(lows) or len(highs) < MIN_BARS_FOR_SPREAD:
        return None

    estimates: list[float] = []
    for i in range(1, len(highs)):
        h0, l0 = highs[i - 1], lows[i - 1]
        h1, l1 = highs[i], lows[i]
        # A non-positive or inverted bar is bad data, not a zero spread.
        if min(h0, l0, h1, l1) <= 0 or h0 < l0 or h1 < l1:
            continue

        # Single-day log ranges, and the two-day range spanning both bars.
        beta = math.log(h0 / l0) ** 2 + math.log(h1 / l1) ** 2
        h2, l2 = max(h0, h1), min(l0, l1)
        gamma = math.log(h2 / l2) ** 2

        denom = 3.0 - 2.0 * math.sqrt(2.0)
        alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / denom
        alpha -= math.sqrt(gamma / denom)
        if alpha != alpha or math.isinf(alpha):  # NaN/inf from degenerate bars
            continue

        # s = 2(e^a - 1)/(1 + e^a) is the FULL spread as a fraction of price.
        try:
            ea = math.exp(alpha)
        except OverflowError:
            continue
        spread = 2.0 * (ea - 1.0) / (1.0 + ea)
        # Corwin-Schultz produces negative estimates on ~half of quiet days by
        # construction; the paper's own guidance is to floor them at zero before
        # averaging rather than discard them (discarding biases the mean UP).
        estimates.append(max(0.0, spread))

    if not estimates:
        return None

    mean_full_spread = sum(estimates) / len(estimates)
    half_bps = (mean_full_spread / 2.0) * 10_000.0
    return max(MIN_HALF_SPREAD_BPS, min(MAX_HALF_SPREAD_BPS, half_bps))


def market_impact_bps(
    order_value: float,
    adv_value: float | None,
    daily_volatility: float | None,
    *,
    coefficient: float = IMPACT_COEFFICIENT,
) -> tuple[float, str | None]:
    """Square-root market impact, in basis points. Returns (bps, warning).

    `impact = k · σ · sqrt(order_value / ADV)`, the standard practitioner form
    (Almgren et al. 2005). Impact is what makes size expensive; a model without
    it says a $10M order costs the same per share as a $10k one.

    Returns 0.0 with a warning when ADV or volatility is unavailable — this is
    the one component where "unknown" genuinely does mean "cannot charge",
    since a made-up impact number is worse than an acknowledged gap. The
    warning exists so the caller can report the gap rather than bank the zero.
    """
    if order_value <= 0:
        return 0.0, None
    if not adv_value or adv_value <= 0:
        return 0.0, "no ADV — impact not modeled"
    if daily_volatility is None or daily_volatility <= 0:
        return 0.0, "no volatility — impact not modeled"

    participation = order_value / adv_value
    warning = None
    if participation > MAX_SANE_ADV_PARTICIPATION:
        # Outside the square-root law's calibrated range. Compute it anyway —
        # refusing to would understate a genuinely expensive trade — but say so.
        warning = (
            f"order is {participation:.0%} of ADV, beyond the model's "
            f"calibrated range ({MAX_SANE_ADV_PARTICIPATION:.0%})"
        )

    impact_fraction = coefficient * daily_volatility * math.sqrt(participation)
    return impact_fraction * 10_000.0, warning


def estimate_cost_bps(
    *,
    order_value: float,
    quantity: float = 0.0,
    half_spread_bps: float | None = None,
    adv_value: float | None = None,
    daily_volatility: float | None = None,
    commission_per_share: float = DEFAULT_COMMISSION_PER_SHARE,
    commission_per_trade: float = DEFAULT_COMMISSION_PER_TRADE,
    impact_coefficient: float = IMPACT_COEFFICIENT,
) -> dict:
    """Total one-way cost of a trade, in basis points, with its components.

    ONE WAY. A round trip pays this twice, and the caller is responsible for
    applying it on both entry and exit — folding the round trip in here would
    double-charge anything held across a reporting boundary.

    Returns the breakdown, not just a total, so a reader can see which component
    dominates. A single blended number invites the "just use 10bps" shortcut
    that hides an order being 40% of a day's volume.
    """
    # Precedence: an explicit caller-supplied spread, then the ADV tier, then
    # the conservative default. The tier is preferred over the default whenever
    # ADV is known, because "we know this is a mega cap" beats "assume mid cap".
    spread = half_spread_bps
    spread_source = "explicit"
    if spread is None:
        spread = half_spread_bps_from_adv(adv_value)
        spread_source = "adv_tier"
    if spread is None:
        spread = DEFAULT_HALF_SPREAD_BPS
        spread_source = "default"
    spread = max(MIN_HALF_SPREAD_BPS, min(MAX_HALF_SPREAD_BPS, spread))

    impact, impact_warning = market_impact_bps(
        order_value, adv_value, daily_volatility, coefficient=impact_coefficient,
    )

    commission_cash = abs(quantity) * commission_per_share + commission_per_trade
    commission = (
        (commission_cash / order_value) * 10_000.0 if order_value > 0 else 0.0
    )

    total = spread + impact + commission
    return {
        "total_bps": total,
        "spread_bps": spread,
        "spread_source": spread_source,
        "impact_bps": impact,
        "commission_bps": commission,
        "commission_cash": commission_cash,
        "warning": impact_warning,
        # True when every component was estimated from real inputs. A caller
        # reporting net returns should say so when this is False, rather than
        # presenting a partially-modeled cost as a complete one.
        "fully_modeled": spread_source != "default" and impact_warning is None,
    }


def apply_cost_to_fill(
    reference_price: float, side: str, cost_bps: float,
) -> float:
    """The price a fill actually happens at, given a reference price.

    Costs always work AGAINST the trader: a BUY fills higher, a SELL fills
    lower. Getting this sign wrong turns a cost model into an alpha source,
    which is the single most dangerous bug this module could have — it would
    make every strategy look better the more it traded.
    """
    if reference_price <= 0:
        return reference_price
    adjustment = reference_price * (cost_bps / 10_000.0)
    if str(side).strip().upper() == "BUY":
        return reference_price + adjustment
    return reference_price - adjustment


def net_return_pct(gross_return_pct: float, round_trip_cost_bps: float) -> float:
    """Restate a gross return net of a round-trip cost.

    Used to re-express historical decisions that were recorded gross. The cost
    is subtracted from the RETURN, not the price, because the stored figure is
    already a percentage move.
    """
    return gross_return_pct - (round_trip_cost_bps / 100.0)
