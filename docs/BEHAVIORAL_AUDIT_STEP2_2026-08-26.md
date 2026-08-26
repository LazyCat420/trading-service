# Behavioral audit step 2 — the model contract batch (2026-08-26)

Service-side record. The narrative chapter is trading-client
`documentation/chapters/100-the-box-that-answered-as-someone-else-2026-08-26.md`
(served at http://10.0.0.16:8888/documentation); this file records what
changed in THIS repo and how to verify it.

## What was diagnosed

45 of 74 production desks since Phase A (2026-08-23) died as
`board_degraded_fallback` before the Board ran — no trade_results row, no
page. Cause: dgx_spark intermittently serves `cyankiwi/Qwen3.6-…` (prism's
memory jobs hold the box) and `resolve_default_model_for_agent` trusts
whatever the box answers; Qwen replied to the regime prompt with a 10-token
contract-failing artifact and all 5 retries burned 75 s per desk. The 08-25
LLM pre-flight passed because the endpoint was alive.

## What changed here

- `app/config/config.py` — `DECISION_MODEL_PATTERN` (default `deepseek`).
  Update it when the decision model is deliberately switched.
- `app/services/prism_agent_caller.py` — `ModelContractError`; the resolver
  refuses a non-matching dgx_spark model before any prompt is sent (Jetson
  exempt).
- `app/services/llm_preflight.py` — contract violation = positive evidence →
  cycle aborts; other resolver errors still fail open.
- `app/services/pipeline_service.py` + `app/services/degraded_alert.py` —
  pre-flight aborts page (`llm_preflight_abort`); ≥50% DEGRADED analyses in
  24 h page (`llm_degraded_partial`) even when healthy cycles interleave.
- `app/quant/decision_score_store.py` — rows stamp `id` + `created_at` (the
  PG default died at the cutover; 130/442 rows were invisible to
  date-windowed reads). `scripts/backfill_decision_scores_created_at.py`
  derives the missing 130 from the cycle id epoch — **run `--apply` after
  deploy**.
- `app/utils/batch_screener.py` — empty-frame path returns the declared
  2-tuple (ch.98 A2a; a bare string made callers raise and the cycle ran
  AAPL).
- `scripts/grounding_decay_report.py` — first reader of `_grounding_shadow`
  (per-hop grounded rate; live: bull 96.4 → bear 81.0 → defense 76.2 →
  judge 86.0; bear+defense below the 85.6% floor).
- `scripts/measurement_window_report.py` — ch.91 go/no-go counter + desk
  mortality (live: 26/100 clean decisions, NO-GO).

## Verify

- `python3 -m pytest tests/unit/test_model_contract.py tests/unit/test_decision_score_created_at.py`
  (14 tests; 10 proven red on `c944e6e`).
- After deploy: `v3_agent_telemetry` should show zero `v3_regime_engine`
  AGENT_ERROR rows on new cycles; a Qwen window should now show an aborted
  cycle + one `llm_preflight_abort` fund_alert instead of dead desks.
- `python3 scripts/measurement_window_report.py` — degraded count should
  stop growing.
