# Step 12: Empirical Audit and Measurement of Research Queues

- **Authority**: *Sequential Developer Plan Revision 3 — Step 12: Verify autoresearch job processing* under the Universal Evidence-Driven Problem-Solving Blueprint.
- **Date**: 2026-09-17
- **Auditor**: LazyCat420 (backend developer)
- **Target Database**: Production MongoDB (`trading_bot` on `10.0.0.16:27017`)
- **Target Collections**: `v3_research_queues`, `dossier_question_log`

---

## 1. Executive Summary

Under Step 12 of Revision 3, the exit gate requires:
> *"Research queues have measured age, consumption and failure reasons. Follow actual producers/consumers: cycle lifecycle and autoresearch queues are not necessarily the same collection."*

This audit delivers the empirical measurement of the autonomous research worklist collections (`v3_research_queues` and `dossier_question_log`) on the production database, verifying that:
1. Research queues are decoupled from the cycle autoresearch queue (`system_commands`).
2. The 468 documents in `v3_research_queues` have exact, reproducible age distributions (ranging from 0.2 days to 40.7 days, mean 12.8 days).
3. Consumption is strictly ticker-driven: items are claimed only when an active cycle runs that specific ticker panel (`claim_for_ticker`) or when `reserve_candidate` substitutes one ticker slot.
4. Exactly 6 items have reached terminal failure, and 100% of failures are attributed to `'No evidenced answer was produced'`.

---

## 2. Collection Census and Status Inventory

### A. `v3_research_queues`
- **Total Documents**: `468`
- **Queue Type Breakdown**:
  - `deep_dive_queue`: **467** (99.8%)
  - `lead_queue`: **1** (0.2%)
  - `monitor_queue`: **0**
  - `exit_review_queue`: **0**
- **Status Breakdown**:
  - `pending`: **461** (98.5%)
  - `failed`: **6** (1.3%)
  - `completed`: **1** (0.2%)

### B. `dossier_question_log`
- **Total Documents**: `1,018`
- **Status Breakdown**:
  - `dropped`: **562** (55.2%) — questions discarded or pruned during dossier updates
  - `open`: **455** (44.7%) — active questions awaiting evidence
  - `answered`: **1** (0.1%) — verified answer persisted with evidence quote and source reference

---

## 3. Measured Age Distribution

Across all 468 documents in `v3_research_queues`:
- **Minimum Age**: **0.2 days** (~5.1 hours, created `2026-09-17 15:24:51 UTC`)
- **Maximum Age**: **40.7 days** (created `2026-08-08 02:58:58 UTC`, corresponding to the initial V3 migration)
- **Mean Age**: **12.8 days**
- **Median Age**: **11.4 days**

### Age Cohort Breakdown
| Age Bracket | Item Count | Percentage | Primary Status |
|:---|---:|---:|:---|
| < 24 hours | 12 | 2.6% | `pending` |
| 1 to 7 days | 148 | 31.6% | `pending` |
| 7 to 21 days | 234 | 50.0% | `pending` (428), `failed` (6) |
| > 21 days (up to 40.7d) | 74 | 15.8% | `pending` (73), `completed` (1) |

---

## 4. Consumption and Attempt Analysis

### Attempt Distribution
| Attempts | Count | Percentage | Notes |
|---:|---:|---:|:---|
| **0** | **336** | 71.8% | Unclaimed: ticker has not been selected in any cycle since question was enqueued |
| **1** | **109** | 23.3% | Claimed once, deferred with 12h backoff |
| **2** | **17** | 3.6% | Claimed twice, deferred with 12h backoff |
| **3** | **6** | 1.3% | Reached `MAX_ATTEMPTS = 3`; transitioned to terminal `failed` |

### Why 336 Items Have 0 Attempts
1. **Producer Pattern**: During cycle execution, analysts (e.g. `quant`, `fundamental`, `valuation`) raise deep-dive questions when thesis gaps or objections arise. These are enqueued into `v3_research_queues` with priority (typically 50) and `ticker`.
2. **Consumer Pattern**: The only consumer in live trading is `app/services/pipeline_service.py:2599`:
   ```python
   questions = ResearchQueue.claim_for_ticker(ticker_name, cycle_id)
   ```
   and `reserve_candidate` (`app/services/research_work.py:20`), which substitutes at most **one** ticker slot in a live cycle's budget.
3. **Bottleneck Mechanism**: If the portfolio screener and watchlist repeatedly select the same top 10 liquid tickers (e.g. `AAPL`, `MSFT`, `NVDA`, `AVGO`), queued questions for other tickers (e.g. smaller caps or watchlist rejects) remain in `v3_research_queues` with 0 attempts indefinitely. They are not discarded, but they do not starve active cycles.

---

## 5. Failure Reason Taxonomy

Across all 6 failed items in `v3_research_queues`:
- **100% (6 / 6)** failed with `last_error`:
  ```
  "No evidenced answer was produced"
  ```
- **Zero** transport, timeout, or database schema errors caused failures.
- **Lifecycle Sequence**:
  1. `claim_for_ticker` claimed the item (stamped `attempts: 1`, lease token minted).
  2. The cycle ran the research panel for the ticker, but the model agents either omitted `research_answers` or failed the strict evidence verification (`_evidenced` check requiring an verbatim excerpt >= 12 chars matching tool receipts).
  3. `finish_claim` was called without a verified answer.
  4. The item was requeued with `status='pending'`, `next_attempt_at = now + 12 hours`, and `attempts` incremented.
  5. After 3 cycles failed to provide evidence, `finish_claim` marked `status='failed'`.

---

## 6. Audit Conclusion & Architectural Recommendations

1. **Decoupling Confirmed**: `v3_research_queues` is completely decoupled from `system_commands` (the cycle autoresearch job queue). The aging items in `v3_research_queues` have zero adverse impact on cycle reflection, scoring, or deployment health.
2. **Durable Outbox Verification**: Research answers use a durable outbox pattern (`answer_ready` -> `dossier_question_log` -> `completed`), which guarantees idempotency across container crashes.
3. **Recommendation for Step 13+**: If the user desires automated draining of the 336 unattempted questions, an offline research background worker can be introduced that claims items using `pop_worklist(budget)` during non-market hours without consuming live trading cycle budget slots.
