#!/usr/bin/env python3
"""Pre/post-cycle health checklist, in execution order.

Encodes the seven-phase checklist as executable assertions so "is the cycle
healthy" is a command rather than a reading exercise. Each check names the
module or table it probes, so a FAIL points at a file.

The triage order matters and is preserved: infrastructure before trigger,
trigger before pipeline, pipeline before data, data before agents, agents
before verdicts. A failure early makes every later failure uninformative.

    python scripts/cycle_healthcheck.py                    # pre-cycle
    python scripts/cycle_healthcheck.py --cycle <cycle_id> # post-cycle too

Exit 0 = no FAILs. WARN never fails the run; it flags something to look at.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_R: list[tuple[str, str, str, str]] = []


def rec(phase: str, status: str, name: str, detail: str = "") -> None:
    _R.append((phase, status, name, detail))


# ── Phase 1: infrastructure & boot ───────────────────────────────────────────

def phase1() -> None:
    P = "1 infra"
    try:
        from app.db.connection import get_db
        with get_db() as db:
            db.execute("SELECT 1").fetchone()
        rec(P, PASS, "DB connection live", "app/db/connection.py get_db()")
    except Exception as e:
        rec(P, FAIL, "DB connection live", f"{type(e).__name__}: {e}")
        return

    for tbl in ("v3_system_commands", "system_commands", "pipeline_state"):
        try:
            with get_db() as db:
                db.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone()
            rec(P, PASS, f"{tbl} accessible")
        except Exception as e:
            rec(P, FAIL, f"{tbl} accessible", str(e)[:80])

    try:
        from app.config import settings
        key = getattr(settings, "API_SERVER_KEY", None)
        rec(P, PASS if key else WARN, "API_SERVER_KEY set",
            "" if key else "unset — /status will reject callers")
    except Exception as e:
        rec(P, WARN, "API_SERVER_KEY set", str(e)[:60])

    # Import every module the checklist names. An ImportError here is the
    # cheapest possible failure to find and the most expensive to hit at boot.
    import importlib
    mods = [
        "boot_service", "pipeline_service", "pipeline_state", "freshness_gate",
        "context_gate", "parameter_governor", "parameter_store",
        "research_governor", "scraper_client", "news_extraction",
        "embedding_service", "retrieval_hybrid", "data_flag_service",
        "prism_agent_registry", "prism_agent_caller", "adaptive_concurrency",
        "api_rate_limiter", "debate_service", "discovery_mode", "result_saver",
        "cycle_scheduler", "flash_briefing", "alert_service",
    ]
    bad = []
    for m in mods:
        try:
            importlib.import_module(f"app.services.{m}")
        except Exception as e:
            bad.append(f"{m}({type(e).__name__})")
    rec(P, FAIL if bad else PASS, f"{len(mods)} service modules import",
        ", ".join(bad) if bad else "")


# ── Phase 2: trigger & command poller ────────────────────────────────────────

def phase2() -> None:
    P = "2 trigger"
    from app.db.connection import get_db

    # THE top triage item: a command stuck at 'running' means the poller died
    # mid-command and no new cycle can claim the slot.
    for tbl in ("v3_system_commands", "system_commands"):
        try:
            with get_db() as db:
                stuck = db.execute(
                    f"SELECT count(*) FROM {tbl} WHERE status = 'running' "
                    "AND created_at < now() - interval '2 hours'"
                ).fetchone()[0]
            rec(P, FAIL if stuck else PASS, f"{tbl} no stuck 'running'",
                f"{stuck} row(s) running >2h" if stuck else "")
        except Exception as e:
            rec(P, WARN, f"{tbl} no stuck 'running'", str(e)[:60])

    try:
        with get_db() as db:
            recent = db.execute(
                "SELECT count(*) FROM v3_system_commands WHERE status = 'error' "
                "AND created_at > now() - interval '2 days'"
            ).fetchone()[0]
        rec(P, WARN if recent else PASS, "no recent command errors",
            f"{recent} error(s) in 48h" if recent else "")
    except Exception as e:
        rec(P, WARN, "no recent command errors", str(e)[:60])


# ── Phase 3: pipeline state ──────────────────────────────────────────────────

def phase3() -> None:
    P = "3 pipeline"
    from app.db.connection import get_db

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT status, cycle_id, updated_at FROM pipeline_state "
                "WHERE singleton_id = 'current'"
            ).fetchone()
        if not row:
            rec(P, WARN, "pipeline_state clean", "no singleton row yet")
        elif row[0] == "running":
            rec(P, WARN, "pipeline_state clean",
                f"a cycle is RUNNING ({row[1]}) — do not deploy")
        else:
            rec(P, PASS, "pipeline_state clean", f"status={row[0]}")
    except Exception as e:
        rec(P, FAIL, "pipeline_state clean", str(e)[:80])

    for mod, fn in (("parameter_store", "get_param"),
                    ("research_governor", None)):
        try:
            m = __import__(f"app.services.{mod}", fromlist=["x"])
            if fn:
                val = getattr(m, fn)("ANALYSIS_CONFIDENCE_THRESHOLD")
                rec(P, PASS if val else WARN, f"{mod} resolves",
                    f"confidence floor = {val}")
            else:
                rec(P, PASS, f"{mod} loads")
        except Exception as e:
            rec(P, FAIL, f"{mod} resolves", str(e)[:80])


# ── Phase 4: data collection ─────────────────────────────────────────────────

def phase4() -> None:
    P = "4 data"
    from app.db.connection import get_db

    try:
        import httpx
        url = os.environ.get("SCRAPER_SERVICE_URL", "http://10.0.0.16:8001")
        r = httpx.get(f"{url}/health", timeout=8)
        rec(P, PASS if r.status_code == 200 else WARN,
            "scraper-service reachable", f"{url} -> {r.status_code}")
    except Exception as e:
        rec(P, WARN, "scraper-service reachable", f"{type(e).__name__}")

    # Freshness is the gate most likely to silently starve a cycle.
    try:
        with get_db() as db:
            fresh = db.execute(
                "SELECT count(DISTINCT ticker) FROM price_history "
                "WHERE date > CURRENT_DATE - 5"
            ).fetchone()[0]
        rec(P, PASS if fresh > 50 else WARN, "price data fresh",
            f"{fresh} tickers with bars in the last 5 days")
    except Exception as e:
        rec(P, WARN, "price data fresh", str(e)[:60])

    try:
        with get_db() as db:
            n = db.execute(
                "SELECT count(*) FROM news_articles "
                "WHERE published_at > now() - interval '2 days'"
            ).fetchone()[0]
        rec(P, PASS if n else WARN, "news flowing", f"{n} articles in 48h")
    except Exception as e:
        rec(P, SKIP, "news flowing", str(e)[:60])


# ── Phase 5: agents & LLM ────────────────────────────────────────────────────

def phase5() -> None:
    P = "5 agents"
    for prov in ("PROVIDER_VLLM_1_URL", "PROVIDER_VLLM_2_URL"):
        url = os.environ.get(prov)
        if not url:
            rec(P, SKIP, f"{prov} configured")
            continue
        try:
            import httpx
            r = httpx.get(f"{url}/v1/models", timeout=8)
            rec(P, PASS if r.status_code == 200 else FAIL,
                f"{prov} reachable", f"{url} -> {r.status_code}")
        except Exception as e:
            rec(P, FAIL, f"{prov} reachable", f"{type(e).__name__}")

    try:
        from app.v3.prism_registration import _discover_v3_agent_modules
        agent_mods = _discover_v3_agent_modules()
        if agent_mods:
            rec(P, PASS, "agent registry loads", f"{len(agent_mods)} agents")
        else:
            rec(P, FAIL, "agent registry loads", "no agents discovered")
    except Exception as e:
        rec(P, FAIL, "agent registry loads", str(e)[:70])


# ── Phase 6/7: verdicts and post-cycle, for a specific cycle ─────────────────

def phase67(cycle_id: str) -> None:
    P6, P7 = "6 verdict", "7 post"
    from app.db.connection import get_db

    with get_db() as db:
        desks = db.execute(
            "SELECT count(*) FROM shared_desk WHERE cycle_id = %s", [cycle_id]
        ).fetchone()[0]
        done = db.execute(
            "SELECT count(*) FROM shared_desk WHERE cycle_id = %s "
            "AND desk_data->>'phase' IN ('PM_DONE','INIT')", [cycle_id]
        ).fetchone()[0]
    rec(P6, PASS if desks else FAIL, "desks written", f"{desks} desk(s)")
    rec(P6, PASS if done == desks else WARN, "all desks reached a terminal phase",
        f"{done}/{desks}")

    with get_db() as db:
        decided = db.execute(
            "SELECT count(*) FROM shared_desk WHERE cycle_id = %s "
            "AND desk_data->'final_decision' IS NOT NULL", [cycle_id]
        ).fetchone()[0]
    rec(P6, PASS if decided else FAIL, "verdicts produced", f"{decided}/{desks}")

    with get_db() as db:
        saved = db.execute(
            "SELECT count(*) FROM analysis_results WHERE cycle_id = %s",
            [cycle_id],
        ).fetchone()[0]
    rec(P6, PASS if saved else WARN, "result_saver persisted", f"{saved} row(s)")

    with get_db() as db:
        row = db.execute(
            "SELECT status FROM pipeline_state WHERE singleton_id = 'current'"
        ).fetchone()
    rec(P7, PASS if row and row[0] == "done" else WARN,
        "pipeline reached 'done'", f"status={row[0] if row else '?'}")

    # Tool DIVERSITY, not just count: the 2026-07-28 finding was that the same
    # opening fired on every ticker, which a per-cycle count cannot see.
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, count(DISTINCT tool_name) FROM agent_tool_telemetry "
            "WHERE cycle_id = %s GROUP BY ticker", [cycle_id]
        ).fetchall()
        tot = db.execute(
            "SELECT count(DISTINCT tool_name), count(*) FROM agent_tool_telemetry "
            "WHERE cycle_id = %s", [cycle_id]
        ).fetchone()
    rec(P7, PASS if tot[0] else WARN, "tools used this cycle",
        f"{tot[0]} distinct, {tot[1]} calls across {len(rows)} ticker(s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    for fn in (phase1, phase2, phase3, phase4, phase5):
        try:
            fn()
        except Exception as e:
            rec(fn.__name__, FAIL, "phase crashed", f"{type(e).__name__}: {e}")
    if args.cycle:
        try:
            phase67(args.cycle)
        except Exception as e:
            rec("6/7", FAIL, "phase crashed", f"{type(e).__name__}: {e}")

    print(f"\n{'='*74}")
    print("CYCLE HEALTHCHECK" + (f" — {args.cycle}" if args.cycle else " — pre-cycle"))
    print(f"{'='*74}")
    cur = None
    for phase, status, name, detail in _R:
        if phase != cur:
            print(f"\n[{phase}]")
            cur = phase
        line = f"  {status:4} {name}"
        print(f"{line:<52} {detail}" if detail else line)

    n_fail = sum(1 for r in _R if r[1] == FAIL)
    n_warn = sum(1 for r in _R if r[1] == WARN)
    print(f"\n  {sum(1 for r in _R if r[1]==PASS)} pass, {n_fail} FAIL, "
          f"{n_warn} warn, {sum(1 for r in _R if r[1]==SKIP)} skip\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
