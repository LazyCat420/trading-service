# V3 Tool Surface Audit — whitelists vs. Prism force-adds vs. actual calls

**Scope:** read-only. `agent_tool_telemetry`, `trading_bot` DB, window = last 14
days by `created_at`: **2026-08-23 13:28:51.94 → 2026-09-06 13:28:51.94 UTC**
(3,855 rows of 20,852 total). No files edited, no writes, no deploys.

**Sources reconciled**
- `app/agents/tool_whitelists.py` (`AGENT_TOOL_WHITELISTS`, merged at import
  time with each `app/v3/agents/*.py::TOOL_WHITELIST`) — the *intended* grant.
- Prism's force-add: not code in this repo (Rod's), but its effect is fully
  visible in telemetry and is partially documented in
  `app/v3/prism_registration.py` (`_V3_DENIED_TOOLS`, `_V3_TOOL_POLICIES`) and
  `app/v3/tool_telemetry.py` (`_META_TOOLS`, `_FORBIDDEN`).
- `agent_tool_telemetry` — what actually ran, per call, with `elapsed_ms` /
  `success` / `error_message`.
- `app/services/mcp_prefix.py` — the prefix-stripping seam telemetry names and
  the whitelist must agree through.
- `app/agents/tool_whitelists.py::get_agent_tools` — where schemas are
  assembled per agent; `app/services/context_gate.py::estimate_tokens` /
  `measure_tools` is the codebase's own token-cost estimator, reused here
  unmodified (tiktoken `o200k_base` if installed, else 4-chars/token — this
  dev venv lacks tiktoken even though `requirements.txt` pins `tiktoken==0.13.0`,
  so the numbers below are the 4-chars/token fallback; production is presumably
  more accurate but same order of magnitude).
- `tool_schemas.json` (91 rows ecosystem-wide, 56 scoped to `owner_app in
  {None, "trading"}` via `app/tools/registry.py::_load_scoped_schemas`) — the
  catalog whitelists are checked against.

**Agent universe:** `AGENT_TOOL_WHITELISTS` (post-merge) has **20** keys:
13 are the Prism-registered V3 pipeline agents living in `app/v3/agents/*.py`
(discovered by `app.v3.prism_registration._discover_v3_agent_modules`, the
ones subject to Prism's force-add and the `_V3_DENIED_TOOLS` policy); the
other 7 (`user_chat`, `v3_worker_quant`, `v3_worker_fundamental`,
`v3_worker_news`, `v3_worker_insider`, `ticker_validator`, `tournament_pitch`)
are **not** Prism-registered and, more importantly, have **zero rows in
`agent_tool_telemetry` across its entire history** (since 2026-07-12) —
confirmed by a direct count, not absence-of-evidence. `base_agent.py` calls
`record_tool_call` for any `agent_name` whenever `enable_tools=True`
regardless of a `v3_` prefix, so this isn't a recording gap by construction —
either these 7 code paths are not exercised in production, or they run through
a route that never reaches `run_agent_loop`'s tool-result hook. Their
whitelists (`user_chat` has 32 entries) are unverifiable from this table and
are flagged, not analyzed further, below.

The persona store (`app/config/agent_personas.json`, read via
`app.db.agent_persona_store._load_store`) was checked and does **not**
override any `v3_*` agent: its 6 entries are legacy V2 role keys
(`DATA_JANITOR`, `QUANT`, `FUNDAMENTAL`, `BEHAVIORAL`, `RISK`, `PM`), all
`allowed_tools: []`, none of which match a `v3_` agent name — so the static
whitelist files are in fact the live grant for every agent analyzed here.

---

## 1. Reconciliation — whitelisted vs. called, both ways

12 of the 13 v3 pipeline agents made at least one tool call in the window.
**`v3_portfolio_manager` made zero — not just in 14 days, but ever** (it has
a 13-tool `TOOL_WHITELIST` in `app/v3/agents/portfolio_manager.py` and is
registered with Prism). Either it always runs with `enable_tools=False`
(reasoning-only, in which case the whitelist is never rendered either — see
`get_agent_budget_turns`, which is only invoked `if enable_tools`), or its
tool path is broken. This audit cannot tell which from telemetry alone; it is
the single biggest open question this reconciliation surfaced.

| agent | whitelist (n) | calls/14d | dead-never-called | off-whitelist: force-added (known) | off-whitelist: **UNEXPECTED** |
|---|---:|---:|---|---|---|
| v3_bear_agent | 2 | 276 | — | execute_python, execute_javascript*, emit_structured_output, think | **read_url, search_web** |
| v3_board_of_directors | 9 | 214 | get_market_data, get_portfolio_state, get_strategy_health, propose_parameter_change, whiteboard_write | execute_python, emit_structured_output, think | **read_url, search_web** |
| v3_bull_agent | 2 | 315 | — | execute_python, emit_structured_output, think | **read_url** |
| v3_bull_defense | 1 | 219 | — | execute_python, emit_structured_output, think | **read_mcp_resource**† |
| v3_debate_judge | 1 | 226 | — | execute_python, emit_structured_output, think | — |
| v3_decision_synthesizer | 1 | 494 | — | execute_python, execute_command*, think | — |
| v3_delta_analyst | 4 | 8 | **get_market_data, get_portfolio_state, get_position_pnl, get_technical_indicators (all 4 — whole whitelist unused)** | execute_python, think | — |
| v3_fundamental_analyst | 16 | 553 | get_market_data, get_upcoming_events, list_scheduled_research, request_research_now, run_tool_chain, schedule_research, scrape_url | execute_python, execute_command*, emit_structured_output, think | **list_directory** |
| v3_junior_analyst | 13 | 691 | get_market_data, run_tool_chain | execute_python, think | **read_url, search_web, write_datastore**‡ |
| v3_portfolio_manager | 13 | **0** | **all 13 — never exercised at all** | — | — |
| v3_quant_analyst | 11 | 416 | calculate_stop_loss, get_market_data, get_portfolio_state, run_backtest, save_equation | execute_python, emit_structured_output, think | — |
| v3_regime_engine | 4 | 32 | **get_finnhub_news, get_market_data, get_technical_indicators, lazy_web_search (all 4 — whole whitelist unused)** | execute_python, think | — |
| v3_valuation_analyst | 7 | 411 | get_sec_filings | execute_python, execute_command*, emit_structured_output, think | — |

`*` = DENY policy held: `execute_command`/`execute_javascript` were attempted
(6 and 1 calls respectively) and every single one came back
`error_message="POLICY_DENIED"`, 0 succeeded. The security control in
`app/v3/prism_registration.py::_V3_DENIED_TOOLS` is holding for the whole
14-day window — no regression.

`†` = `read_mcp_resource` failed with `MCP server "mcp__lazy-agent-service__
whiteboard_read" is not connected` — a transport-layer error, not a whitelist
breach; 1 call.

`‡` = `write_datastore` (the write-side counterpart of the already-denied
`query_datastore`) is **not on any deny policy** and 3 of 4 calls **succeeded**
for `v3_junior_analyst`. Not flagged as a security regression in this audit
(scope is tool-surface reconciliation, not security), but it is a live gap:
nothing in `_V3_DENIED_TOOLS` covers it and one call already failed on a
namespace-format validation error, meaning the model is probing a mutating
tool that was never designed to be reachable.

### The real finding under "force-added"

`_META_TOOLS` (`discover_and_enable_tools, enable_tools, search_tools, think,
emit_structured_output, list_artifacts`) and `_FORBIDDEN`
(`execute_command, execute_javascript, execute_skill, write_file,
query_datastore`) plus the explicitly-allowed `execute_python` — the set
`app/v3/tool_telemetry.py` and `scripts/audit-loop.py` already know about —
account for most of the "called but not whitelisted" volume (`execute_python`
811 calls, `think` 768, `emit_structured_output` 226). **None of these 12
force-added names exist anywhere in `tool_schemas.json`** (91 rows, checked
against every name) — confirming they are Prism/platform-native tools, not
trading-service registrations, exactly as the code comments say.

But **5 more names are being force-added and are not accounted for anywhere
in this repo's canary logic** (`_META_TOOLS`/`_FORBIDDEN` don't list them, so
every one of these calls currently logs a `[ToolCanary] OFF-WHITELIST`
warning — the exact "canary that cries wolf" pattern the code already fixed
once for `emit_structured_output` on 2026-08-03, now recurring for 5 new
names):

| tool | calls/14d | agents | outcome |
|---|---:|---|---|
| `read_url` | 6 | bear, board, bull, junior | 100% success |
| `search_web` | 23 | bear, board, junior | 100% success |
| `list_directory` | 2 | fundamental | 100% success |
| `write_datastore` | 4 | junior | 75% success |
| `read_mcp_resource` | 1 | bull_defense | failed (MCP not connected) |

None of these exist in `tool_schemas.json` either. They read as a second,
undocumented wave of Prism CORE_AGENTIC tools (a generic web-search/read-URL/
filesystem/datastore-write bundle) that joined the platform after the
`_META_TOOLS` set was last updated. **Recommendation (not actioned — read-only
audit): add these 5 to `_META_TOOLS` in `app/v3/tool_telemetry.py`** so the
canary stops firing on benign, platform-injected, 100%-succeeding calls and
keeps its signal for real breaches.

---

## 2. Cost table — last 14 days, ranked by wall-clock burned (calls × p50)

All 32 distinct bare tool names seen in the window (MCP prefixes stripped via
`app.services.mcp_prefix.strip_mcp_prefix` before aggregating — 21 of 32
names arrived prefixed as `mcp__lazy-agent-service__*`, 0 arrived under the
retired `lazy-tool-service` spelling in this window, confirming the
2026-08-07 cutover is complete for live traffic). "Deadline death" uses the
same rule as `app/v3/tool_telemetry.py::record_tool_call`
(`BRIDGE_TOOL_DEADLINE_MS=55000`, flagged at `elapsed_ms >= 0.9 * 55000 =
49500` **and** `not success** — a call that finishes at 54.8s but succeeds is
not a death).

| tool | calls | distinct callers | success% | p50 | p95 | deadline-death % | burn (calls×p50) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **get_finnhub_news** | 108 | 2 | 70.4% | 22.2s | 84.4s | **13.0%** | **39.95 min** |
| **get_upcoming_events** | 93 | 1 | 90.3% | 22.2s | 87.7s | 3.2% | **34.42 min** |
| lazy_web_search | 236 | 4 | 92.8% | 0.9s | 32.7s | 1.7% | 3.44 min |
| get_polygon_price_history | 50 | 1 | 96.0% | 3.9s | 10.2s | 2.0% | 3.27 min |
| screener_query | 240 | 3 | 80.0% | 0.7s | 44.4s | 2.9% | 2.68 min |
| get_institutional_holdings | 99 | 2 | 95.0% | 0.9s | 40.7s | 1.0% | 1.45 min |
| get_market_data | 88 | 3 | 88.6% | 0.9s | 31.4s | 2.3% | 1.30 min |
| get_reddit_trending_stocks | 2 | 1 | **0.0%** | 30.0s | 30.0s | 0.0%* | 1.00 min |
| list_scheduled_research | 42 | 1 | 95.2% | 0.8s | 54.8s | 0.0% | 0.54 min |
| get_earnings_data | 78 | 2 | 97.4% | 0.4s | 24.6s | 0.0% | 0.53 min |
| whiteboard_annotate | 230 | 4 | 92.2% | 0.1s | 5.0s | 0.0% | 0.51 min |
| whiteboard_read | 448 | 8 | 94.0% | 0.1s | 1.4s | 0.7% | 0.38 min |
| get_finviz_fundamentals | 23 | 1 | 91.3% | 0.8s | 29.6s | 0.0% | 0.32 min |
| search_web | 23 | 3 | 100% | 0.8s | 3.7s | 0.0% | 0.31 min |
| execute_python | 811 | 12 | 94.7% | 19ms | 75ms | 0.0% | 0.26 min |
| whiteboard_write | 160 | 2 | 92.5% | 0.1s | 4.0s | 0.0% | 0.25 min |
| scrape_url | 14 | 1 | 85.7% | 1.0s | 4.2s | 0.0% | 0.24 min |
| run_equation | 71 | 1 | 97.2% | 0.2s | 4.1s | 0.0% | 0.22 min |
| search_equations | 15 | 1 | 100% | 0.8s | 4.0s | 0.0% | 0.20 min |
| think | 768 | 12 | **58.3%** | 4ms | 896ms | 0.3% | 0.05 min |
| emit_structured_output | 226 | 8 | 79.7% | 6ms | 15ms | 0.0% | 0.02 min |
| (remaining 11 tools) | ≤6 each | — | — | — | — | 0.0% | ≤0.01 min each |

`*` `get_reddit_trending_stocks`: only 2 calls, both timed out at ~30.0s
(29,995ms / 30,020ms) — too small a sample to rank, but the near-exact 30.0s
clustering on 0/2 success looks like an internal timeout distinct from the
55s bridge deadline; flagged for someone to check with more data, not sized
here.

`think`'s 58.3% success rate over this 14-day window is expected and already
explained by the established finding: `think` was DENY-policied from
2026-09-02 to 2026-09-06 (4 of these 14 days), producing the bulk of the
failures; not re-derived here.

**`get_finnhub_news`'s p95 (84.4s) is markedly worse than the previously
cited ~49.7s** figure. That earlier number came from a narrower snapshot
(a single cycle, per the comment in `tool_telemetry.py`); this 14-day pull
shows a fatter tail — max observed 147,973ms, several calls 2-3x the bridge's
own 55s hard deadline. That by itself is worth a flag: `elapsed_ms` for this
tool is not capped near 55s the way the bridge deadline implies it should be,
meaning either retries are being folded into one recorded call, or the
recorded clock starts before the bridge's own timer. Not root-caused further
here (out of scope — no fixing), but it means the "established" 49.7s
figure should not be treated as this tool's steady-state p95.

---

## 3. Dead weight in the prompt

Per-agent, using `app.agents.tool_whitelists.get_agent_tools(agent)` (the
literal schema list handed to the LLM) and
`app.services.context_gate.measure_tools`/`estimate_tokens` (the codebase's
own token estimator — 4-chars/token fallback here, tiktoken in a properly
provisioned environment):

| agent | schema tokens/run | dead tools (never called/14d) | dead tokens/run | % of run's tool tokens that are dead | est. dead tokens burned over 14d (dead tokens × distinct cycles) |
|---|---:|---:|---:|---:|---:|
| v3_portfolio_manager | 2,330 | 13 (100%) | 2,320 | **99.6%** | unknown — 0 cycles recorded, see §1 |
| v3_delta_analyst | 364 | 4 (100%) | 360 | **98.9%** | 1,440 (4 cycles) |
| v3_regime_engine | 309 | 4 (100%) | 306 | **99.0%** | 7,650 (25 cycles) |
| v3_board_of_directors | 1,444 | 5 | 795 | 55.1% | 42,135 (53 cycles) |
| v3_fundamental_analyst | 2,980 | 7 | 1,375 | 46.1% | 66,000 (48 cycles) |
| v3_quant_analyst | 1,967 | 5 | 785 | 39.9% | 49,455 (63 cycles) |
| v3_junior_analyst | 2,635 | 2 | 367 | 13.9% | 19,084 (52 cycles) |
| v3_valuation_analyst | 1,281 | 1 | 140 | 10.9% | 6,020 (43 cycles) |
| v3_bear_agent / v3_bull_agent / v3_bull_defense / v3_debate_judge / v3_decision_synthesizer | 162–246 | 0 | 0 | 0% | 0 |

**Total measurable dead-schema burn across the 7 agents that have any: ≈192,000
tokens over 14 days** (sum of the "est. dead tokens burned" column, excluding
the unknown `v3_portfolio_manager` figure). `v3_delta_analyst` and
`v3_regime_engine` are the cleanest cases: every whitelisted tool for both is
dead — both agents ran only on `execute_python`/`think` in the window, so
100% of their advertised, role-specific toolset is pure prompt overhead right
now (306-360 tokens/run, small in absolute terms because both whitelists are
tiny, but proportionally total waste). `v3_fundamental_analyst` carries the
largest absolute dead weight (1,375 tokens/run, 46% of its schema tokens) —
`get_market_data` alone is dead for **6 of the 13 agents that whitelist it**
(board_of_directors, delta_analyst, fundamental_analyst, portfolio_manager,
quant_analyst, regime_engine) while being genuinely used by `bull_agent` and
`valuation_analyst`. This matches the documented design in several of these
modules (e.g. `v3_valuation_analyst`'s comment: "everything else it needs is
already precomputed into the prompt by `app/quant/valuation_block.py`") — the
tool is a deliberate fallback for a value that's usually already inlined, not
a dead accident, but it still costs the same schema tokens on every run
whether or not the fallback ever fires.

---

## 4. Slow tools (p95 > 30s) — shortlist for the next optimisation pass, not fixed here

| tool | p95 | why slow (one sentence) |
|---|---:|---|
| `get_upcoming_events` | 87.7s | `app/tools/research_tools.py:212` calls `collect_earnings_calendar()` live on **every** invocation with no DB read-through/cache at all (unlike its siblings `get_market_data`/`get_finnhub_news`), so it always pays a live Finnhub round-trip and queues behind other callers on the same 5-slot Finnhub semaphore. |
| `get_finnhub_news` | 84.4s | `app/tools/finance_tools.py:171` has a read-through cache, but a miss falls back to `collect_finnhub_news()` gated by `rate_limiter.acquire("finnhub")` — a **shared, 5-concurrent-slot** semaphore (`FINNHUB_MAX_CONCURRENT=5` in `app/config/config.py:206`) also contended by `get_upcoming_events`, so under a multi-ticker cycle the wait time for a slot compounds with vendor latency into a long tail. |
| `screener_query` | 44.4s | `app/tools/screener_tools.py:101` does nothing itself but `await screener_client.query(...)` — a live HTTP call out to the trading-client screener backend (an external service, not this repo), with no local cache in this path. |
| `get_institutional_holdings` | 40.7s | `app/collectors/fund_scanner.py:345` (`get_institutional_signal`) and `:485` (`get_fund_momentum`) each run their **own, separate, unbounded** `mongo_store.find_docs("sec_13f_holdings", {"ticker": ticker})` — no `limit`, no index creation found anywhere in the codebase for that collection — then filter/sort/aggregate quarters in Python; one tool call issues at least two full unindexed scans of the same collection. |
| `lazy_web_search` | 32.7s | `app/tools/web_tools.py:196` queries its two RSS providers (Bing News, Google News) **sequentially** (`for name, fn in providers: await fn(...)`), each with its own 12s `httpx` timeout — a stalled first provider adds its full 12s before the second is even attempted, instead of racing them concurrently. |
| `get_market_data` | 31.4s (borderline) | `app/tools/finance_tools.py:33` already carries a documented read-through cache, but a cold cache (or a missing recent session) still runs **four sequential** vendor calls (`fetch_price_history`, `fetch_fundamentals`, `fetch_financials`, `fetch_balance_sheet`) inside one `rate_limiter.acquire("yfinance")` (3-concurrent) hold — the code's own comment already flags this as a known cost (previously measured 20.9s median / 36.0s p95 cold). |

Not included: `get_reddit_trending_stocks` (p95=30.0s, but n=2 in-window —
too small to size confidently, flagged in §2 instead).

---

## 5. Whitelist correctness risks

**Broken/missing names: none found.** Every name in every one of the 20
agents' whitelists (47 distinct tool names, union) resolves to a schema in
the scoped catalog (`tool_schemas.json`, 56 rows scoped to
`owner_app in {None, "trading"}`) — verified two ways: (a) direct set
comparison against the catalog, and (b) actually calling
`app.agents.tool_whitelists.get_agent_tools()` for all 20 agents and
diffing the requested whitelist against the returned schema names (this is
the exact "missing" check `get_agent_tools` itself logs a warning for — it
fired for none of them).

**Prefix-stripping: currently sound, one historical near-miss.**
`app/services/mcp_prefix.py::MCP_PREFIXES` covers
`mcp__lazy-agent-service__`, `mcp__lazy-tool-service__`, `mcp__lazy-tools__`,
and a bare `mcp_` catch-all. Checked against **every distinct raw `tool_name`
ever recorded** in `agent_tool_telemetry` (98 distinct values, full history
since 2026-07-12): **zero** carry an `mcp__*` prefix that fails to match one
of these four. The retired `lazy-tool-service` spelling has 8,714 rows
all-time but **0 in the last 14 days** — consistent with the documented
2026-08-07 cutover being complete for live traffic. No whitelisted name
itself starts with `mcp__`, `mcp_`, or `domain:`, so `mcp_tool_name()`'s
pass-through branch (which would silently skip prefixing) is never
triggered by accident.

**One thing worth a human look, not a "broken name":** the historical-only
`mcp__lazy-tool-service__*` rows include names that no longer exist in any
whitelist or the scoped catalog at all (`html_notes_news`,
`html_notes_read_page`, `html_notes_stock_news`, `html_notes_web_search`,
`query_financial_metrics`) — pre-dating the `owner_app` catalog-scoping fix
in `app/tools/registry.py`. These are dead history, not a live risk; noted so
nobody mistakes them for a currently-reachable tool if they grep telemetry
without a time filter.

---

## Caveats / what this audit could not verify from telemetry alone

1. **`v3_portfolio_manager` has zero rows in `agent_tool_telemetry` across
   the table's entire history**, not just 14 days. Its dead-weight and
   burn-extrapolation figures above are therefore based on its *whitelist*
   only, not on any observed run; whether it runs tool-enabled at all needs a
   direct check of `PipelineService`/its call site, not telemetry.
2. **7 of the 20 whitelisted agents (`user_chat`, both worker tiers,
   `ticker_validator`, `tournament_pitch`) also show zero rows ever** in this
   table. `record_tool_call` is not gated by agent-name prefix at the
   recording layer, so this is a genuine signal that these code paths are
   either unused in production or route around `run_agent_loop`'s hook —
   not confirmed further here (out of scope for a tool-surface audit; flagged
   for whoever owns those paths).
3. Token counts use the 4-chars/token fallback because `tiktoken` is not
   installed in this dev venv (it is pinned in `requirements.txt==0.13.0`,
   so production likely gets the more accurate `o200k_base` count) — treat
   the absolute token numbers as order-of-magnitude, the relative/percentage
   comparisons (which tool is worse than which) as solid, since the same
   estimator was applied uniformly.
4. `get_reddit_trending_stocks`'s 0/2 success at ~30.0s and
   `read_mcp_resource`'s single "not connected" failure are both n=1/n=2 —
   noted, not sized as trends.
