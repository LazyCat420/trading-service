"""
Quant equation tooling.

What remains here after the 2026-08-28 tournament retirement:
  - equation_library: shared quant-equation storage and sandboxed executor,
    exposed to agents through app/tools/quant_tools.py
  - backtest_runner: deterministic backtest for a stored equation, also
    reached through quant_tools
  - equation_lab: nightly job that compiles new equations into the library
  - panel_math: scoring maths, retained for scripts/score_panel.py, which
    scores the panel runs still on record

The 4-stage Tournament Debate (pitch → backtest → h2h → jury) and the
probabilistic panel that replaced it are DELETED, along with debate_coordinator,
format_validator, action_gate, thesis_agent, specialized_agents and this
package's own debate_judge. They had been unreachable since 2026-07-12 and were
retired on measurement — see the "# Debate" block in
app/services/parameter_store.py for the numbers, and app/v3/agents/debate_judge.py
for the judge that actually runs.
"""
