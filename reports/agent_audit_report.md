# Trading Cycle Agent Audit Report - Cycle cycle-1780889243

This report documents the findings and anomalies observed during the observation-only monitoring of trading cycle `cycle-1780889243`.

## 1. Invoked Agents and Purposes

During the active cycle, the following agents were invoked:

| Agent Name | MongoDB Conversation / Title | Purpose / Responsibility |
| :--- | :--- | :--- |
| **Data Curator** | `CUSTOM_SYSTEM_JANITOR_AGENT · <ticker>` | Curates and filters news/Reddit articles for watchlist tickers. |
| **Position Monitor** | `Agent: position_monitor_<ticker> · <ticker>` | Evaluates held portfolio positions (`BCE`, `SW`) on a fast-track single-agent loop to determine if they should be held or sold. |
| **Cycle Analysis Agent** | `CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT` | Extracts positive behavioral rules from agent trajectories and does trajectory reflections. |
| **Post-Cycle Learner** | `CUSTOM_SYSTEM_JANITOR_AGENT · <ticker>` (Mis-mapped) | Extracts reusable lessons from high-confidence/notable decisions and writes them to TradingMemory. |
| **Quant Research Agent** | `quant_research__RESEARCH__selector` (Selector) <br> `Agent: quant_research__RESEARCH_` (Run) | Performs post-cycle research on mathematical indicators or strategies, writing insights back to LLMWiki memory. |
| **Meta Audit Agent** | `meta_audit__AUDIT__selector` (Selector) <br> `Agent: meta_audit__AUDIT_` (Run) | Audits the overall decision quality, portfolio health, and metrics of the cycle, scheduling the next cycle. |

---

## 2. Identified Anomalies and Issues

### A. Missing Integration of Post-Cycle Observation
- **Observation**: Developer 2's module `app/cycle/orchestration/post_cycle_observe.py` defines `create_decision_observation` and `create_outcome_observation` to log raw episodic insights.
- **Issue**: This module is never imported or called anywhere in the cycle execution flow (e.g. `post_cycle_hooks.py` does not reference it).
- **Database Gap**: The PostgreSQL database lacks the `episodic_observations` table entirely. If this code had been hooked up, the database writes would have thrown fatal errors because the table does not exist.

### B. Mismatch in Custom Agent Registration and 409 Conflicts
- **Observation**: When running `run_prism_agent`, the client attempts to register dynamic custom agent IDs like `quant_research__RESEARCH_`.
- **Issue**:
  - The client formats names with double underscores: `quant_research__RESEARCH_` -> `CUSTOM_QUANT_RESEARCH__RESEARCH`.
  - The Prism server normalizes display names and compresses multiple underscores, storing them as `CUSTOM_QUANT_RESEARCH_RESEARCH` (single underscore).
  - The client's lookup check `agent.get("agentId") == agent_id` fails because of the mismatch. The client then repeatedly attempts to register the agent via a `POST /custom-agents` request, triggering a `409 Conflict` error on the server. Although handled gracefully as a warning, it fills logs with clutter.

### C. Post-Cycle Learner Routing / Mapping Confusion
- **Observation**: The `post_cycle_learner` (which runs `maybe_learn`) routes to `CUSTOM_POST_CYCLE_LEARNER_AGENT`.
- **Issue**:
  - In `prism_agent_registry.py`, both `CUSTOM_POST_CYCLE_LEARNER_AGENT` and the name `post_cycle_learner` map directly to `CUSTOM_SYSTEM_JANITOR_AGENT`.
  - As a result, MongoDB logs store these interactions under the title `CUSTOM_SYSTEM_JANITOR_AGENT · <ticker>`, causing confusion since the logs depict the janitor agent executing learning rules instead of data curation.

### D. Empty/Quick Collection Phases
- **Observation**: The collection phase logged `0.0s` elapsed time. Curation did run successfully and processed 8 qualitative pieces, but tickers already held in the portfolio (`BCE`, `SW`) bypassed full debate loops to avoid unnecessary token consumption.

---

## 3. Conclusions and Recommended Refactoring
With the active run successfully completed, the following next steps are recommended for the improvement phase:
1. Map out all agents defined in the codebase and simplify the agent registry to eliminate duplicate or redundant aliases.
2. Formally hook up `post_cycle_observe.py` to `post_cycle_hooks.py` and create the required `episodic_observations` database schema in PostgreSQL.
3. Clean up the dynamic agent registration name formatting to avoid duplicate underscores and prevent 409 conflict warnings.
4. Improve mapping clarity in the registry so that post-cycle learning, curation, and janitorial operations are cleanly segregated in MongoDB logs.
