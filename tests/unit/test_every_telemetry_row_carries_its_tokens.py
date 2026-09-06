"""A measured token count that is not passed to the row is a zero in the ledger.

MEASURED 2026-09-06 over 56 days of `v3_agent_telemetry`: 584 non-success rows,
and `prompt_tokens == 0` on every one of them. 193 of those (33.0%) had their
spend MEASURED in-process — `token_usage > 0` — and dropped at the telemetry
call: 11,817,729 tokens recorded as free. Three call sites in
`run_v3_agent` handle a run whose artifact failed to parse (`output_rule`
class, SCHEMA_INVALID, missing required fields); `prompt_tokens` and
`cached_tokens` are bound from the run result a thousand lines above and are
in scope at all three, and the SUCCESS site next to them passes both. The
three simply did not.

That is the larger half of the "partial cost" problem: the genuinely unknown
case (a request cancelled mid-flight) is 39 rows; the measured-and-dropped
case is 193. So the guard is structural: every `_record_telemetry` call in the
runner passes `prompt_tokens=`. The runner's crash paths cannot be driven from
a test — the `except` blocks sit 1,600 lines inside one function — so the
call sites are checked as source, with a negative control so the scan cannot
be vacuous.
"""
from __future__ import annotations

import ast
import inspect

from app.v3 import agent_runner

RECORDER = "_record_telemetry"


def _calls_missing_kwarg(source: str, kwarg: str) -> list[int]:
    tree = ast.parse(source)
    missing: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == RECORDER):
            continue
        if not any(k.arg == kwarg for k in node.keywords):
            missing.append(node.lineno)
    return missing


def test_every_telemetry_row_in_the_runner_carries_prompt_tokens():
    src = inspect.getsource(agent_runner)
    missing = _calls_missing_kwarg(src, "prompt_tokens")
    assert missing == [], (
        f"{RECORDER} is called without prompt_tokens= at agent_runner lines "
        f"{missing}: the run's measured spend is in scope there and the row "
        "records 0 — 11.8M tokens went unrecorded this way in 56 days"
    )


# No matching scan for `cached_tokens`: the three crash sites (TIMED_OUT,
# CANCELLED, RUNNER_EXCEPTION) record a run that never returned, so no cached
# count exists there and passing 0 would be a confident zero with no producer.
# `prompt_tokens` differs — the crash sites carry the cost sink's figure, and
# the parse-failure sites carry the run result's.


def test_the_scan_sees_a_call_that_drops_the_kwarg():
    """Negative control."""
    bad = (
        "def f():\n"
        "    _record_telemetry(desk, 'a', 1, 2, 3, 'AGENT_ERROR',\n"
        "                      sys_prompt_chars=1, model_used='m')\n"
    )
    assert _calls_missing_kwarg(bad, "prompt_tokens") == [2]


def test_the_scan_accepts_a_call_that_passes_it():
    good = (
        "def f():\n"
        "    _record_telemetry(desk, 'a', 1, 2, 3, 'AGENT_ERROR',\n"
        "                      prompt_tokens=pt, cached_tokens=ct)\n"
    )
    assert _calls_missing_kwarg(good, "prompt_tokens") == []
