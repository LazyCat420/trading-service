# The Jetson's job

Written 2026-08-07. Supersedes the open question in `05-jetson-plan.md`: *what
should the Jetson actually do?* It now does something. This chapter records the
measurement that decided it, including the part where the pre-registered rule
said **no** to the obvious answer.

## What was wrong before any of this

The Jetson was already listed as the failover host for news fact-extraction. It
could never have worked.

Extraction sends a ~530-token prompt with `max_tokens: 2048`. With reasoning
left on, Qwen3.6 spends the **entire** completion budget thinking and returns
`finish_reason="length"` with **empty content** — 42.6 seconds, every call.
`extract_article_facts` reads an unparseable answer as *try the next host*, so
this presented as a slow, flaky box rather than as a malformed request. Nobody
noticed because Gold Spark was first in the list and always answered.

| | reasoning on | reasoning off |
|---|---|---|
| Jetson | 42.6s, **0 chars**, truncated | **4.3s**, valid JSON |
| Gold Spark | 12.2s, valid | **5.9s**, valid |

`build_payload()` now sets `chat_template_kwargs: {"enable_thinking": false}`
for both boxes. Extraction is transcription, not reasoning; the thinking tokens
were discarded on Gold Spark too, which is why its latency halves as well.

This is the second time this box has been condemned for a defect in how it was
being *asked* — the first was prism's injected `minP=0.05`. Diagnose from the
request.

## The measurement

`scripts/news_extraction_ab.py` replays real articles through both boxes
concurrently, alternating which box leads, and grades with the **production**
grader — every extracted fact must carry a quote that aligns back to a character
offset in the source, or it is dropped. The bench imports `build_payload` and
`align_quote` from the service rather than copying them, so it cannot drift into
measuring a configuration nobody ships.

Decision rule, written into `decide()` before the first run: promote the Jetson
only if valid-JSON rate ≥ primary − 5pp, drop rate ≤ primary + 5pp, grounded
yield ≥ 80% of primary, and p95 under the 25s call timeout.

**Run 1 — 40 articles, as collected:**

| | Gold Spark | Jetson |
|---|---|---|
| valid JSON | 98% | **100%** |
| facts/article | **2.77** | 1.85 |
| ungrounded drop rate | **3.5%** | 6.3% |
| p50 / p95 latency | 7.7s / 21.1s | **3.2s / 11.3s** |
| span overlap | 61% | |

Yield failed (67% of primary). But the gap concentrated on four articles where
Gold Spark found 3–6 facts and the Jetson returned `{"facts": []}` — and reading
them, they are a CNBC macro roundup filed under `CME` and a Genesis Healthcare
bankruptcy filed under `GEN`. On a mis-attributed article, extracting **fewer**
facts is the better answer, and a raw fact count scores it as the worse one.

**Run 2 — 40 articles whose body actually names their own ticker** (a mechanical
symbol match, not a judge model — removing the confound must not smuggle in a
second model's opinion as ground truth):

| | Gold Spark | Jetson |
|---|---|---|
| valid JSON | 100% | 100% |
| facts/article | **3.90** | 3.00 |
| ungrounded drop rate | **3.1%** | 4.8% |
| p50 / p95 latency | 10.2s / 17.1s | **6.5s / 12.5s** |
| span overlap | 74% | |

The confound explained part of the gap (67% → 77%) and not all of it. **77% is
still under the 80% bar. The rule says no, twice, and the rule stands** — Gold
Spark keeps the in-cycle extraction job. The threshold was not moved after the
fact, which is the only thing that makes the number worth anything.

## The job the Jetson actually got

The rule answers *which box extracts an article an agent is about to read*. It
does not answer *what to do with the 42,715 articles nobody has asked for*.

Measured 2026-08-07: **42,715 of 44,868 eligible articles (95.2%) had never been
extracted.** Only 2,153 ever had. The in-cycle extractor is bounded by a 22-second
per-cycle budget that clears a handful at a time and is outrun by ~1,000 newly
collected articles a day. Every one of those articles reaches an agent as raw
scrape — ~2,300 characters typically opening with site navigation.

For that backlog the counterfactual is not Gold Spark. It is **nothing**:

| | in-cycle | backfill |
|---|---|---|
| not extracting means | Gold Spark extracts it | the agent reads raw scrape |
| the comparison is | Jetson vs Gold Spark | Jetson vs nothing |
| verdict | Gold Spark, on the rule | Jetson |

`app/services/news_backfill.py` runs as a background loop from boot, pulling
newest-first batches of 24 at concurrency 3 (~28 articles/minute against a
measured 6.5s median — comfortably ahead of the daily inflow). It is the first
production work this box has ever done.

**It stands down while a cycle runs**, and not for CPU reasons — the cycle
extracts on Gold Spark, so the two never compete for a box. It is to protect a
measurement. `MODEL_SHADOW_AGENTS` sends one gatekeeper prompt per cycle to this
same Jetson, and that comparison is still accruing toward n≥10. A box kept
permanently busy queues those calls into timeouts, and a timeout is recorded as
`AGENT_ERROR` — indistinguishable afterwards from the model having failed, which
is exactly how the first gatekeeper shadow row was already lost. The backlog has
infinite patience; the shadow evidence does not.

**The pin is hard, and that is the design.** If the Jetson is unreachable the
worker backs off and stops. It must never fail over onto Gold Spark: silently
converting "the spare box is idle" into "the trading cycle's box is serving
42,000 low-priority extractions" is the exact failure this exists to prevent.
`_chat_targets(only=...)` removes hosts rather than demoting them, and an empty
result is the correct answer.

## Verifying it is actually running

The stored note used to be the constant `"vllm"` for every row, which made the
host preference unverifiable: no query could distinguish *the Jetson is doing
this job* from *the Jetson is listed first and failing every call*. It now
records the box key, deliberately **not** prism's provider label — the legacy
constant was itself `"vllm"`, which is the Jetson's provider label, so returning
the provider would have made 2,153 rows written by the old constant look
identical to rows the Jetson produced, and the verification query would have
confirmed the change before it shipped.

```sql
SELECT grounded_facts->>'model' AS box, count(*)
FROM news_articles WHERE facts_extracted_at IS NOT NULL GROUP BY 1;
-- "vllm" = legacy rows, written before 2026-08-07 by the hardcoded constant
-- "jetson" / "dgx_spark" = attributed rows
```

Backlog trend is the other signal: `news_backfill.backlog_size()` is logged at
worker start and every batch.

## What this does not settle

- **The gatekeeper shadow is still the blocking measurement for a decision-path
  role**, and still at n=1 of 10 (`v3_regime_engine` has 25 SUCCESS rows;
  `v3_portfolio_manager` has one `AGENT_ERROR`). Backfill is a data-quality job
  and deliberately does not touch a trading decision. Nothing here shortens that
  wait.
- **Yield is a proxy, not a quality metric.** Run 1 showed it scoring the wrong
  box higher on mis-attributed articles. It bounds "is the box lazy"; it does
  not measure whether the facts are the *right* facts. A better metric would
  need labelled articles, which do not exist.
- **The ticker mis-attribution is a real upstream defect** surfaced by this
  work, not fixed by it — see `03-open-items.md`.
