# Trading Domain Evidence, Lineage, & Telemetry (2026-09-17)

## Phase 0: Trading Service Telemetry & Memory Inventory

| Path / System | Owner | Writer | Reader | Storage Location | Scope | Prompt Injection & Budget | Provenance Fields | Freshness / Expiry | Test & Status |
|---|---|---|---|---|---|---|---|---|---|
| **Trading Lineage Tracker** | `trading-service` | `TradingLineageTracker` | Telemetry exporter | Memory / NAS `telemetry-service` | `cycle`, `ticker`, `order` | N/A (Telemetry/Audit) | `trace_id`, `cycle_id`, `phase`, `timestamp` | Exported per cycle; buffered in fail-safe queue | `test_trading_adapter.py` (Active) |
| **Candidate Trading Observation** | `trading-service` | `CandidateTradingObservation` | Verification loop | SQLite / In-memory | `ticker`, `cycle` | Quarantined from prompt until shadow verified | `source_ref`, `observed_at`, `validation_result` | Must pass shadow run before promotion to ACTIVE | `test_trading_adapter.py` (Active) |
| **Market Snapshot / Cache** | `trading-service` | Market scanner | PM Gatekeeper / Agents | MongoDB `market_snapshots` | `cycle`, `portfolio` | Agent prompt context (Budget: 3,000 tokens) | Quote timestamps, exchange feed SHA | Freshness: 60s max stale threshold | Pipeline sync rules |

## Phase 5: Complete Financial Lineage Trace
Every execution strictly records and tracks the complete boundary chain:
```text
cycle
  → market snapshot
  → research / model call
  → decision
  → policy verification
  → execution intent (WAL)
  → reservation / slot allocation
  → order execution / fill
  → reconciliation
  → outcome recording
```

- **Fail-Safe Telemetry Guard**: All NAS telemetry export calls run in non-blocking background tasks with hard 2.5s timeouts and silent fallbacks. Network partition or collector restarts never block protective stops, trade execution, or pipeline loops.
