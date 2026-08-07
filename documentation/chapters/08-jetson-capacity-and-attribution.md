# 08 — What the Jetson has left, and the defect that turned up looking for it

*2026-08-07. Follows `07-jetson-role.md`, which gave the box its first job.*

The question was "how do we give the Jetson more work, or is it maxed out?"
The answer is that it is at about 45% of its ceiling, that the ceiling is
lower than the hardware suggests, and — the part that matters more — that its
current job is a burn-down which runs out in about a day. Looking for the next
job surfaced a live defect in the trade-enabled wake path, which is what most
of this chapter is about.

## 1. The ceiling, measured under the real workload

Load was added **alongside the running backfill**, and total box throughput
read from the Jetson's own `/metrics`. That design is the point: the question
is not "how fast is this box" but "can it absorb another job on top of this
one".

| extra streams | total req/hr | prefill tok/s | added-stream p50 | peak running | peak waiting |
|---|---|---|---|---|---|
| 0 (backfill only) | 2,554 | 406 | — | 3 | 0 |
| +3 | 4,146 | 751 | 3.0s | 6 | 0 |
| +6 | 5,325 | **911** | 4.9s | 8 | 1 |
| +12 | 5,155 | 904 | 9.7s | 8 | **7** |

**The knee is 8 concurrent sequences.** `num_requests_running` pins at exactly
8 at both +6 and +12 while prefill throughput flatlines (911 → 904) and the
queue goes 1 → 7 and p50 doubles. Past 8, work only queues.

**It is compute-bound, not memory-bound, so there is no config win available.**
`kv_cache_usage_perc` peaked at 12.7%, `num_preemptions_total` is 0, the
engine's own `kv_cache_max_concurrency` is 10.75, and `gpu_memory_utilization`
is 0.7. Raising `max_num_seqs` or handing vLLM more GPU memory would not move a
number that is already flat at the cap.

**The workload is prefill-dominated 6:1** — 364 prompt tok/s against 59
generation tok/s in steady state, ~1,350 prompt / ~136 generated tokens per
extraction. Judge any candidate job by its *prompt* size, not its output.

### What changed as a result

`NEWS_BACKFILL_CONCURRENCY` 3 → 6 and `NEWS_BACKFILL_BATCH_SLEEP_S` 20 → 10.
Six is the largest setting measured with zero queueing, deliberately leaving
two slots: the in-cycle extractor and the gatekeeper shadow also land on this
box, and a queued shadow call times out into an `AGENT_ERROR` indistinguishable
from the model failing. The sleep mattered because the old duty cycle was 36%
idle — visible in a sampled `num_requests_running` trace as
`0000233332323333333233332331312311110000`, one batch of 24 then the pause.

Shortening the pause does not weaken the cycle stand-down: `_cycle_is_running`
is checked once per batch, so what bounds an overrun is the in-flight batch,
never the pause. Measured — the cycle began at 13:32:01.5, the last 15
straggler extractions landed by 13:32:38, then nothing for 99 minutes.

### The real constraint is that the job ends

31,577 articles remained at the time of writing, ~32h at the old rate. Standing
inflow is only ~1,000 eligible articles/day — about **42/hr against a
~5,300/hr ceiling, under 1%**. The Jetson returns to idle roughly a day and a
half after the backlog clears. "More jobs" is a question about that Sunday, not
about contention today.

**Confirmed after deploy: 2,067 rows/hr** over a 12-minute window, against a
clean concurrency-3 baseline of ~1,320/hr mean (best hour 1,457). That is +57%,
and ~15h to drain rather than ~32h.

Worth recording because it nearly caused a wrong revert: **batch wall time did
not move** (39.5s / 46.7s / 33.2s / 30.0s at concurrency 6, against ~35s at
concurrency 3), which read as "the change did nothing". It was a confound —
prompt tokens per request rose from ~840 to ~1,367 in the same window, because
the backlog drains newest-first and article length varies. Batch duration over
a variable-size work unit is not a throughput measurement; rows/hour is.

## 2. The defect: a discriminator with five writers, two of which wrote

`news_articles.ticker_attribution` gates `watch_desk._recent_news`, which
decides which rows may trip a wake — and **a wake is a trade-enabled cycle**:

```sql
AND (ticker_attribution IS NULL OR ticker_attribution != 'query_fallback')
```

NULL is admitted. The docstring justified that as a transition cost: *"NULL
attribution (legacy) ... dropping them would blind every watch on pre-migration
rows for 48h."*

**That window never closed.** The collector began writing the column
2026-08-03 07:45. Four days later:

| bucket | rows | NULL | % NULL |
|---|---|---|---|
| last 48h | 4,633 | 3,447 | **74.4%** |
| 2–7 days | 7,157 | 5,582 | 78.0% |
| older | 51,547 | 51,547 | 100% |

Cause: five `INSERT INTO news_articles` paths, three of which never wrote the
column — `news_api_rotator.py` ×2 and the RSS writer in `news_collector.py`.
The clause was not tolerating a shrinking set of legacy rows. It was admitting
three quarters of *current* ones, unscreened.

This is [[a-discriminator-with-one-source-fails-open]] — an absent row read as
permission — and [[a-shared-helper-is-not-a-shared-fix]].

### Why the existing tests did not catch it

`tests/unit/test_news_ticker_attribution.py` had passed continuously since the
ghost-wake fix. It only ever exercised `collect_finnhub_news`, so it proved the
concept on the one writer that already worked and was structurally blind to the
other three. The replacement is a **scan**, not another per-writer case:
`test_every_news_insert_writes_ticker_attribution` walks every
`INSERT INTO news_articles` in `app/` and fails if any omits the column, so a
sixth writer cannot reopen the hole quietly. It was confirmed to fail on the
pre-fix tree naming exactly those three sites, and to pass after — a check that
passed in both states would have proved nothing.

### The vocabulary is now four values, and one is an open question

`detected` (we found the symbol in the text), `query_fallback` (refused by the
wake filter), `general` (ticker IS NULL — cannot match a `ticker = %s` read
either way), and **`provider`**.

`provider` is new and deliberate. The rotator has two ticker provenances: the
vendor's own entity tagging (`article.tickers`) and our own
`_detect_tickers_in_text`. Collapsing both into `detected` would put an
unverified vendor claim behind the mark the watch desk trusts to arm a trade.
`provider` rows currently **pass** the filter, so behaviour is unchanged from
when they were silently NULL — the label only makes the trust level visible.
Whether a vendor claim should arm a wake is now answerable and **not yet
answered**; measure wake precision by `ticker_attribution` before tightening,
because excluding it blind could blind the desk more than it protects it.

NULL now means "collected before 2026-08-07" and nothing else, so the clause
self-closes once that date falls out of the lookback.

## 3. The classifier: measured, and not adopted

`scripts/news_attribution_ab.py`, with `decide()` written before the first run.

**The counterfactual, stated first.** An idle GPU is not a reason to run a job.
Legacy rows feed ticker-keyed reads — news-mention counts that drive discovery,
`flash_briefing`, `discovery_mode`, `freshness_gate`, and the extraction
backfill itself. Nothing labels them today, so the alternative is not a cheaper
labeller; it is the status quo, in which every misfiled row counts as evidence.

**What it had to beat was not nothing.** `_is_article_relevant_to_ticker` is
free and already deployed — and weak in a specific way: it returns `True`
unconditionally for any ticker of 4+ characters.

**The oracle was frozen before the box ran** — 60 rows sampled, hand-labelled
from title and lead, 3 left `null` as genuinely ambiguous, committed to
`tests/fixtures/attribution_oracle.json`. (Not `scripts/data/`: `.gitignore`
carries a blanket `data/` rule, and a ground truth that does not survive a
clone is not frozen.)

| n=57 | keep-recall | reject-precision | balanced accuracy |
|---|---|---|---|
| jetson | 90.9% | 97.7% | **93.2%** |
| free heuristic | 100.0% | 100.0% | 65.9% |

```
[PASS] reject precision >= 90%          97.7%  (Wilson LB 87.9%, n=43)
[FAIL] keep recall >= 95%               90.9%  (Wilson LB 62.3%)
[PASS] balanced acc >= free + 10pp      93.2% vs 65.9%
[PASS] p95 < 25s                        2.8s
=> DO NOT ADOPT
```

**The threshold was not moved.** Same discipline as the extraction A/B, which
lost on 77% against an 80% bar.

**The corpus finding is bigger than the verdict: 44 of 57 labelled rows — 77% —
are misfiled.** The free heuristic keeps 30 of those 44. Two thirds of a
heavily misfiled corpus flows into every ticker-keyed read today.

**The single failure is diagnosable, which is not permission to retune.** The
one false reject was *"NVIDIA vs. Apple: Which Tech Titan Is the Better Buy
Right Now?"* under AAPL — a two-company comparison, where the prompt's "a
subject of the story, not merely mentioned in passing" reads as too strict. The
two false keeps were both analyst-call roundups. Fixing the prompt and
re-scoring against **this** oracle would be fitting to the test set now that
the labels are known. A prompt change needs a freshly sampled, freshly
labelled oracle — and many more than 13 positives, since the keep-recall
denominator here was 11 usable, which is exactly why its lower bound is 62.3%
and why this run settles the gate in neither direction.

## 4. What is true now

- Jetson knee: 8 concurrent, ~910 prefill tok/s, ~5,300 req/hr. Backfill at 6.
- All five news writers stamp `ticker_attribution`; a scan test holds the line.
- The wake filter's fail-open is now bounded and self-closing. Behaviour
  unchanged for `provider` rows, pending measurement.
- The classifier is **not** in production. Nothing routes to it.
- The Jetson's decision-path role is still gated on the gatekeeper shadow,
  which stands at n=1 usable of 10 (`05-jetson-plan.md`).

## 5. Open

1. **`provider` rows and the wake.** Measure wake precision by
   `ticker_attribution` before deciding whether a vendor claim may arm a trade.
2. **A larger attribution oracle.** ~150 rows with ≥40 positives would let the
   keep-recall gate actually decide. Only then is a prompt revision meaningful.
3. **Attribution at collection time for the misfiled 77%** — open item #9 in
   `03-open-items.md`. The classifier addresses the 51,547 legacy rows; it does
   not fix the filing that created them.
4. **What the Jetson does after the burn-down**, which is now roughly 16 hours
   away.
