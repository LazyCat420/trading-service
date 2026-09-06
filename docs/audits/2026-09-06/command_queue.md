# The command-queue contract — audit

Scope: `trading-service` (primary checkout, read-only), with brief read-only
cross-reference into the sibling `trading-client` repo where it is a producer
onto one of the two collections in scope. No files were edited, no writes were
made, nothing was deployed. All Mongo access was read-only
(`find`/`count_documents`/`aggregate`/`distinct`) against `trading_bot` via
`PRISM_MONGO_URI`.

**Caveat on freshness**: `app/services/pipeline_service.py` changed mtime
*during this audit session* (2026-09-06 09:34:58 local — a live edit landed
while this audit was running, evidenced by two successive reads of the same
line range returning different code: an inline `system_commands`-writing
block first, then a call to a new `enqueue_autoresearch()` helper). Every
finding below reflects the **final** re-read of each file, confirmed via a
fresh `grep`/`sed` immediately before writing this report. Where the change
mid-flight is itself informative (it is, for §2 and the AUTORESEARCH
duplication story) it is called out explicitly.

---

## 1. Inventory table

| command_type | producer(s) — file:line | trigger | target collection | payload type | required fields written | consumer — file:line | terminal statuses observed |
|---|---|---|---|---|---|---|---|
| `START_CYCLE` | `app/services/cycle_queue.py:28` `enqueue_start_cycle` (shared helper) — called from `app/services/research_governor.py:154` (`request_research_now`, agent tool call), `app/services/cycle_scheduler.py:1817` (`_run_market_open_cycle`, APScheduler cron), `app/services/watch_desk.py:847` (`_enqueue_wake`, Watch Desk ticker wake), `scripts/canary_loop.py:26` (ops script). Also **trading-client** `app/routers/pipeline.py:41` `_enqueue_command` (UI "Start Cycle" button/API) — a *second*, independently-fixed helper, not a call into `cycle_queue.py`. Also two manual ops scripts that construct the doc locally rather than importing the helper: `scripts/trigger_cycle.py:107`, `scripts/observe_cycle.py:81` (both replicate the full field set by hand). | endpoint/button, scheduler cron, watch desk, governor, CLI | `v3_system_commands` | JSON **string** (`json.dumps`) | `id`, `command_type`, `payload`, `status="pending"`, `progress=0`, `created_at` | `cycle_main.py:132` `poll_system_commands` — atomic claim via `find_one_and_update` (`cycle_main.py:142`) → `PipelineService.start_cycle`/`_run_all_v3` | `completed`, `skipped` (when `PipelineService.start_cycle` returns `deduplicated`/`error`/`ignored`), `error` |
| `START_V3_CYCLE` (legacy alias, same branch) | same producers; `scripts/observe_cycle.py:83` uses this literal type | same | `v3_system_commands` | JSON string | same | `cycle_main.py:132`, same branch (`cmd_type in ("START_CYCLE","START_V3_CYCLE")`) | same |
| `STOP_CYCLE` | **trading-client** `app/routers/pipeline.py:402,418` `_enqueue_command("STOP_CYCLE", ...)` (UI Stop / Fast-Stop buttons). No producer inside trading-service. | dashboard button | `v3_system_commands` | JSON string | complete (see `_enqueue_command`, `pipeline.py:41`) | `cycle_main.py:132` → `PipelineService.request_stop`/`stop_cycle` | `completed`, `error` |
| `FORCE_RESET` | **trading-client** `pipeline.py:531` `_enqueue_command` | dashboard button | `v3_system_commands` | JSON string | complete | `cycle_main.py:132` → `PipelineService.force_reset` | `completed`, `error` |
| `PAUSE_CYCLE` / `RESUME_CYCLE` | no live producer found in either repo | — | `v3_system_commands` | — | — | `cycle_main.py:184-186` replies `{"status":"not_supported"}` immediately | `completed` (immediate no-op) |
| `FLASH_BRIEFING` / `MORNING_BRIEFING` / `GENERATE_MORNING_BRIEFING` | **trading-client** `pipeline.py:198` `_enqueue_command(job_id, cmd_name, payload)` (dashboard "run now" buttons). The *scheduled* flash/morning briefings (`app/services/cycle_scheduler.py` `_run_flash_briefing`/`_run_morning_briefing`) call `generate_flash_briefing()`/`generate_morning_briefing()` **directly** and never touch this queue — the cron path and the button path are two different mechanisms for the "same" feature. | dashboard button (queued) vs. scheduler cron (direct call, bypasses the queue entirely) | `v3_system_commands` | JSON string | complete | `cycle_main.py:190-196` → `generate_flash_briefing`/`generate_morning_briefing` | `completed`, `error` |
| `DISCARD_CHECKPOINT` / `FORCE_CHECKPOINT` (v3) | **trading-client** `pipeline.py:454,463` `_enqueue_command` | dashboard button | `v3_system_commands` | JSON string | complete | `cycle_main.py:198-201` — intentional no-op ("No checkpoint system active") | `completed` (immediate no-op) |
| `REFRESH_SCHEDULE` | **Two producers, one compliant, one not.** Compliant: `app/services/cycle_queue.py:47` `enqueue_refresh_schedule` (shared helper), called from `app/services/research_governor.py:329,411` (bot schedule create/cancel). **Non-compliant**: **trading-client** `app/routers/scheduler.py:140,174,184,200,209` (`create_schedule`/`update_schedule`/`delete_schedule`/`delete_all_schedules`/`toggle_schedule`) each build `{'id':cmd_id,'command_type':"REFRESH_SCHEDULE",'payload':json.dumps(...)}` **directly**, bypassing both `cycle_queue.py` and its own repo's fixed `pipeline.py:_enqueue_command`. | schedule CRUD (client UI) + governor (bot-created schedules) | `v3_system_commands` (moved here from `system_commands` in trading-client commit `5afa6b14`, 2026-08-28) | JSON string | Compliant path: `id`,`command_type`,`payload`,`status="pending"`,`progress=0`,`created_at` (complete). **Non-compliant path (`scheduler.py`): `id`,`command_type`,`payload` only — `status` and `created_at` omitted.** | `cycle_main.py:84` `drain_schedule_refreshes` — per-row atomic claim via `find_one_and_update` (`cycle_main.py:107`), filtered on `{"status":"pending","command_type":"REFRESH_SCHEDULE"}` | Compliant docs: `completed`. **Non-compliant docs: invisible forever** — a missing `status` field cannot match a `{"status":"pending"}` filter. |
| `AUTORESEARCH` | `app/services/pipeline_service.py:421` `enqueue_autoresearch()` — one function, called from all three terminal tails of `_run_all_v3`: the "done" tail (`pipeline_service.py:2957`), the `CancelledError`/"stopped" tail (`:3062`), and the generic-`Exception`/"error" tail (`:3094`). The three-tail wiring is dated **2026-09-06 (today)** per its own comment — before that, only the "done" tail enqueued AUTORESEARCH, so a stopped-while-trading cycle (e.g. LULU on `cycle-v3-1788642086`) never got a reflection at all. | end of every trading cycle (success, human stop, crash) | `system_commands` | **dict / document** (never JSON-stringified — the eval worker indexes `payload["cycle_id"]` directly) | `id`, `command_type`, `payload`(dict), `status="pending"`, `created_at` | `app/autoresearch/eval_worker.py:101` `poll_system_commands` — **non-atomic**: `find_row` (`mongo_query.py:169`, called at `eval_worker.py:105`) then a separate `update_docs` claim (`eval_worker.py:110`) → `run_autoresearch` | `completed`, `error` |
| `ACTIVATE_BRAIN_GRAPH` | trading-client "Activate" button on the Brain Graph panel (writes into `system_commands`; every live doc sampled carries `created_at`, so this producer is not reproducing the missing-field defect) | dashboard button | `system_commands` | dict | complete | `eval_worker.py:41` `run_activate_brain_graph`, with `progress`/`progress_message` updates mid-flight | `completed`, `error` |
| `RUN_FRED_COLLECTION` | trading-client "Collect FRED" button | dashboard button | `system_commands` | dict | complete | `eval_worker.py:67` `run_fred_collection` | `completed`, `error` |
| `RUN_MARKET_COLLECTION` | trading-client "Collect Market Data" button | dashboard button | `system_commands` | dict | complete | `eval_worker.py:76` `run_market_collection` | `completed`, `error` |
| `EVALUATE_STRATEGY` | trading-client Strategy Score "Run Audit" button | dashboard button | `system_commands` | dict | complete | `eval_worker.py:85` `run_evaluate_strategy` | `completed`, `error` |
| `DISCARD_CHECKPOINT` / `RESUME_INTERRUPTED` (in `system_commands`) | **dead.** All 152 docs (134 + 18) date 2026-05-16 → 2026-06-24 — i.e. before the V3 cutover that created `v3_system_commands` (its earliest doc is 2026-06-24 04:24:41; the newest pre-cutover `system_commands` doc, a `START_V3_CYCLE` error, is timestamped 04:22:27 the same morning). `eval_worker.poll_system_commands`'s whitelist (`{'$in':['AUTORESEARCH','ACTIVATE_BRAIN_GRAPH','RUN_FRED_COLLECTION','RUN_MARKET_COLLECTION','EVALUATE_STRATEGY']}`) does **not** include either type — if one were written today it would sit `pending` forever, unclaimed, silently. | historical only | `system_commands` | dict | complete (historical) | none live | historical `completed`/`error` only |
| legacy `START_CYCLE`/`STOP_CYCLE`/`FORCE_RESET`/`FLASH_BRIEFING`/`START_V3_CYCLE` rows found **in `system_commands`** | dead pre-cutover rows from the same May–June 2026 window, from whatever poller drained `system_commands` before the V3 split | historical only | `system_commands` | JSON string (pre-cutover shape) | mixed | none live (superseded by the `v3_system_commands` split) | historical `completed`/`error` |

**16 inventory rows** (see `inventory_rows` in the returned JSON — counted as the
16 distinct table rows above, several of which cover more than one literal
`command_type` value where the code treats them identically).

---

## 2. Shared-helper compliance

`app/services/cycle_queue.py` is the sole documented writer for
`v3_system_commands`'s `START_CYCLE`/`REFRESH_SCHEDULE` from **trading-service**,
and every trading-service producer (`research_governor.py`, `watch_desk.py`,
`cycle_scheduler.py`, `scripts/canary_loop.py`) goes through it correctly —
verified by reading each call site.

Non-compliant producers found:

1. **`scripts/trigger_cycle.py:107`** and **`scripts/observe_cycle.py:81`**
   (trading-service, manual ops scripts) construct the `v3_system_commands`
   document locally instead of calling `enqueue_start_cycle`. They do,
   however, replicate the complete field set (`id`, `command_type`, `payload`,
   `status="pending"`, `progress=0`, `created_at`) — verified by reading both
   files in full. This is drift risk (two more places that must be kept in
   sync with the helper by hand), not a live defect: neither omits a required
   field.

2. **trading-client `app/routers/scheduler.py:140,174,184,200,209`** (five call
   sites: create/update/delete/delete-all/toggle schedule) write directly to
   `v3_system_commands` with **`status` and `created_at` omitted** —
   `mongo_store.insert_docs('v3_system_commands', [{'id': cmd_id,
   'command_type': "REFRESH_SCHEDULE", 'payload': json.dumps({"job_id":
   job_id})}])`, verified by reading the current file. This is the **exact
   defect class** the "past incident" (7 duplicated writer shapes,
   consolidated) was supposed to have closed — reproduced in a sibling file,
   in the *correct* collection this time (a 2026-08-28 commit,
   `5afa6b14`, moved these writes off the wrong collection `system_commands`
   onto the right one, `v3_system_commands`, but did not add the missing
   fields). trading-client's own `app/routers/pipeline.py:41`
   `_enqueue_command` already carries the fix (explicit `status`/`created_at`,
   with a docstring naming this exact incident and citing "fourteen [rows] ...
   in production, all START_V3_CYCLE, all from 2026-08-19") and a purpose-built
   `app/services/system_command_queue.py` shared helper exists for
   `system_commands` writers — but neither is imported by `scheduler.py`
   (confirmed: no `_enqueue_command`/`system_command_queue` import anywhere in
   that file). **Live Mongo data currently shows only one `REFRESH_SCHEDULE`
   document ever in `v3_system_commands`, and it is complete** (produced by
   `research_governor.py`'s use of `enqueue_refresh_schedule(..., prefix="cmd")`,
   not by `scheduler.py`) — i.e. this defect has not yet visibly struck
   production, only because the client's schedule-CRUD endpoints have not
   been exercised since the 08-28 deploy, not because the code is safe. The
   next schedule created/edited/deleted/toggled through the client UI will
   write an invisible, un-drained `REFRESH_SCHEDULE` command.

3. **`system_commands` "class" is closed on trading-service's own writer**:
   `pipeline_service.py`'s `enqueue_autoresearch()` (the sole AUTORESEARCH
   writer today) sets `status`/`created_at` correctly and uses a dict payload
   consistently with what `eval_worker.py` expects. No missing-field
   defect found on this path in the **current** state of the file (see the
   freshness caveat above — an earlier read mid-audit, before a same-day
   commit landed, showed an inline duplicate of this same write inside
   `_run_all_v3`'s "done" tail with a comment about "two writers [left by] the
   conversion"; that inline copy is gone in the current file, consolidated
   into the one `enqueue_autoresearch()` function called from all three
   tails).

---

## 3. Idempotency and races

**Atomic claim exists and is used correctly by both of trading-service's own
pollers:**
- `cycle_main.py:142` (`poll_system_commands`) and `cycle_main.py:107`
  (`drain_schedule_refreshes`) both claim via `find_one_and_update` (the
  primitive documented at `app/db/mongo_store.py:464`), re-asserting
  `status="pending"` in the filter — a second claimant racing the same
  document gets `None`, not a double claim.

**Non-atomic consumer found:**
- **`app/autoresearch/eval_worker.py:101` `poll_system_commands`** claims via
  `find_row` (a plain read, `mongo_query.py:169-178`, called at
  `eval_worker.py:105`) and then a *separate* `update_docs('system_commands',
  {'id': job_id}, {'$set': {'status':'running', ...}})` call
  (`eval_worker.py:110`) with no filter re-asserting `status='pending'` at the
  write. Two processes polling this collection concurrently could both read
  the same pending doc before either marks it `running`, and both would then
  proceed to execute the job. Today this poller runs as a single asyncio task
  inside the same process as `cycle_main.poll_system_commands`
  (`cycle_main.py:277-282`, both started from `run_worker` after
  `BootService.startup()` completes) — so there is no *intra*-process race —
  but nothing in the code prevents a second container/replica of the cycle
  backend from claiming the same row a second time; this is a latent
  defect that is exposed only under multi-replica or overlapping-deploy
  conditions, and would present exactly as the observed non-idempotent
  AUTORESEARCH duplication below.

**Double submit:**
- *`v3_system_commands` (`START_CYCLE`)*: a double click/proxy-retry produces
  two separate documents, both `pending`. `cycle_main`'s poller drains them
  serially (one `find_one_and_update` per loop iteration), so both get
  claimed and executed in sequence — but `PipelineService.start_cycle` itself
  refuses a second start while one is active (`pipeline_service.py:780-826`:
  checks the `pipeline_state` DB singleton and the in-memory `_cycle_task`,
  returns `{"status": "deduplicated", ...}`), which `cycle_main.py` maps to
  command status `skipped`. So double-submit for START is handled — but by an
  **in-process singleton guard**, not by the queue. It has the same
  multi-replica blind spot as above: two processes would each see their own
  `_cycle_task`/local process state as idle and both proceed.
- *`v3_system_commands` (`STOP_CYCLE`)*: `PipelineService.stop_cycle()`
  (`pipeline_service.py:3141-3164`) is idempotent by construction — if there
  is no active `_cycle_task`, it returns the current terminal status
  untouched rather than re-stopping. Minor cosmetic gap: that no-op result
  is *not* one of `("deduplicated","error","ignored")`, so `cycle_main.py`'s
  status-truth check (`cycle_main.py:213-221`) marks a redundant STOP
  `completed` rather than `skipped`, even though it did nothing the second
  time. Harmless, but slightly overstates what happened.
- *`system_commands` (`AUTORESEARCH`)*: **no dedup guard exists at all.**
  `enqueue_autoresearch()` always mints a fresh `job_id` and inserts
  unconditionally; nothing checks whether the same `cycle_id` already has a
  pending/completed AUTORESEARCH job. Live data (§4) shows this has actually
  happened repeatedly, most recently 2026-09-04 (4 duplicate jobs for
  `cycle-v3-1788486930`) — already established as not idempotent; this audit
  additionally found three earlier recurrences (2026-07-05 ×7,
  2026-07-20 ×2, 2026-08-20 ×2 — the last one is the exact
  `job_a000e299`/`cycle-v3-1787193855` incident documented inline in
  `eval_worker.py`'s own comments about the dict/string payload bug, followed
  by a second, manually-looking retry `job_c32b4563`). This is a
  long-running, still-open pattern, not a one-off.

**Worker restart mid-job — `boot_service.py:241-242`:**
```
mongo_store.update_docs('v3_system_commands', {'status': {'$in': ['running','pending']}}, {'$set': {'status': 'error', 'error_message': 'Container restarted unexpectedly'}})
mongo_store.update_docs('system_commands',   {'status': {'$in': ['running','pending']}}, {'$set': {'status': 'error', 'error_message': 'Container restarted unexpectedly'}})
```
This runs inside `BootService.startup()` (`boot_service.py:65`, stage "Reset
Application State"), which is `await`ed **before** either poller task is
created (`cycle_main.py:275-282`) — confirmed by reading `run_worker`'s
sequencing — so there is no boot-time race between this blanket reset and a
freshly (post-boot) claimed document. But the reset itself is a blunt
instrument:
- It touches **every** `pending`/`running` row regardless of age or type, not
  just ones plausibly orphaned by the crash. A command that was `pending`
  because it was queued seconds before the restart (e.g. a scheduler-cron
  `START_CYCLE`, or a `REFRESH_SCHEDULE` from an edit made moments earlier)
  is marked `error` and **never retried** — there is no re-enqueue logic
  anywhere in boot; the work is simply dropped and whoever queued it must
  notice the error and resubmit.
- A job that was `running` and had, in fact, **already finished its real work**
  microseconds before the crash (e.g. a cycle whose trades all executed and
  whose only remaining step was writing `status="completed"`) is
  retroactively relabelled `error` — "Container restarted unexpectedly" — even
  though the trading/analysis it represents actually happened. This is exactly
  the same class of problem `pipeline_service.py`'s own comments describe for
  `stop_cycle()` relabelling a completed cycle ("used to relabel a COMPLETED
  cycle as 'Cycle stopped by user' at boot") — the boot-time blanket reset has
  the identical failure mode for `pending`/`running` rows and is not guarded
  against it.

---

## 4. Live state (queried read-only, `trading_bot` on `PRISM_MONGO_URI`, 2026-09-06 ~16:42 UTC)

### `v3_system_commands` — 1037 documents total

By `command_type`: `START_CYCLE` 587, `START_V3_CYCLE` 364, `STOP_CYCLE` 64,
`FORCE_RESET` 12, `FLASH_BRIEFING` 8, `MORNING_BRIEFING` 1,
`REFRESH_SCHEDULE` 1.

By `status`: `completed` 902, `skipped` 82, `error` 53. **Zero** documents
missing `status` or `created_at`. **Zero** documents currently `pending` or
`running` (i.e. nothing stuck at query time).

Cycles with more than one command, by type (grouped by the `cycle_id`
embedded in the command's `result` field once claimed):
- `START_CYCLE`/`START_V3_CYCLE`: 770 distinct `cycle_id`s produced a result;
  **0** have more than one START command mapped to them.
- `REFRESH_SCHEDULE`: only 1 document exists total (see §2) — no duplication
  possible to observe.

### `system_commands` — 1366 documents total

By `command_type`: `AUTORESEARCH` 478, `START_CYCLE` 475, `STOP_CYCLE` 129,
`DISCARD_CHECKPOINT` 134, `ACTIVATE_BRAIN_GRAPH` 50, `REFRESH_SCHEDULE` 64,
`RESUME_INTERRUPTED` 18, `FORCE_RESET` 10, `FLASH_BRIEFING` 4,
`RUN_FRED_COLLECTION` 1, `RUN_MARKET_COLLECTION` 1, `EVALUATE_STRATEGY` 1,
`START_V3_CYCLE` 1.

By `status`: `completed` 1294, `error` 56, **missing (`None`) 16**.

**Missing `status`/`created_at`: 16 documents, all `command_type =
REFRESH_SCHEDULE`**, `_id` generation_time 2026-08-18 01:24 → 02:21 UTC
(already established; confirmed again here). All 64 `system_commands`
`REFRESH_SCHEDULE` docs date to a narrow historical window — 44 `completed`
from 2026-08-17, these 16 status-less from 2026-08-18, and 4 `error`
("Container restarted unexpectedly") from 2026-08-25 — and **none** since;
`REFRESH_SCHEDULE` writes moved to `v3_system_commands` on 2026-08-28
(trading-client commit `5afa6b14`), which is where the still-open version of
this defect now lives (§2).

The `START_CYCLE`/`STOP_CYCLE`/`FORCE_RESET`/`FLASH_BRIEFING`/`START_V3_CYCLE`
rows in `system_commands` are **all pre-cutover**: newest timestamps
2026-06-27, 2026-06-24, 2026-06-24, 2026-06-07, 2026-06-24 respectively — all
before or at the moment `v3_system_commands` came into existence (earliest
doc there: 2026-06-24 04:24:41; the last `system_commands` `START_V3_CYCLE`
is an `error` at 04:22:27 the same morning, two minutes earlier). Dead
history from the pre-split pipeline, not a live risk. Likewise
`DISCARD_CHECKPOINT`/`RESUME_INTERRUPTED` (152 docs) are all 2026-05-16 →
2026-06-24 and unreachable by `eval_worker.poll_system_commands`'s
whitelist today.

**Zero** documents currently `pending` or `running` at query time.

Cycles with more than one command, by type:
- `AUTORESEARCH` grouped by `payload.cycle_id`: 466 distinct `cycle_id`s, of
  which **4 have more than one AUTORESEARCH command**:
  - `cycle-1781945339` — **7** (2026-07-05, 4×`error`/3×`completed` — legacy
    pre-V3 cycle_id format)
  - `cycle-v3-1788486930` — 4 (2026-09-04, all `completed` — the
    already-established case)
  - `cycle-v3-1784554200` — 2 (2026-07-20, both `completed`; one carries id
    `skillopt-ar2-7ce8cbe6`, from a producer no longer present in the
    codebase — a retired `skill_optimizer` path)
  - `cycle-v3-1787193855` — 2 (2026-08-20: `job_a000e299` `error` then
    `job_c32b4563` `completed` — the exact incident documented inline in
    `eval_worker.py` about the dict/JSON-string payload mismatch)
- `REFRESH_SCHEDULE` grouped by `payload.job_id`: 48 distinct job ids, of
  which 4 have more than one command (`sch-default` ×12, `sch-bot-b8944f0b`
  ×4, `sch-2632195d` ×2, `sch-bot-344d7dbd` ×2) — all dated on/before
  2026-08-25, i.e. from the pre-08-28 `system_commands` era. Unlike
  AUTORESEARCH duplication, a repeated REFRESH_SCHEDULE is close to harmless
  (it just reloads/re-registers scheduler jobs — `SchedulerService.refresh_job`
  is idempotent by nature), so this is noted but not flagged as a defect.

---

## 5. Operational queries (text — do not run against write concerns; all read-only)

```python
# --- Stuck pending/running commands (either collection) ---
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
for coll in ("v3_system_commands", "system_commands"):
    cur = db[coll].find(
        {"status": {"$in": ["pending", "running"]}},
        {"id": 1, "command_type": 1, "status": 1, "created_at": 1,
         "started_at": 1, "worker_id": 1},
    ).sort("created_at", 1)
    for d in cur:
        print(coll, d["id"], d["command_type"], d["status"],
              d.get("created_at"), d.get("started_at"), d.get("worker_id"))

# --- Documents invisible to their own poller (missing status/created_at) ---
for coll in ("v3_system_commands", "system_commands"):
    cur = db[coll].find(
        {"$or": [{"status": {"$exists": False}}, {"created_at": {"$exists": False}}]},
        {"id": 1, "command_type": 1, "payload": 1},
    )
    for d in cur:
        gen_time = d["_id"].generation_time  # ObjectId timestamp = true age
        print(coll, d["id"], d["command_type"], "age(from _id)=", gen_time)

# --- Duplicate commands per cycle (AUTORESEARCH, system_commands) ---
db.system_commands.aggregate([
    {"$match": {"command_type": "AUTORESEARCH"}},
    {"$addFields": {"_payload": {"$cond": [
        {"$eq": [{"$type": "$payload"}, "string"]},
        {"$function": {"body": "function(s){try{return JSON.parse(s)}catch(e){return {}}}",
                        "args": ["$payload"], "lang": "js"}},
        "$payload"]}}},
    {"$group": {"_id": "$_payload.cycle_id", "n": {"$sum": 1},
                "ids": {"$push": "$id"}, "statuses": {"$push": "$status"}}},
    {"$match": {"n": {"$gt": 1}}},
    {"$sort": {"n": -1}},
])

# --- Duplicate REFRESH_SCHEDULE per job_id (v3_system_commands or system_commands) ---
db.v3_system_commands.aggregate([
    {"$match": {"command_type": "REFRESH_SCHEDULE"}},
    {"$addFields": {"_job": {"$function": {
        "body": "function(s){try{return JSON.parse(s).job_id}catch(e){return null}}",
        "args": ["$payload"], "lang": "js"}}}},
    {"$group": {"_id": "$_job", "n": {"$sum": 1}, "ids": {"$push": "$id"}}},
    {"$match": {"n": {"$gt": 1}}},
])

# --- Age of anything stuck, precomputed, sorted worst-first ---
db.v3_system_commands.aggregate([
    {"$match": {"status": {"$in": ["pending", "running"]}}},
    {"$addFields": {"age_seconds": {"$divide": [
        {"$subtract": [now, {"$ifNull": ["$created_at", "$$NOW"]}]}, 1000]}}},
    {"$sort": {"age_seconds": -1}},
    {"$project": {"id": 1, "command_type": 1, "status": 1, "age_seconds": 1, "worker_id": 1}},
])

# --- Per command_type, commands-per-cycle histogram (generalizes the AUTORESEARCH check) ---
# Run once per collection/command_type of interest, adjusting the payload-key
# used as the "cycle" grouping key (cycle_id for AUTORESEARCH/START_CYCLE
# results, job_id for REFRESH_SCHEDULE).
```

Fields to display for a human operator triaging a stuck/duplicate row:
`id`, `command_type`, `status`, `created_at` (or `_id.generation_time` if
`created_at` is absent — that absence is itself the finding), `started_at`,
`completed_at`, `worker_id`, `error_message`, and the parsed `payload`
(cycle_id / job_id / tickers) so the operator can tell which cycle or
schedule a stuck row belongs to.

---

## Summary: is the "missing status/created_at" class closed?

**No.** It is closed for the two producers that were patched
(`app/services/cycle_queue.py` in trading-service, and trading-client's
`app/routers/pipeline.py:_enqueue_command`) — both now used correctly by
every trading-service producer and by the trading-client dashboard's
start/stop/reset/briefing/checkpoint buttons. But it recurred, unpatched, in
a **fifth** producer that was moved to the correct queue on the same date
(2026-08-28) the class was supposedly fixed: trading-client's
`app/routers/scheduler.py` schedule-CRUD endpoints write `REFRESH_SCHEDULE`
into `v3_system_commands` today with `status` and `created_at` still
omitted. It has not yet visibly struck production only because those
endpoints have not been exercised since the deploy — the one live
`REFRESH_SCHEDULE` row in `v3_system_commands` came from a different producer
(`research_governor.py`). Separately, the **non-idempotent-enqueue** class
(no producer-side or consumer-side dedup for `AUTORESEARCH` per cycle) is
long-running and still open — this audit found four separate historical
occurrences spanning 2026-07-05 through 2026-09-04, not a single incident.
