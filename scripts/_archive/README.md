# Archived Scripts

One-off diagnostic, scratch, and fix scripts moved here during the August 2026
audit cleanups (scratch_* on 2026-08-14, the rest on 2026-08-16). They were all
ad-hoc tools that served a specific debugging session and are preserved here
rather than deleted in case someone needs to reference the approach.

**These are NOT production scripts. They may use deprecated patterns like bare
`get_db()` calls that don't work with the context manager.**

| Script | What it did |
|---|---|
| `scratch_db.py` | Raw DB queries for debugging |
| `scratch_query.py` | Ad-hoc query runner |
| `scratch_query_errors.py` | Query execution_errors table |
| `scratch_query_state.py` | Query pipeline state |
| `scratch_trigger_and_poll.py` | Trigger a cycle command and poll for result |
| `scratch_trigger_command.py` | Insert raw system command |
| `check_pipeline_state2.py` | Second version of pipeline state checker (see `check_pipeline_state.py`) |
| `fix_state.py` | One-off pipeline state reset |
| `fix_pipeline_state.py` | Another pipeline state fix |
| `fix_max_tokens.py` | Regex-based max_tokens replacement across files |

Live cycle diagnostics are standardized in `scripts/cycle_healthcheck.py`,
`scripts/bench_stage.py`, `scripts/hold_wall_report.py`, and
`scripts/agent_scorecard.py`.
