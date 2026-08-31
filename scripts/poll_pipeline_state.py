#!/usr/bin/env python3
"""Block until the live pipeline state moves off a baseline, then exit.

Reads MONGO.

WHAT THIS ANSWERS, AND WHY IT IS NOT check_pipeline_state.py
------------------------------------------------------------
`scripts/check_pipeline_state.py` prints the singleton once and always exits 0.
It answers "what is the state right now?".

This script answers a different question — "has the state moved yet?" — and it
answers it with an EXIT CODE, so a shell or an agent can wait on a cycle
advancing instead of sleeping a guessed number of seconds and re-reading:

    0  STATE_CHANGED   cycle_id, phase or status differs from the baseline
    2  POLL_TIMEOUT    the window elapsed with the state still on the baseline
    3  POLL_UNREADABLE every read raised; the store was never actually read

THE COMPARE IS THREE-WAY, AND THAT IS THE CONTRACT
--------------------------------------------------
The pre-port loop fired on

    curr_cycle_id != last_cycle_id or curr_phase != last_phase
                                   or curr_status != last_status

and the commonest real use moves exactly ONE of the three — "block until this
cycle leaves `collecting`" keeps `cycle_id` and usually `status` fixed. A watch
narrowed to `cycle_id` alone would let a phase change past in silence, burn the
whole window and exit 2, and the caller would blame the pipeline for standing
still. So each of the three fields is exercised ALONE in
tests/unit/test_poll_pipeline_state.py: dropping any one of them from the
compare turns that file red.

WHAT CHANGED IN THE INTERFACE, AND WHAT DID NOT
-----------------------------------------------
0 and 2 keep their meaning, their trigger and their message text, byte for
byte. 3 is NEW, and since the exit codes are this script's only interface that
is a WIDENING, not a preservation — recorded here rather than quietly done. It
exists because the two answers it separates are not the same answer: a poll
that never reached the store has NOT observed "no change", and reporting one as
the other is how a fault comes to look like a quiet pipeline. The property that
matters is preserved in both directions — an unreadable store never returns 0,
and a store that answered (including one holding no document at all) never
returns 3. That is the same split `app/services/pipeline_state.py` draws
between `idle` (a real answer) and `unknown` (a read that failed); see
tests/unit/test_pipeline_state_mongo_read.py. Nothing in this repo invokes this
script or greps its codes (`grep -rn poll_pipeline_state` finds only the script,
its test and the migration inventory), so the widening breaks no caller today.

WHY THE STORE CHANGED
---------------------
Until this port the loop ran

    SELECT cycle_id, phase, status FROM pipeline_state WHERE singleton_id='current'

against the SQL archive, where `pipeline_state` stopped at the 2026-08-19
cutover. Re-measured from this tree on 2026-08-30, the singleton in each store:

    archive  cycle-v3-1787179210  analyzing  done   updated_at 2026-08-19 22:55:05.087554
    mongo    cycle-v3-1788074145  analyzing  done   updated_at 2026-08-30 07:21:56.598000

Microseconds against milliseconds is the provenance: the first row can only
have come out of the archive, the second only out of Mongo.

The archive is FROZEN, not unreachable, and the difference matters because an
earlier draft of this file asserted the opposite. Running the pre-port version
unchanged against the live archive on 2026-08-30 printed

    STATE_CHANGED: cycle_id='cycle-v3-1787179210', phase='analyzing', status='done'

and exited 0 in about a second. That is the whole failure, and it is worse than
an error would have been: a row that cannot change is a watch that can only
lie. Given a live baseline it returns STATE_CHANGED at once naming an
eleven-day-old cycle; given the archive's own values it sits out the full
window and exits 2 however the pipeline actually moved. (The retained archive
pool did fail to open a connection for two days; that was fixed in 94c6602, an
ancestor of this commit, so any claim that this script "cannot connect" is
false as of 2026-08-30 and has been deleted rather than left for the next
reader to trust.)

Mongo has no column types, so the three values are normalised through `_text()`
rather than `(v or "").strip()`: a non-string `cycle_id` would have raised
inside the try, been reported as a poll error, and the transition would have
been missed for the whole window. The `.strip()` is load-bearing on BOTH sides
— the pre-port code stripped the baseline flags and the row values — so a
baseline passed as `' analyzing '` must still compare equal.

ONE KNOWN ENVIRONMENT GAP
-------------------------
The read is `find_row`, i.e. limit 1 in natural order with no sort, which is
exactly `fetchone()` on the original statement (no ORDER BY, no LIMIT). It is
exact only while `singleton_id='current'` selects one document. In the archive
`singleton_id` was the PRIMARY KEY; in Mongo the `natural_key` index on it is
NOT unique (verified 2026-08-30: `{'singleton_id': 1}`, background, no
`unique`). Today `count_docs('pipeline_state') == 1` so the read is exact, but
if a second `current` document were ever written this would return the OLDEST.
Adding a sort would break strict equivalence with the SQL, so it is reported
rather than patched here — the fix belongs on the index.

Usage:
    python scripts/poll_pipeline_state.py --cycle-id cycle-123 --phase collecting --status started
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Optional

# `python scripts/poll_pipeline_state.py` puts `scripts/` on sys.path[0], not
# the repo root, so `from app.db import ...` needs the root added first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query  # noqa: E402

#: The SQL TABLE NAME, spelled out as a literal on purpose. `mongo_query`
#: resolves a table to a collection exactly once, internally; binding
#: `collection_for("pipeline_state")` to this constant would resolve it twice
#: and, the day renames are switched on, read a collection that does not exist.
#: A double resolution hidden behind a module constant is invisible to the
#: repo-wide scanner in tests/unit/test_no_double_collection_resolution.py,
#: which matches the resolver only as a DIRECT call argument — so this file's
#: own test pins it instead, by re-importing the module with a renaming
#: `collection_for` in place and requiring TABLE to be unchanged.
TABLE = "pipeline_state"
QUERY = {"singleton_id": "current"}
FIELDS = ("cycle_id", "phase", "status")

POLL_INTERVAL_SECONDS = 10.0

EXIT_CHANGED = 0
EXIT_TIMEOUT = 2
EXIT_UNREADABLE = 3


def _text(value: Any) -> str:
    """One state field as the comparable string the baseline flags carry.

    `None` (a NULL column, or a field absent from a post-cutover document) and
    a missing value both flatten to "", which is what `(v or "").strip()` did
    on the archive's rows. Anything that is not a string is stringified instead
    of raising: the archive guaranteed TEXT here and Mongo guarantees nothing.

    The `.strip()` is not cosmetic — it is applied to the baseline flags too,
    so `--phase ' analyzing '` and a stored `'analyzing'` are the same state,
    exactly as before the port.
    """
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def read_state() -> Optional[tuple[str, str, str]]:
    """The live (cycle_id, phase, status), or None when no document exists.

    None is `cursor.fetchone()` returning nothing — the pipeline has never
    written state. It is an ABSENCE, not a failure: the caller keeps polling,
    exactly as the SQL version's `if row:` did.
    """
    row = mongo_query.find_row(TABLE, dict(QUERY), list(FIELDS))
    if row is None:
        return None
    return (_text(row[0]), _text(row[1]), _text(row[2]))


def poll_state(last_cycle_id: str, last_phase: str, last_status: str,
               timeout_seconds: int = 900,
               interval: float = POLL_INTERVAL_SECONDS) -> int:
    baseline = (_text(last_cycle_id), _text(last_phase), _text(last_status))

    print(f"Polling Mongo ({TABLE}, singleton_id='current') for a state change "
          f"from baseline: cycle_id='{baseline[0]}', phase='{baseline[1]}', "
          f"status='{baseline[2]}'")
    sys.stdout.flush()

    start_time = time.monotonic()
    reads = 0            # attempts that actually got an answer out of the store
    failures = 0
    last_error: Optional[Exception] = None

    while time.monotonic() - start_time < timeout_seconds:
        try:
            state = read_state()
        except Exception as e:  # noqa: BLE001 - a blip must not end the watch
            failures += 1
            last_error = e
            print(f"Error polling database: {e}", file=sys.stderr)
            sys.stderr.flush()
        else:
            reads += 1
            # All THREE fields, as the SQL did. See "THE COMPARE IS THREE-WAY".
            if state is not None and state != baseline:
                print(f"\nSTATE_CHANGED: cycle_id='{state[0]}', "
                      f"phase='{state[1]}', status='{state[2]}'")
                sys.stdout.flush()
                return EXIT_CHANGED

        # Capped at the time left, so `--timeout 12` returns at 12s rather than
        # overshooting to the end of a full 10s sleep.
        remaining = timeout_seconds - (time.monotonic() - start_time)
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    # `reads == 0` and not `failures` alone: one blip in an otherwise readable
    # window IS an observation of "no change", and must stay a 2. Only a window
    # in which nothing was ever read says nothing about the pipeline.
    if reads == 0 and failures:
        print(f"\nPOLL_UNREADABLE: {failures} read(s) attempted, none succeeded — "
              f"the store was never read, so this run says nothing about the "
              f"pipeline. Last error: {last_error}")
        sys.stdout.flush()
        return EXIT_UNREADABLE

    print(f"\nPOLL_TIMEOUT: No state change detected within {timeout_seconds} seconds.")
    sys.stdout.flush()
    return EXIT_TIMEOUT


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Block until the live Mongo pipeline state leaves a baseline")
    parser.add_argument("--cycle-id", default="", help="Baseline cycle ID")
    parser.add_argument("--phase", default="", help="Baseline phase")
    parser.add_argument("--status", default="", help="Baseline status")
    parser.add_argument("--timeout", type=int, default=900, help="Timeout in seconds")
    args = parser.parse_args()

    return poll_state(args.cycle_id, args.phase, args.status, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
