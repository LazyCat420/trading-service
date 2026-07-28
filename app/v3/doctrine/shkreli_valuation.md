# Valuation doctrine

> **STATUS: HAND-WRITTEN PLACEHOLDER (2026-07-27).** This is not mined output.
> It is a deliberate baseline written before `scripts/mine_shkreli_doctrine.py`
> runs, so that the agent, the artifact and the measurement are live and
> scoreable first — and so the mined doctrine has to BEAT something rather than
> merely exist. Do not present it as a distillation of anyone's public
> commentary; it is the obvious method, written down. When the mine promotes a
> reviewed doctrine, it replaces this file wholesale.

Every rule below names a metric the PRECOMPUTED VALUATION MATH block actually
emits. A rule that asks for a number nothing computes is a rule that cannot
fire, and it belongs in the draft YAML, not here.

## 1. Solve for the expectation before you judge the price
`implied_growth_pct` is the first number to read, not the last. It answers what
the market is *asserting* about this business. A price is not high or low on its
own — it encodes a forecast, and your job is to decide whether that forecast is
reasonable. State it explicitly in `price_implied_assumption` before you form a
verdict.

## 2. The gap between implied and realized IS the thesis
Compare `implied_growth_pct` against the like-for-like realized series
(`ebit_cagr_pct` when the DCF ran on NOPAT, `fcf_cagr_pct` when it ran on cash).
A price demanding 17%/yr from a business that has compounded at 10%/yr is making
a specific, checkable claim. Name the size of that gap. If the two are close,
the honest answer is FAIR.

## 3. Enterprise value, never market cap
Use `enterprise_value`. Net debt is a real claim on the business that the equity
does not escape, and two companies with identical market caps and different
balance sheets are not similarly priced. If `enterprise_value` is NOT COMPUTABLE
because no balance sheet is on file, say so — do not substitute market cap.

## 4. Most things are fairly valued
FAIR is the default and the most common correct answer. OVERVALUED and
UNDERVALUED are claims that the market has got something specific wrong, and
they require you to say *what*. An agent that finds mispricing everywhere is
finding noise.

## 5. Cheap and shrinking is not cheap
A low `ev_to_ebit` beside a negative `revenue_cagr_pct` or `ebit_cagr_pct` is a
melting ice cube, not a bargain. The multiple is low *because* the market has
priced the decline. Before calling anything UNDERVALUED on a low multiple,
check that the underlying series is not falling.

## 6. Expensive is a claim about the future, not a verdict
A high `ev_to_ebit` or `pe_ratio` is not by itself OVERVALUED. It is only
overvalued if the growth required to justify it exceeds what the business can
plausibly deliver — which is precisely what rule 2 measures. Say which.

## 7. A multiple alone is not an argument
`ev_to_ebit` means nothing without a comparison. Use `screener_query` to pull
sector peers and compare against them, not against a remembered absolute
threshold. "18x is expensive" is not analysis; "18x against a sector median of
11x, with slower growth than the median" is.

## 8. Check the balance sheet before the income statement on anything levered
`net_debt_to_ebit` above ~4x changes the question from "what is this worth" to
"who gets paid". High leverage narrows the range of outcomes the equity
survives, and it belongs in the bear case as a mechanism, not a caveat.

## 9. Cash beats accounting profit
Prefer `fcf_yield_pct` and `ev_to_fcf` over `pe_ratio` wherever the block emits
them. Where it does not — currently the common case, because no free-cash-flow
data exists in this system — say so in `data_gaps` rather than silently treating
EBIT as cash. The reverse-DCF caveat line tells you which basis was used.

## 10. Name the number that would change your mind
`what_would_change_my_mind` must contain a threshold, not a mood. "If EBIT
growth comes in under 6% for two consecutive quarters" is falsifiable. "If
fundamentals deteriorate" is not, and a thesis that cannot be wrong cannot be
right either.

## 11. Margin of safety is the width of your error, not your confidence
`margin_of_safety_pct` is the gap between price and your fair value estimate.
It exists because your estimate is wrong by some amount you cannot measure. A
5% gap is not a margin of safety; it is a rounding difference on your own
assumptions.

## 12. Say which method produced the number
`fair_value_basis` must name the multiple and what it was applied to — "14x
EV/EBIT on TTM operating income of $11.2B" — so the estimate can be checked and
disagreed with. A fair value with no stated basis is an opinion wearing a
decimal point.
