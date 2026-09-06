# Audit: do the decision's `signal_weights` mean anything?

Read-only. Data pulled 2026-09-06 from `trading_bot.trade_results` (Mongo), 1,237 rows
(task brief said 1,232 — 5 more cycles landed between the brief being written and this
run; the desk is live). Code read from `/home/lazycat/github/projects/sun/trading-service`
(no edits made).

## Bottom line

`signal_weights` is decorative. It is LLM-authored metadata that is validated for
**shape** only (`app/v3/artifacts.py:normalize_signal_weights` — 4 canonical keys,
finite, non-negative, sums to 1) and is never fed into any downstream arithmetic:
not to choose `action`, not to compute `internal_consensus_score`, not to size the
trade. The one number that *does* move money — `internal_consensus_score` — is an
**independent** top-level field the model declares directly in the same JSON blob;
it is not a function of `signal_weights` and there is no stored per-signal numeric
score it could be combined with even if someone wanted to recompute it. The only
consumer that reads the weight *values* is a debug/replay API endpoint that echoes
them back verbatim for display. Two other call sites check only whether the field
is non-empty (a boolean), never what it says.

Separately: the `signal_weights_source` provenance field this audit was asked to
distribute — the very thing that would let you tell "the model chose this" from
"we equalized it" — was **stamped for the first time today** (2026-09-06, the same
day as this audit). 1,228 of 1,237 rows (99.3%) have no value in that field at all;
only 9 rows (all from today) carry `model` (8) or `default_equalized` (1). For
99.3% of history, "was this weighted or equalized" is simply not recorded by that
field, and has to be inferred from the vector's values instead (see §1).

## 1. Distribution of sources

**Literal `signal_weights_source` field** (the persisted provenance):

| source | all-time (n=1,237) | last 30d (n=347) |
|---|---|---|
| `default_equalized` | 1 (0.08%) | 1 (0.29%) |
| `model` | 8 (0.65%) | 8 (2.31%) |
| `model_normalized` | 0 (0%) | 0 (0%) |
| **unstamped (field absent/null)** | **1,228 (99.27%)** | **338 (97.41%)** |

All 9 non-null rows are from **2026-09-06** (today) — timestamps 03:28 through 13:40 —
because the stamping code (`app/v3/agent_runner.py:295-310`, `app/services/trade_result_saver.py:53-56`,
`app/v3/artifacts.py:929-993`) reads as a very recent addition (its own header comment
is dated "MEASURED 2026-09-06"). **The source distribution the audit asked for cannot
be read from this field for 99%+ of the table** — it only exists going forward from today.

**By-value proxy** (the only thing available for history): classify a row's stored
4-key vector as "equalized" when all four weights equal exactly 0.25, since that is
the literal `DEFAULT_EQUALIZED_WEIGHTS` constant in code:

- 93/1,237 (7.5%) rows: `signal_weights` is an **empty dict** — no vector at all.
- 7/1,237 (0.6%): non-canonical key set (extra/renamed keys) — unusable as-is.
- 1/1,237 (0.08%): canonical keys but sum off by >0.01 from 1.0.
- 1,136/1,237 (91.8%) rows: clean canonical vector (4 keys, sums to 1 ± 0.01).
  - **472/1,136 (41.5%) — 38.2% of all rows** — are exactly `0.25/0.25/0.25/0.25`,
    the equalized-default signature.
  - 664/1,136 (58.5%) — 53.7% of all rows — have a genuinely varying vector.
  - Last 30 days: 306 canonical rows, 91 (29.7%) exactly equalized, 215 (70.3%) varying.

This matches a prior in-repo measurement already sitting in `app/v3/artifacts.py:895-919`
(comment dated 2026-09-06, n=1,134 vs. our n=1,136 — 2 more cycles since): canonical
1,127 vs 1,136, non-canonical 7, bad-sum 2 vs 1, equalized 471 (41.5%) vs 472. Consistent.

**Conclusion for §1**: by the only measurement actually available (the vector's
values, not the provenance field), the weighting machinery is *not* purely decorative
in the "always equalized" sense — roughly 54-70% of rows (depending on window) carry
a non-trivial, non-equal vector. But whether that vector was a genuine model
judgment or an unlabeled equalized fallback that happened to land elsewhere is
architecturally unanswerable for 99.3% of the table, because the field built to
answer that question didn't exist until today.

## 2. Do the weights vary?

Per-key stats over the 664 **non-equalized** canonical rows (i.e. excluding the
472 exact-0.25 rows, which would otherwise flatten the spread):

| key | min | median | max | sd (population) |
|---|---|---|---|---|
| board | 0.000 | 0.250 | 0.500 | 0.0792 |
| quant | 0.000 | 0.250 | 0.500 | 0.0671 |
| fundamental | 0.000 | 0.300 | 0.500 | 0.0936 |
| debate | 0.000 | 0.200 | 0.500 | 0.0618 |

(Over *all* 1,136 canonical rows, including the equalized ones, sd shrinks to
~0.05-0.07 per key — the equalized rows pull toward the center, as expected.)

Each key ranges the full plausible span (0 to 0.5) with real spread (sd 0.06-0.09),
so this is not "a constant with extra steps" in the way a value pinned to
0.24-0.26 would be. It is also not tightly obeying the prompt's own suggested bands
(`decision_agent.py:29`: board 0.40-0.50, quant 0.20-0.25, fundamental 0.15-0.20,
debate 0.10-0.15) — median `fundamental` (0.30) and median `board` (0.25) both sit
outside the prompt's suggested range, i.e. the model does not reliably follow its
own instructions on this field either.

## 3. Do they change the answer? — NOT REPRODUCIBLE

The task asked to recompute each decision's `internal_consensus_score` with (a)
stored weights and (b) equal weights, and count flips/confidence swings. That
requires a formula: `consensus = f(signal_weights, per-signal sub-scores)`. **No
such formula exists anywhere in the codebase**, and the missing input is:

> **A numeric per-signal score for `quant`/`fundamental`/`debate`/`board` to
> multiply against `signal_weights`.** The only per-signal field stored alongside
> the weights is `signal_assessments`, and it is free-text prose, not a score:
> sampled 4,540 individual values across all rows with a populated
> `signal_assessments` dict — **0 were numeric** (e.g. `"board": "Hold; correctly
> identifies the lack of margin of safety..."`). There is nothing to multiply the
> weights by.

Tracing the actual code:
- `action` and `confidence` are output directly by the Decision Synthesizer LLM
  (`app/v3/agents/decision_agent.py` system prompt, lines 25-82) — not computed
  from `signal_weights` in code anywhere.
- `internal_consensus_score` is likewise a top-level number the same LLM call
  declares directly (prompt line 30, 68) — present on only **651/1,237 rows
  (52.6%)**. It correlates with `confidence` (median difference 0) but is a
  distinct number, not a copy: exact equality in only 146/651 (22.4%) of rows,
  and the difference ranges from -23 to +100.
- The **only** downstream arithmetic use of `internal_consensus_score` is in
  `app/services/pipeline_service.py:322-357` (`resolve_buy_size_pct`): the
  explicit agent-set `position_size_pct` is multiplied by
  `max(0.5, internal_consensus_score/100)`, and halved again if
  `conviction_vector.data_quality < 60` — this is confirmed in
  `app/v3/orchestrator.py:3595-3601` ("Consensus + data-quality feed the code-side
  sizing haircut ... 2026-07-21: formulas moved out of the synthesizer prompt into
  code"). **`signal_weights` itself never appears in this or any other formula.**
- `normalize_signal_weights` (`app/v3/artifacts.py:929-993`) is the one place that
  reads the weight values arithmetically — only to validate/repair shape (drop
  non-canonical keys, rescale a bad-sum vector so it sums to 1). Its output feeds
  nowhere except storage.

Per the audit's own stated trap: even if a "recompute" were attempted here, running
`internal_consensus_score` through the same declaration the model already made
would just reproduce the stored number — there is no monotone mapping to invert
because there was never a mapping in the first place. **Reported per the
instructions: stop here.** `flips` and `confidence_moves_gt10` are not computable
and are reported as 0/placeholder in the summary JSON, not as a measured "no
flips found" result — do not read them as evidence the weights don't matter;
read them as "there is nothing to run the counterfactual on."

## 4. Provenance fields

None of the five fields are literally constant (all have ≥2 distinct values), but
several are heavily skewed toward one or two values plus a large null share:

- **`decision_provenance`** (3 distinct + null): `None` 595 (48.1%), `board_reasoned`
  637 (51.5%), `coerced_unshortable` 5 (0.4%). Effectively binary in practice
  (present vs. absent), and "present" means one value.
- **`persona_used`** (9 distinct incl. null, but really 4 personas under
  inconsistent casing): `jane_street` 642, `warren_buffett` 495, `delta_analyst` 46,
  `jim_simons` 30, plus casing variants `Jane Street` 11, `Warren Buffett` 7,
  `Quant-Heavy` 1, empty string 3, null 2. The same persona is recorded under
  ≥2 spellings (`jane_street` vs `Jane Street`, `warren_buffett` vs
  `Warren Buffett`) — a normalization gap, not information.
- **`regime`** (7 distinct incl. empty): `CONTRADICTORY` 663 (53.6%), `DEEP_DISCOUNT`
  499 (40.3%) — these two alone are 94% of the table — `delta_relook` 46,
  `HIGH_VOLATILITY` 21, a 4-row compound value
  `HIGH_VOLATILITY|DEEP_DISCOUNT|CONTRADICTORY`, `EXPANSION` 1, empty 3.
- **`policy_action`** (5 distinct + null): `None` 559 (45.2%), `HOLD_NO_SIGNAL` 597
  (48.3%) — together 93.5% — `HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` 38,
  `EXECUTE_BUY` 30, `HOLD_NO_POSITION` 8, `EXECUTE_SELL` 5.
- **`dynamic_trigger`** (object or null): populated (non-null) on 599/1,237 (48.4%).
  Among those, `.type` takes **68 distinct string values** — dominated by
  `sma_50_drop` (272/599, 45.4%) but with a long invented tail (`support_test`,
  `close_above_sma_50`, `resistance_breakout`, `sma_100_drop`, ...). The decision
  prompt (`decision_agent.py:31`) restricts valid types to 9 canonical names and
  states an invented one "is discarded, leaving you no watch at all" — the stored
  row keeps the model's original (possibly invented) text, so a meaningful chunk
  of these 599 triggers are stored but were never evaluable by the execution
  monitor. (Flagged for completeness; not the audit's primary target.)

None of these carries *zero* information (none is a true single-value constant),
but `decision_provenance` and `policy_action` are each effectively a two-state flag
(present/absent, one real value when present), and `regime`/`persona_used` are
dominated by 1-2 values with a long thin tail plus data-quality noise (casing).

## 5. Readers of `signal_weights`

Repo-wide grep for `signal_weights` (all `.py` files, excluding tests) turns up
exactly 8 non-test files. Only one of them reads the *values* for a purpose other
than shape validation or a presence check:

- **`app/routers/cycle_replay_router.py:99-111`** (`_latest_trade_row`) and
  **`:538-560`** (cycle-ticker-detail response builder) — pulls `signal_weights`
  out of Mongo/SQL and echoes it verbatim into the JSON response of
  `GET /api/v1/cycles/...` (router mounted at `app/routers/cycle_replay_router.py:177`,
  prefix `/api/v1/cycles`). This is a debug/replay endpoint for human inspection
  (presumably rendered by the trading-client UI, which lives in a separate repo
  not read for this audit) — no computation performed on the value.
- **`app/v3/quality_scorer.py:239`** — `signal_weights` is one of 5 fields in
  `_OPTIONAL_FIELDS["trade_decision"]`; `_score_data_completeness` (line 238-260)
  only checks whether the field is non-empty, contributing to a 0-100
  "data completeness" score. Never reads what the weights say.
- **`scripts/agent_contract_report.py:54`** — `("synth", "signal_weights
  non-empty", bool(td.get("signal_weights")), ...)` — same presence-only check,
  for a contract-compliance report.
- **`app/v3/artifacts.py:929-993`** (`normalize_signal_weights`) — the only place
  that reads the numeric values, solely to validate/repair the vector's shape
  (drop non-canonical keys, rescale to sum 1). Its output is written back to
  storage, not consumed by any scoring or execution logic.
- **`app/v3/agent_runner.py:260-310`**, **`app/services/trade_result_saver.py`**,
  **`app/v3/orchestrator.py`** — write/normalize/pass-through paths (producer
  side), not readers.

Grepped separately and found **zero** hits for `signal_weights` in every
calibration/backtest/scoring module in the repo: `app/autoresearch/confidence_calibration.py`,
`app/quant/confidence_calibration.py`, `app/quant/decision_score.py`,
`app/autoresearch/outcome_tracker.py`, `app/autoresearch/eval_engine.py`,
`app/autoresearch/auditors/decision_audit.py`, `app/routers/eval_trust_router.py`.
**If nothing reads them for anything but display and a boolean presence check,
that is itself the finding.**

## Summary of what would need to change

For `signal_weights` to carry information that reaches a trading outcome, either:
(a) a formula would need to exist combining `signal_weights` with some per-signal
numeric score to produce `action`/`confidence`/`internal_consensus_score`
(currently absent — `signal_assessments` is prose, not scores), or (b) some
consumer downstream of storage would need to branch on the weight values
(currently absent — the only reader echoes them to a display API). Today,
neither exists; the vector is captured, validated for shape, and then read only
by a debug view and two "did the model bother to fill this in" checks.
