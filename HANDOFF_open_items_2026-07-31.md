# Open items from HANDOFF_measurement_integrity — worked to completion, 2026-07-31

All six items from the previous handoff's "Open, in rough priority order" are
closed. Two of them were closed by disproving their premise rather than by
doing the work they described, and one turned up a defect on the order path
that nobody had listed.

Shipped: trading-service `master@353fabe` (deployed twice), lazycat-sdk `0.3.3`,
lazy-agent-service `d645532`. Every claim below was checked against the live
container or the live database, not against the source tree.

---

## 1 & 2. Stale-price enforcement + duplicate analyst runs — LANDED

Both were uncommitted WIP in the `stale-vendor-wave` worktree (idle 7 hours).
Reviewed, verified (2701 unit tests), split into four commits by concern, and
merged. Nothing was rewritten; the work was already good.

- **Bad-bar salvage** — the frame-level OHLC check rejected all 125 rows for one
  internally inconsistent bar, and because the bad bar stays inside the 6-month
  fetch window it re-failed *every* collection. RBLX/EC wrote nothing for 10
  sessions while the desk priced RBLX 24% off.
- **Freshness-first vendor pinning** — the row-count tie-break kept choosing a
  vendor that had stopped writing, with a fresh series in the same table.
- **Stale-price gate promoted from shadow** to
  `HOLD_POLICY_BLOCKED_STALE_PRICE_DATA`.
- **Research-tier dispatch latch** — a `desk_note` re-write is the JA answering a
  peer request, not a new research phase. It was re-queueing FA+QA+VA whose runs
  were already complete: 17 of the cycle's 18 extra analyst runs.
- **Boilerplate regex** — unanchored `n/?a` matched "operatioNAl", "sigNAls".
  32 of 58 artifacts were flagged FALLBACK_OUTPUT, 100% false positives.

## 3. get_sec_filings ~20% failures — ROOT CAUSE WAS NOT THE STALE ARTIFACT

The handoff blamed a catalog the build script never regenerated. The artifacts
were in fact byte-identical across all three repos and already carried the
07-29 single-argument schema. The real cause was in the SDK:

**`ToolRegistry.schemas` is a list, and both writers appended to it blindly.**
The compiled catalog (`load_from_json`) and the `@register` decorator each added
an entry, so any tool defined in both was sent to the model **twice**:

```
requested names: 56    schemas returned: 110    names duplicated: 54
v3_worker_fundamental  whitelist=4   schemas_sent=8
user_chat              whitelist=29  schemas_sent=56
```

Three of the pairs disagreed about their contract — `get_sec_filings` was
advertised as both `required:["ticker"]` and `required:[]` with a `symbol`
alias. That is the 26.3%-over-14-days failure.

Fixed in lazycat-sdk 0.3.3 (`_put_schema`): one schema per name, **the catalog
wins**, because the catalog was already the one being enforced — `_schema_params`
returns the first match and the catalog loads first. So what the model is SHOWN
now equals what the executor ENFORCES. Verified live:

```
SDK 0.3.3 | total schemas: 56 | duplicated names: 0
user_chat: whitelist=29 schemas_sent=29
get_sec_filings entries: 1 | required: ['ticker']
```

A contract disagreement now logs a warning instead of being silently absorbed.

### …but the stale-artifact mechanism was real, with a different victim

Rebuilding from source *did* change the artifact. The 07-31 `canvas_add_widget`
guidance (commit `573b9b7`: do NOT route a question that merely *contains*
numbers to the converter) had been written into `tool_schemas/` and never built,
so no agent ever saw it. It had been committed and inert since it landed.

The existing cross-repo test compared the three flat copies **to each other**,
which passes happily while all three are equally stale. Added
`test_flat_artifact_matches_the_split_source`, which compares the artifact to a
fresh in-memory build. Falsified both ways: fires on the stale artifact, passes
on the rebuilt one.

## 4. SDK required-field validation — FIXED, but not the way the handoff proposed

The handoff called it a one-line fix: hoist the missing-required check out of
`if _dropped_keys`. Doing exactly that would have **newly blocked working
calls**. 4 of the 40 tools with required fields declare one the Python function
happily defaults, including `whiteboard_read(section='')` — omitting `section`
is a legitimate read of the desk's own scratchpad.

So the gate is `_unbindable_params`: reject only when the call cannot bind to
the signature — i.e. exactly the calls that were going to raise `TypeError`
anyway. The schema's `required` list still applies where it always did,
alongside dropped keys. 12 new tests; 5 fail against the old registry, 4 are
no-regression guards that pass either way.

## 5. Autovacuum — THE PREMISE WAS HALF WRONG

"Autovacuum has never fired on this database" is false. It fires fine on the
smaller tables (technicals 07-29, ontology_nodes 07-29, sec_13f_holdings 07-26),
`autovacuum=on`, `track_counts=on`, `stats_reset=NULL`. It had never once
touched **price_history**, and the reason is arithmetic:

```
price_history  15,163,653 live | 26,885 dead | reloptions NULL
               autovacuum at 20% dead  = 3,032,781 rows
               autoanalyze at 10% mods = 1,516,415 rows
               last_autovacuum = NULL   last_autoanalyze = NULL
```

The defaults are *proportional*, so on a big append-mostly table the trigger
point recedes as fast as the table grows. Fixed in `run_migrations` with
`scale_factor = 0` plus absolute thresholds on the four large tables.

Verified with a positive control rather than by reading the setting back:
`technicals` autovacuumed at **18:30:43** (it had last run 07-29) and went from
186,591 dead tuples to 0, then fired again at 18:42. `price_history` sits at
27,038 dead against its new 100,000 threshold — a bound it can actually reach.

## 6. CORAL runner — THE JOB WAS STALE; QUEUE IS EMPTY

The runner works from a host checkout (`scripts/evo_runner.py --list`). The one
queued job (since 07-29) was `yfinance_collector.py` / "📥 FCF: yfinance_price
returned no data" — the *same defect* item 1 fixed. Running the shipped collector
on that exact ticker after deploying:

```
[yfinance] FCF: dropped 1 internally inconsistent bar(s) (2026-07-31); keeping 124
collect_price_history(FCF) -> 124 rows
```

The bad bar is **today's**. Under the old code that one bar rejects all 125 rows
and produces precisely the queued error. So the job's repro would no longer fail
on HEAD and it is not gradable — closed as `skipped` with that rationale rather
than left looking actionable. Queue is empty.

---

## Found along the way, not on anyone's list

**`sell_stock` advertised a parameter it cannot honour.** The catalog documented
`qty_pct` ("1.0 for 100%, 0.5 for 50%"); `sell_stock(ticker)` closes the entire
position and takes no such argument. Today that call is *rejected* — qty_pct is
a declared catalog property, so it survives the argument filter and raises
TypeError. The reason it was worth fixing immediately: it is one plausible
cleanup away from inverting. Drop `qty_pct` from the filter instead of from the
schema and the same call silently becomes a **full liquidation of a position the
model wanted to halve**. Removed from the schema. Surfaced by the new
catalog/decorator mismatch warning.

**A test that had been red on master.**
`test_whitelists_grant_write_to_pm_and_board_only` asserted the analysts hold
`get_parameters`; both deliberately dropped it on 2026-07-25 (zero calls in 60
days, nothing in the prompt). It was encoding a revoked intent. Now asserts the
drop. Full suite is green: **2749 passed, 13 skipped, 0 failed**.

## Still genuinely open

1. **Nothing schedules `evo_runner.py`.** The queue is empty today, but the next
   enqueued job will sit forever exactly as this one did. Scheduling an
   autonomous loop that proposes patches and pushes `evo/*` branches is a
   judgement call about autonomy, not a bug — it needs an explicit decision.
2. **Two remaining catalog/decorator contract mismatches**, now logged at boot:
   `get_sec_filings` (decorator's `symbol` alias) and `save_trading_chart`
   (decorator declares 4 params the catalog omits — they were already being
   stripped before reaching the function, so the model was being told about
   arguments that could never arrive). Both are cosmetic next to `sell_stock`;
   fix by editing `tool_schemas/`, not the Python.
3. The measurement-integrity findings from the previous handoff stand unchanged:
   the desk's edge is still below the measurement floor, and confidence is still
   overstated ~16 points.

## Verify any of this

```bash
scripts/cycle_audit.py --check
scripts/evo_runner.py --list                     # expect: queue is empty
pytest tests/test_multi_repo_audit.py            # catalog vs its source
ssh nas "sudo docker exec trading-service python -c \"
import app.tools, collections, lazycat
from app.tools.registry import registry
c=collections.Counter(s['function']['name'] for s in registry.schemas)
print(lazycat.__version__, len(registry.schemas), sum(1 for v in c.values() if v>1))\""
# expect: 0.3.3 56 0
```
