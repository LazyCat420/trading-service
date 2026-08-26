# Behavioral audit step 1 — what `7b840b2` proved (2026-08-26)

The full chapter lives in trading-client `documentation/chapters/99-the-agents-first-behavioral-fixes-2026-08-26.md` (served at :8888). This is the service-side record of the three fixes in this repo and the evidence behind each.

## 1. `app/v3/agents/debate_judge.py` — CRITICAL RULES numbered 1–8, then "5." again

The tail rule (the one defining `weaknesses_of_winner` / `strongest_point_of_loser`, which the Board uses for sizing) was never renumbered when rules 5–8 were inserted. Renumbered to 9. Guard: `test_every_agents_numbered_rules_number_once` in `tests/unit/test_adaptive_fair_debate.py` sweeps every agent prompt — proven red against the old prompt.

## 2. `app/v3/prism_registration.py` — the registration seam accepted an empty whitelist

`[]` has three meanings in this codebase: UNSCOPED at prism registration (prism drops `coreToolsLocked` and force-adds the core tool set), zero tools in the static `AGENT_TOOL_WHITELISTS` map, and tools-off/1-turn in `agent_runner`. `test_no_v3_whitelist_is_empty` only guards modules inside `app/v3/agents/`. `register_v3_agents` now refuses an empty `TOOL_WHITELIST` (logs `REFUSING to register`, marks the module failed, leaves the previously-registered persona with its DENY policies in place). Guard: `test_registration_refuses_an_empty_whitelist` in `tests/unit/test_block_superseded_tools.py` drives a fake empty-whitelist module through the real function with `PRISM_URL` empty — red without the guard, no network needed.

## 3. `app/v3/agent_runner.py` — the junior's MANDATORY market_context write was prompt-only

Measured (whiteboard_entries × v3_agent_telemetry): 0/48 desks before 08-19, 54/62 (87%) since, 6 misses across 08-25/26. `_persist_junior_market_context` now derives the section from the artifact (`key_findings` top-3 + `catalyst_call`, `already_priced_in` preserved) as a FALLBACK: an existing agent write wins (write_section supersedes — a digest must not replace prose), empty artifacts post nothing, derived rows carry `derived_from_artifact: true`. Pinned to `v3_junior_analyst` by an agent-name guard with a test enforcing the pin. Tests: `tests/unit/test_junior_market_context_fallback.py`.

## Verification

- Both guards red on old code, green on new; full unit suite 5,264 passed / 0 failed.
- Deployed to synology; container healthy at `git.sha=7b840b2` (checked via `ssh synology "sudo /usr/local/bin/docker inspect ..."` — the bare `ssh sudo docker` probe in `verify_shipped.py` fails from WSL with "sudo: docker: command not found": non-interactive PATH).
- Reference cycle before the deploy: `cycle-v3-1787751005` scored 116/116 on `scripts/agent_contract_report.py`.

## Open

- Watch the next discovery cycle: market_context should reach 100% of junior-SUCCESS desks; count `derived_from_artifact: true` rows vs agent-written to see how much the fallback carries.
- Whether the pre-08-19 zeros were real misses or a store artifact: check the frozen PG whiteboard table for market_context rows before 08-19.
- Remaining per-agent behavioral inventory items from the 08-26 session (beyond the three fixed here).
