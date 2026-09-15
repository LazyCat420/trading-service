# Data Collection, Phase Latency & AutoResearch Hardening (2026-09-14)

## Summary
Audited data collection, data report processing, phase timing, and AutoResearch post-cycle evaluation. Resolved the fast-path collector bypass and delivery receipt ambiguity.

## 1. Issues Identified
1. **48-Hour Fast-Path Collector Bypass**:
   - `build_ticker_data_report()` previously defaulted to `is_fast_path = True` whenever a ticker was analyzed within 48 hours, skipping 4 out of 6 collectors (`yfinance_fund`, `multi_api_news`, `reddit`, `youtube`).
   - Neither `run_v3_pipeline` nor `PipelineService` passed any parameter to override this cache, meaning that explicit data collection runs (`"collect": True`) were silently degraded to cached research.
2. **Phase Timing & Attribution**:
   - In `cycle_benchmarks`, `collect_ms` tracked only global discovery sweeps. Explicit ticker requests bypassed discovery, leaving `collect_ms` recorded as `0` while per-ticker precollection was lumped entirely into `analyze_ms`.
3. **AutoResearch Reflection Receipt False-Alarms**:
   - When no research questions were claimed for a ticker, `research_answers_delivered` was recorded as `false`. AutoResearch Reflection interpreted this as a failure of research propagation.
   - Upstream analysts do not output trade decisions, so `contract_delivered` is intentionally false for them, but was flagged by reflection.

## 2. Changes Made
1. **`app/v3/data_report.py`**:
   - Added `force_refresh: bool = False` to `build_ticker_data_report()`. When `True`, bypasses `is_fast_path = True`, forcing full collection across all collectors even if a recent thesis exists in `analysis_results`.
   - Records `prefix_label = "PREVIOUS ANALYSIS ON FILE (FORCE REFRESH)"` so prior research is still provided as reference while collecting fresh data.
2. **`app/v3/orchestrator.py`**:
   - Added `force_refresh` argument to `run_v3_pipeline()`.
   - Explicitly measures `precollect_ms` around `build_ticker_data_report()` and records it into `desk.cycle_metadata["precollect_ms"]`.
3. **`app/services/pipeline_service.py`**:
   - Passes `force_refresh=bool(kwargs.get("force_refresh") or kwargs.get("collect", False))` to `run_v3_pipeline()`.
4. **`app/v3/agent_runner.py`**:
   - Sets `"research_answers_delivered": ... if has_questions else "not_applicable"` in delivery receipts so un-asked questions are not recorded as delivery failures.
5. **`app/autoresearch/reflection.py`**:
   - Added prompt note clarifying that `contract_delivered` is strictly required only for Decision and Board layers, and `research_answers_delivered='not_applicable'` indicates an empty ledger.
6. **`tests/unit/test_data_report_collection.py`**:
   - Added unit test suite covering `force_refresh=True`, fast-path fallback, and delivery receipt logic.

## 3. Verification
- `tests/unit/test_data_report_collection.py`: 3/3 passed.
- `tests/unit/test_precollect_outcomes.py`: 9/9 passed.
- `tests/unit/test_whiteboard_ablation.py`: 3/3 passed.
