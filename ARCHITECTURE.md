# trading-service Architecture

> Last updated: 2026-06-25

## Overview

`trading-service` is the **agentic orchestration core** of the trading platform.
It owns the V3 pipeline — the cycle that collects market data, runs multi-agent
analysis debates, and executes paper trades. It is the brain; everything else
is infrastructure.

## What This Service Owns

| Domain | Directory | Description |
|--------|-----------|-------------|
| **V3 Pipeline** | `app/v3/` | Linear agentic pipeline — orchestrator, agent runner, shared desk, guardrails, telemetry |
| **Agents** | `app/agents/` | Base agent, agent loop, tool whitelists, specialized agents (janitor, planner, verifier, etc.) |
| **Cognition** | `app/cognition/` | Memory, ontology, evolution, debate, evidence, contracts — trading intelligence stack |
| **Cycle (V2)** | `app/cycle/` | Legacy V2 orchestrator — phases, state management, lifecycle control |
| **Autoresearch** | `app/autoresearch/` | Post-cycle evaluation — auditors, eval engine, eval worker |
| **Monitoring** | `app/monitoring/` | LLM tracker, dashboard, metrics collector, pipeline profiler, context telemetry |
| **Recovery** | `app/recovery/` | Failure types, degraded retry, recovery engine, registry |
| **Worker** | `app/worker/` | Background worker configuration |
| **Trading** | `app/trading/` | Paper trader, portfolio, watchlist, order triggers, risk manager, strategy tracker |
| **Pipeline** | `app/pipeline/` | Trading constitution, analysis engine, context builder, debate engine |
| **Services** | `app/services/` | Prism client, vLLM router, boot service, pipeline service, cycle scheduler |
| **Routers** | `app/routers/` | FastAPI HTTP endpoints — chat, diagnostics, agent tools, verdicts |
| **Config** | `app/config/` | Settings, model configs, ticker configs, context budgets |
| **Database** | `app/db/` | Connection pooling, migrations |
| **Tools** | `app/tools/` | Tool implementations that agents call (also exist in lazy-tool-service) |

## What This Service Does NOT Own

| Category | Owner | Communication |
|----------|-------|---------------|
| **Tool Execution** | `lazy-tool-service` | MCP SSE — agents call tools via the tool service |
| **LLM Inference** | `prism-service` | HTTP — `/agent` endpoint (never `/chat`) |
| **Frontend UI** | `trading-client` | HTTP — REST API calls to this service |
| **Vault / Secrets** | `vault-service` | HTTP — env vars fetched at deploy time |

## Key Entrypoints

### `cycle_main.py` (root)

The Docker entrypoint. Boots three concurrent tasks:
1. **FastAPI health server** (port 8080) — `/health`, `/status`, plus routers for vLLM, chat, diagnostics, etc.
2. **System commands poller** — polls `v3_system_commands` table for `START_CYCLE`, `STOP_CYCLE`, `FORCE_RESET`, etc.
3. **Worker** — runs `BootService.startup()` then waits for shutdown

### `app/v3/orchestrator.py`

The V3 pipeline orchestrator. Receives a list of tickers, runs them through the linear pipeline:
1. **Data Collection** — market data, news, filings
2. **Multi-Agent Analysis** — swarm debate with specialized agents
3. **Trading Decisions** — action gating, risk checks, order execution
4. **Post-Cycle** — autoresearch, memory consolidation, lesson extraction

## Communication Flow

```
  ┌─────────────────┐
  │  trading-client  │
  │    (React UI)    │
  └────────┬─────────┘
           │ HTTP (REST)
  ┌────────▼─────────┐     HTTP (/agent)     ┌──────────────┐
  │  trading-service  │◄───────────────────►│ prism-service │
  │  (this service)   │                      │   (LLM API)   │
  └────────┬─────────┘                      └──────────────┘
           │ MCP SSE
  ┌────────▼──────────┐
  │ lazy-tool-service  │
  │   (tool runner)    │
  └────────────────────┘
```

## Rules for Future Development

1. **All agent orchestration goes here** — agent loops, harnesses, whitelists, budgets
2. **All cycle management goes here** — pipeline state, start/stop, checkpoints
3. **All cognition goes here** — memory, ontology, evolution, debate, evidence
4. **Use `/agent` not `/chat`** when calling prism-service
5. **Use `v3_system_commands`** not `system_commands` for pipeline commands
6. **Shared code extracts to `lazycat-sdk`** once built (Phase 8)
