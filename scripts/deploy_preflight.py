"""Last-moment live-cycle gate for the deploy chain (open item 45).

The pre-deploy check at command time proves the desk was idle when the deploy
STARTED; the build takes ~150s and the scheduler is free to start a cycle in
that window — that race killed cycle-v3-1786424970 on 2026-08-11. This script
runs from EXTRA_SSH_SYNC in deploy.sh, which deploy-kit/lib.sh invokes AFTER
the image build and immediately BEFORE the image transfer + container swap
(lib.sh:744 vs :792), under `set -e` — a non-zero exit here aborts the deploy
before the swap.

Residual window, stated honestly: the image transfer + `compose down/up`
(~tens of seconds) still runs unchecked after this gate. Closing that fully
needs a scheduler quiesce, which lives in the service, not the deploy chain.

Exit codes:
  0 — pipeline idle; deploy may proceed.
  1 — a cycle is LIVE, or the state is UNKNOWABLE (DB unreachable after
      retries). Unknown fails CLOSED: deploying into an unknowable state is
      the 2026-08-11 incident with less evidence, not more.

Escape hatch: DEPLOY_SKIP_CYCLE_CHECK=1 skips the gate (prints that it did).
"""

from __future__ import annotations

import os
import sys
import time

# Same vocabulary as sun/.claude/hooks/guard_deploy.py — the command-time
# guard this gate backstops. "running"/"pending"/"starting" etc. are live.
IDLE_STATUSES = {"idle", "done", "error", "stopped", "interrupted"}

CONNECT_TIMEOUT_S = 8
# The NAS Postgres refuses forks in bursts; a burst is not a verdict. Retries
# spread over ~1 min before we declare the state unknowable.
ATTEMPTS = 6
RETRY_SLEEP_S = 10


def main() -> int:
    if os.environ.get("DEPLOY_SKIP_CYCLE_CHECK") == "1":
        print("[deploy_preflight] DEPLOY_SKIP_CYCLE_CHECK=1 — live-cycle gate SKIPPED by operator")
        return 0

    import psycopg
    from dotenv import load_dotenv

    # override=True: deploy.sh has already `set -a`-sourced deploy-kit/.env.deploy,
    # whose DATABASE_URL is treesearch's `postgresql+asyncpg://` form — psycopg
    # cannot parse it and the gate aborts every deploy. This service's own .env
    # must win for its own preflight.
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[deploy_preflight] DATABASE_URL not set — state UNKNOWABLE, failing closed")
        return 1

    last_err: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_S) as conn:
                row = conn.execute(
                    "SELECT status, phase, cycle_id FROM pipeline_state"
                    " WHERE singleton_id='current'"
                ).fetchone()
            break
        except psycopg.OperationalError as e:
            last_err = e
            if attempt < ATTEMPTS:
                print(f"[deploy_preflight] DB attempt {attempt}/{ATTEMPTS} failed; retrying...")
                time.sleep(RETRY_SLEEP_S)
    else:
        print(f"[deploy_preflight] pipeline_state UNKNOWABLE after {ATTEMPTS} attempts ({last_err})")
        print("[deploy_preflight] failing CLOSED — a deploy into unknown cycle state is the 08-11 incident")
        print("[deploy_preflight] override deliberately with DEPLOY_SKIP_CYCLE_CHECK=1")
        return 1

    if row is None:
        # No singleton row: nothing can be mid-flight in a pipeline that has
        # never written state. Idle.
        print("[deploy_preflight] no pipeline_state row — treating as idle")
        return 0

    status, phase, cycle_id = row
    if (status or "").lower() in IDLE_STATUSES:
        print(f"[deploy_preflight] pipeline idle (status={status}) — deploy may proceed")
        return 0

    print(
        f"[deploy_preflight] BLOCKED — cycle {cycle_id} is {status} (phase: {phase}).\n"
        "  A swap now SIGTERMs the container and the cycle persists as 'stopped'\n"
        "  with its unfinished tickers dropped terminally. Wait for idle/done."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
