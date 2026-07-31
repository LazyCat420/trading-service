# HANDOFF — measurement integrity & confidence calibration, 2026-07-31

Everything below is shipped, deployed to synology, and verified in the running
container. Two repos: `trading-service` at `cbd4d86`, `trading-client` at `5a375d6`.

## Why this session happened

An audit of `cycle-v3-1785504601` turned into "does any of this machinery
actually work", which turned into "can we even measure whether it works".
The answer to the last one was no, in four separate places. Those are fixed.

## The headline: the edge question is settled, and the answer is "not measurable"

Full write-up in **`docs/EDGE_MEASUREMENT_2026-07-31.md`** — read that before
re-deriving it.

Desk BUYs return +2.74% over 5 bars; the matched baseline (same 288-ticker
universe, same dates) returns −0.20%. That is **+2.94pp at a naive 4.7σ**, and
it is **not real**: `scripts/power_report.py --horizon 5` puts the honest MDE at
**8.84pp** once the design effect cuts effective n from 1802 → 329. Detecting
3pp needs ~4.3× more data — about a year at this cadence.

Context that matters more: **46 trade fills exist in the entire history.** The
1,802 scored outcomes are almost entirely hypothetical.

Do not chase selection alpha here. `power_report.py`'s own conclusion applies:
prefer controls that are self-validating at small n.

## What was actually broken, and is now fixed

### Confidence calibration (`cbd4d86`)
Confidence is stated as a probability and nothing checked that it behaves like
one. Measured over resolved directional calls, 5-point buckets n≥20:

| stated | 60 | 65 | 70 | 75 | 80 | 85 | 90 | 95 |
|---|---|---|---|---|---|---|---|---|
| won | 51.5% | 43.3% | 61.8% | 63.2% | 66.7% | 67.4% | 68.3% | **45.5%** |

The scale mostly works (Kendall τ +0.50 vs win rate, +0.64 vs |P&L|). Two real
defects: **15.8 points of overstatement**, and **the top bucket inverts**.

Nobody caught it because the discrimination term compared
`win_rate(conf>=70)` against `win_rate(conf<50)` — a two-bucket gate that
ignores 50–69, cannot see a collapse inside ≥70, and scores this data ~1.0.
Four fixes:

1. Discrimination is now **Kendall's tau over every qualified bucket**.
2. New **`app/autoresearch/confidence_calibration.py`** — isotonic (PAVA) map
   from stated → earned. **Stated 95 calibrates to 65.3.** PAVA *pools* the
   inversion rather than deleting it, and refuses to extrapolate outside the
   evidenced range.
3. The report carries both taus + `confidence_predicts`. The honesty/
   discrimination split separates level from ordering: live cohort scores
   honesty **0.335**, discrimination **1.0**.
4. **The source:** `TRADE_DECISION_SCHEMA.confidence` in `app/v3/artifacts.py`
   — the field that reaches `decision_outcomes` — **had no description at
   all.** It now states P(correct at 7d) and quotes the record back.

**SCORE_VERSION v4 → v5.** Scores either side are not comparable on the
calibration term.

### Blocked decisions were credited as kept trades (`1248883`)
On a policy block, `shared_desk.final_decision` and `analysis_results.action`
both still read `BUY`; the refusal lives only in `trade_results.policy_action`.
The provenance check is `board_action != action`, so it never fired. All 19
blocked decisions sat in `override_scorecard`'s **`kept_buys`** bucket — the
desk credited with keeping trades the floor refused — and were graded WIN/LOSS
as if taken.

Fixed + backfilled 19 rows. New `blocked_by_gate` bucket, ordered before
`kept_buys`. This unlocked a back-test that was impossible before: blocked
trades average **−1.72%** vs **+2.77%** for allowed. Only 5 of 19 resolved, so
the code deliberately withholds a verdict below 20 rows.

### DEGRADED_ARTIFACT leaking into scores (`1248883`)
361 pipeline crashes scored as trades (mean −5.75%, zero fills). Most consumers
already excluded them correctly; these did not, and now do: `decision_audit`
(window, has-history gate, and the `cycle_summaries` backfill that copied a
crash's P&L), `challenger_router._champion_correct` (was a **deny-list** — any
unknown label graded as "this side was wrong"), `base_agent`'s PRIOR TRADE
HISTORY prompt (was teaching analysts from our outages), and `power_report`
(exclusion is now the **default**).

Note: the v4 score was correct only *by accident* of `ORDER BY … LIMIT 100`
cutting the rows out. It is now correct by filter.

## New tooling

**`scripts/cycle_audit.py`** — `--watch` a live cycle, `--check` to grade one
(exit 1 on failure, so it can gate a deploy or run from cron). Ten checks, each
encoding a defect that actually happened, with the observed bad value as the
threshold. Validated as a positive control: it independently reproduced all
nine findings of the manual audit.

## Also shipped today
- Precollect **slow lane** for `multi_api_news`/`youtube` (`5c3cfd0`): slow
  collectors went from 0-of-12 in-budget to **15 of 16**.
- Screener: added `ev_to_ebit`, sanitized JSON punctuation off filter tokens,
  fixed `in` filters silently matching nothing (returned 0 rows with HTTP 200).
- `lazy_web_search`: "no results" no longer reports as "provider outage".
- Regressing-sectors gate: vendor sector taxonomies folded, noise floor added.
- Evolution panel: stopped rendering a retired archive as a live work queue;
  added a CORAL panel showing the loop that actually runs.
- Ran `ANALYZE` database-wide — `price_history` estimates were **508× wrong**.

## Open, in rough priority order

1. **Autovacuum has never fired on this database.** I ran `ANALYZE`; the
   estimates will drift back. Root cause unknown. Also: `n_live_tup` is not a
   row count — it reported two populated tables (93,553 rows) as *empty*.
2. **Stale-price enforcement** is still uncommitted in the `stale-vendor-wave`
   worktree (another session's WIP). Top correctness item, unchanged.
3. **Duplicate analyst runs** — 24 extra in the last cycle, ~30% of 7.9M
   tokens. Fix is in that same worktree.
4. **Tool catalog is generated by a script nobody runs** — `get_sec_filings`
   fails ~20% because the 07-29 fix never reached the artifact agents read.
5. **SDK required-field validation** only fires when junk keys are *also*
   present; a plain missing field dies as a raw `TypeError`. One-line fix in
   `lazycat-sdk`, but it redeploys every consumer.
6. **CORAL runner** — one repair job queued since 07-29 because nothing runs
   `scripts/evo_runner.py` (needs a host checkout with git + pytest).

## Verify any of this
```bash
scripts/cycle_audit.py --check          # 10 checks; exit 1 on failure
scripts/power_report.py --horizon 5     # honest MDE; excludes degraded by default
python -c "from app.autoresearch.confidence_calibration import calibration_map; print(calibration_map())"
```
