# Audit: cycle → summary → AUTORESEARCH command → worker claim → report → client, as one contract

Scope: trading-service (`app/autoresearch/*`, `app/services/pipeline_service.py`,
`app/services/boot_service.py`) and trading-client (`app/routers/autoresearch.py`,
`frontend/src/components/AutoResearchPanel.jsx`). Read-only; Mongo queried live
(`trading_bot` DB) via `.venv/bin/python`, scripts left under
`scratchpad/audit_mongo.py` and `audit_mongo2.py`.

---

## 1. Partial-failure honesty at core.py:303

The write is one Python statement:

```python
mongo_store.update_docs('autoresearch_reports', {'id': report_id}, {'$set': {
    'score_version': score_ver, 'data_quality_score': ..., ...,
    'data_gaps': json.dumps(data_quality.get("gaps", [])),
    'decision_issues': json.dumps(decision_quality.get("issues", [])),
    'llm_issues': json.dumps(llm_analysis.get("issues", [])),
    'performance_metrics': json.dumps(perf_metrics),
    'reflection': json.dumps(reflection),
    'recovery_stats': json.dumps(recovery),
    'status': 'done'}})
```
(`app/autoresearch/core.py:303`)

Python evaluates every value in a dict literal — all six `json.dumps()` calls —
**before** `mongo_store.update_docs` is invoked. If any one of the six raises
(`TypeError: Object of type X is not JSON serializable`), none of the six get
written; the call never happens. The exception is not caught locally — it
propagates out of the `try:` opened at `core.py:116` and is caught by
`except Exception as e:` at `core.py:403-410`, which sets
`status='error'`, `error=str(e)` (via `_update_ar_state` at 405 and an explicit
`update_docs` at 407-408).

**Answer: No, a report cannot reach `done` with a section that failed to
serialize.** The write is all-or-nothing, and the failure path correctly
demotes the whole report to `error`. This matches the already-known incident
(cycle-v3-1788660665, `recovery_stats.recent_events[].at`): that report is
`status='error'`, not `done` with a null/partial field — exactly the trace
above predicts.

Two things worth flagging even though the direct question is "no":

- **Atomicity is also a liability.** One bad field anywhere in the six
  (say, a future producer leaks a numpy scalar into `performance_metrics`)
  discards the other five, which may all have serialized fine. A cycle with
  perfectly good `data_quality`/`decision_quality`/`llm_analysis` results in
  a report with **zero** scores and `status='error'`, because there is no
  per-section fallback or partial persist.
- **Some sections are *designed* to never reach the row at all** — this is a
  second, non-exceptional way "done" can carry less than what was computed.
  The full `audit_bundle` built at `core.py:244-269` includes `triage_audit`,
  `schedule_health`, `execution_errors`, `learning_signals`, and the complete
  `data_quality`/`decision_quality`/`llm_analysis` objects (per-ticker
  breakdown, `outcome_stats`, availability/judge/eval breakdown). None of
  these are in the `$set` at `core.py:303` — only `.gaps`/`.issues`/`.issues`
  subsets and score scalars survive. They are consumed once, by the
  reflection LLM's prompt (`reflection.py:34-124`), and then discarded; if the
  LLM's prose doesn't mention a finding, it leaves no trace anywhere a report
  reader can see. Trading-client's own `REPORT_COLS`
  (`app/routers/autoresearch.py:57-72`) matches exactly what core.py persists
  — confirming this is structural, not a client bug — yet the panel's own
  phase list still advertises `triage_audit` / `schedule_audit` as steps
  (`AutoResearchPanel.jsx:806`, `:809`) with no field to ever render their
  result on a finished report.

---

## 2. The other five `json.dumps()` calls — types and converters

| field | producer | contents today | risky types today | explicit converter |
|---|---|---|---|---|
| `data_gaps` | `_audit_data_quality` — `app/autoresearch/auditors/data_audit.py:280-338` (list built ~309-322) | `{"ticker": str, "missing_sources": list[str], "recommendation": str}` | none currently | **No.** The function does call `_safe_iso()` on date fields, but those land in `per_ticker`/other keys, never in `gaps` itself. |
| `decision_issues` | `_audit_decisions` — `app/autoresearch/auditors/decision_audit.py:112-345` (issues appended throughout) | `{"issue": str, "severity": str}`, all built via f-string formatting | none currently | **No.** Nothing centralizes a JSON-safety pass over `issues`. |
| `llm_issues` | `_audit_llm_traces` — `app/autoresearch/auditors/llm_audit.py:15-174` (issues at 54-63, 140-152) | `{"issue": str, "severity": str}` | none currently | **No.** |
| `performance_metrics` | assembled in `core.py:194-239` from `_audit_performance` (`performance_audit.py:15-26`, plain ints/strs off `cycle_summary`) + `decision_cohort`/`outcome_stats` (`core.py:205-230`) + `confidence_calibration.calibration_map()` (`confidence_calibration.py:79-146`) | ints/floats/strings/lists of strings | **Decimal** (documented in-code: `core.py:200-203`, "DB-sourced numerics can be Decimal, which strict json.dumps rejects") | **Yes** — a local `_jsonsafe()` helper (`core.py:200-203`) is applied to every `decision_cohort`/`outcome_stats` value specifically for this reason, and `calibration_map()` independently wraps every value in explicit `float()`/`int()`/`round()` (`confidence_calibration.py:130-146`). This is the *only* field with a documented defense against the exact defect class asked about. |
| `reflection` | `_reflect()` — `app/autoresearch/reflection.py:13-160` | LLM path: `parse_json_response(response)` — anything surviving a JSON round-trip from the LLM's own text is, by construction, limited to JSON-native types. Rule-based fallback (`_rule_based_reflection`, :160-172) builds only str/list. `core.py:279-289` then adds three plain fields (`partial_cycle: bool`, `anomaly: bool`, `anomaly_detail: str`) after `_reflect()` returns. | none possible today | Structurally safe (JSON round-trip / hand-built dict), not by an explicit converter. |
| `recovery_stats` | `_audit_recovery` — `app/autoresearch/auditors/performance_audit.py:75-190` | **Already the known-fired defect.** `recent_events[].at` held raw Mongo `datetime` objects from `cycle_audit_log.timestamp`/`pipeline_events.timestamp` until converted *after* the sort (`performance_audit.py:179-187`, `if hasattr(at,"isoformat"): e["at"]=at.isoformat()`). `by_type`/`by_agent` are `dict(Counter(...))` (ints, safe); a possible `error` string is safe. | datetime (fixed) | Yes, but scoped to `.at` only — added specifically after the incident. |

**Finding:** of the five siblings, only `performance_metrics` (and structurally
`reflection`) carry any defense against the datetime/Decimal128/ObjectId/numpy
class of bug that hit `recovery_stats`. `data_gaps`, `decision_issues`, and
`llm_issues` are safe **only because their current producers happen not to put
a raw DB type in an issue/gap dict** — nothing enforces that. None of the three
producer functions import or share a `_jsonsafe`/`_safe_iso`-style helper the
way their neighbors do. Per finding #1, if any one of them ever does (e.g. an
"issue" dict grows a raw `created_at` for provenance), the *whole* report — not
just that field — drops to `status='error'` with no scores.

---

## 3. Cycle attribution — readers enumerated

`enqueue_autoresearch()` (`app/services/pipeline_service.py:421-461`) is called
from **three** cycle-tail sites — done (`:2961`), stopped (`:3062`), error
(`:3094`) — each unconditionally creating a brand-new `job_id` / command row,
with no check for "has this cycle_id already been enqueued." Uniqueness is
not enforced anywhere downstream either. Live in Mongo right now:

```
cycle_id            n reports
battle-orch          7
cycle-v3-1788486930  4   (scores: 84.7, 87.8, 87.8, 87.8 — not identical)
battle-skip          3
cycle-1781496566     2
cycle-v3-1784554200  2
```
(5 of 681 distinct cycle_ids currently have >1 report; `battle-*` are test
fixtures written straight into the shared store.)

Readers:

| reader | filters by cycle_id? | verdict |
|---|---|---|
| `get_latest_report()` — trading-client `app/routers/autoresearch.py:78-89` (`GET /api/v1/autoresearch/latest`) | **No** — `sort=[("created_at",-1)]`, no cycle_id predicate | **UNSAFE.** Returns whichever report is newest system-wide. |
| `get_status()` — trading-client `app/routers/autoresearch.py:101-121` (`GET /status`) | **No** — same "latest row" query at `:107` | **UNSAFE**, and doubly so: `is_running = bool(row or report_status == 'running')` (`:117`) means an unrelated stale/orphaned "latest" report can force `running=True` on its own (see §5). |
| `get_report_history()` — `app/routers/autoresearch.py:93-98` (`GET /reports`) | Each row carries its own honest `cycle_id`, but the endpoint offers no server-side cycle_id filter | **Safe per-row, but no attribution query exists** — a caller wanting "the report for cycle X" must scan client-side; nothing does. |
| `AutoResearchPanel.jsx` (frontend, `:741-745`, `:760-788`, `:919`) | Consumes only `/latest` and `/status`; renders `report.cycle_id` as a label but never cross-checks it against "the cycle I'm watching" | **UNSAFE by inheritance.** |
| `record_autoresearch_metrics()` — trading-client `app/services/subsystem_benchmarks.py:65` | **Yes**, `{'cycle_id': cycle_id}` — but no `sort` | Safe on scope, **non-deterministic under duplicates** (Mongo's natural order decides which of N reports is "the" one recorded). |
| `scripts/collect_cycle_bundle.py:48` | Yes | Safe (diagnostic tool). |
| `llm_audit.py:128` (trend) / `janitor.py:122` (degenerate-score) | No, by design — rolling windows across many reports | Not an attribution claim; not a defect. |
| `_reflect()`'s prompt (`reflection.py`) | N/A — never reads `autoresearch_reports` back; builds entirely from the current run's own `audit_bundle` plus a separate `cognition/lesson_store` collection | Safe — no "latest report" dependency. |

**Bottom line:** the two endpoints the client panel actually calls
(`/latest`, `/status`) are both "most recent report system-wide," never "the
report for this cycle." Combined with unenforced uniqueness, whichever
duplicate finishes last silently becomes what the user sees, with the other
runs (and any disagreement between their scores) invisible.

---

## 4. State vocabulary

Code-reachable values of `autoresearch_reports.status`:

- `running` — set on insert, `core.py:135`
- `done` — `core.py:303`, and `_update_ar_state(running=False)` maps to it, `core.py:26-31`
- `error` — `core.py:403-410`
- `interrupted` — `asyncio.CancelledError` handler, `core.py:381-401`; and the `finally` backstop at `core.py:411-425` when a report is found still `running` with `overall_score is None`
- `stale` — self-cleanup at the top of the *next* `run_autoresearch` call, `core.py:122-124`; mirrored by `janitor.py:_clean_stale_reports()` (`app/autoresearch/janitor.py:80-92`), itself only run as a phase inside `run_autoresearch` (`core.py:372-376`)

**Actually stored right now** (live query): `{'done': 690, 'error': 4}` — **zero**
rows at `running`, `interrupted`, or `stale`, out of 694 total (10 of which are
`battle-*` test fixtures, not real cycles).

Client rendering — `AutoResearchPanel.jsx`:

- No report at all (`/latest` → `{"status":"no_reports"}`) → `hasReport=false`
  (`:847`) → the whole cycle-info/score block is skipped. Distinct "not yet
  written" state, correctly separated.
- `report.status === 'done'` → green `var(--success)` badge (`:925`).
- `report.status === 'interrupted'` → amber `#f59e0b` badge **plus** an
  explanatory line, "stopped during '{phase}' — scores were never computed"
  (`:926`, `:929-932`).
- **Every other value** — `running`, `error`, `stale`, or anything unforeseen —
  falls through to the same final branch, `: 'var(--danger)'` (`:926`). Same
  red color; the only thing that differs is the literal text
  `{report.status}` rendered inside the badge (`:927`).

**Collapsed states: `running`, `error`, and `stale` render identically** (same
red badge); only `done` and `interrupted` get their own color and explanation.
A viewer cannot tell "still running" from "gave up" from "errored out" by
looking at the badge — only by reading the word inside it, and `stale` (which
the code can produce) has no explanatory text at all, unlike `interrupted`.

---

## 5. Restart interaction — boot_service.py:238-243

```python
mongo_store.update_docs('pipeline_state', {...}, {'$set': {'status': 'error', ...}})
mongo_store.update_docs('v3_system_commands', {'status': {'$in': ['running','pending']}}, {'$set': {'status': 'error', ...}})   # :241
mongo_store.update_docs('system_commands',    {'status': {'$in': ['running','pending']}}, {'$set': {'status': 'error', ...}})   # :242
```

This touches `pipeline_state` and **both** command queues. It never references
`autoresearch_reports` (confirmed: zero hits for "autoresearch" anywhere in
`boot_service.py`).

So a report whose worker was killed mid-flight (container dies between two
`_update_ar_state()` calls — never reaching the `CancelledError` branch or the
`except`/`finally` blocks, because the process itself is gone) is left exactly
where the crash caught it: `status='running'`, some `phase`, `overall_score=None`.
Boot marks its **command** row `error`; the **report** row is untouched —
an orphaned `running` report.

**Resolution path:** the only code that ever revisits a `running` report is
(a) `core.py`'s own inline sweep at the top of the *next* `run_autoresearch`
call (`:122-124`, 30-minute cutoff → `stale`), or (b)
`janitor.py:_clean_stale_reports()` (same 30-minute cutoff), which itself only
executes as a phase *inside* `run_autoresearch` (`core.py:372-376`). **Both
require a brand-new AUTORESEARCH job to run to completion** — i.e., a new
cycle. And `boot_service.py:255-262` explicitly starts the system **paused**
on every boot ("Start the system PAUSED by default on boot ... until the user
explicitly starts a trading run or resumes"). So immediately after the crash
that orphaned the report, no cycle runs, and nothing ever sweeps it, until a
human manually resumes.

**Client-visible consequence:** trading-client's `get_status()`
(`app/routers/autoresearch.py:107-118`) computes
`is_running = bool(row or report_status == 'running')`. The `row` half
(pending/running rows in `system_commands`/`v3_system_commands`) is correctly
cleared by boot. But `report_status` is read from "the latest
`autoresearch_reports` row by `created_at`" — which, until a new report is
written, **is** the orphaned one. So `is_running` stays **true**, and the
panel's progress bar / "AutoResearch running…" spinner is pinned on
indefinitely — precisely during the window (system paused, no cycle in
flight) where nothing can ever fix it — with no error surfaced anywhere on
the client side, even though the command-queue side already recorded one.

**Can it ever be resolved?** Yes, but only via: a human resumes trading → one
full cycle completes → that cycle's own `enqueue_autoresearch` produces a
*newer* report row, which (1) becomes the new "latest" for `/latest` and
`/status`, pushing the orphaned row out of consideration, and (2) that new
`run_autoresearch` call's own stale-sweep formally reclassifies the old row
to `stale` in the store (invisible to the client either way — see §4). There
is no independent boot-time or scheduled reconciliation between
`autoresearch_reports` and the command queues it is derived from.

---

## Evidence scripts

- `scratchpad/audit_mongo.py` — status distribution, running/stale/interrupted
  counts, cycle-v3-1788486930 confirmation, duplicate cycle_id sweep.
- `scratchpad/audit_mongo2.py` — battle-*/cycle-* breakdown, earliest/latest
  timestamps for `autoresearch_reports` vs `system_commands` (reports go back
  to 2026-05-07; the AUTORESEARCH command queue only exists from 2026-07-05 —
  explains most of the 694-vs-478 count gap: older reports predate the
  queue-based dispatch entirely).
