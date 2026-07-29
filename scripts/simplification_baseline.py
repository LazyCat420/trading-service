#!/usr/bin/env python3
"""Stage-0 baseline for the simplification wave (2026-07-29).

The system currently places ZERO trades (44 decisions, 0 BUYs on 07-28/29), so
P&L feedback is unavailable for weeks. Every simplification stage is therefore
judged on NON-P&L invariants captured here, and the bar is:

    the policy_action and Board-confidence distributions must be
    indistinguishable before/after, EXCEPT where a stage predicted a change
    and named its direction in advance.

Run before a stage and after it, then diff the two JSON files:

    python scripts/simplification_baseline.py --label pre-stage1
    # ... make the change, run a cycle ...
    python scripts/simplification_baseline.py --label post-stage1
    python scripts/simplification_baseline.py --diff pre-stage1 post-stage1

Structural counts (LOC, orchestrator branches, duplicate modules) are captured
alongside the behavioural ones because the whole point of the wave is to move
them, and a stage that moves LOC without moving behaviour is exactly what we
want to be able to prove.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".simplification_baselines"
MAIN_CHECKOUT = Path("/home/lazycat/github/projects/sun/trading-service")
CLIENT_CHECKOUT = Path("/home/lazycat/github/projects/sun/trading-client")

# The window that is fully instrumented: policy_action landed 07-23,
# decision_provenance 07-25. Before that the classifier would be labelling
# rows that carry no labels — see docs/BOARD_HOLD_DECOMPOSITION_2026-07-29.md.
INSTRUMENTED_FROM = "2026-07-26"


def _db():
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    # A git worktree has no .env of its own — it lives in the main checkout.
    for env in (ROOT / ".env", MAIN_CHECKOUT / ".env"):
        if env.exists():
            load_dotenv(env)
            break
    import psycopg2

    url = os.environ["DATABASE_URL"].strip().strip('"')
    return psycopg2.connect(url, connect_timeout=10)


# ── behavioural invariants ───────────────────────────────────────────────────


def behavioural(cur) -> dict:
    out: dict = {"window_from": INSTRUMENTED_FROM}

    cur.execute(
        "SELECT action, count(*) FROM trade_results "
        "WHERE created_at >= %s GROUP BY 1", [INSTRUMENTED_FROM])
    out["action_distribution"] = {str(a): n for a, n in cur.fetchall()}

    cur.execute(
        "SELECT policy_action, count(*) FROM trade_results "
        "WHERE created_at >= %s GROUP BY 1", [INSTRUMENTED_FROM])
    out["policy_action_distribution"] = {str(a): n for a, n in cur.fetchall()}

    cur.execute(
        "SELECT decision_provenance, count(*) FROM trade_results "
        "WHERE created_at >= %s GROUP BY 1", [INSTRUMENTED_FROM])
    out["provenance_distribution"] = {str(a): n for a, n in cur.fetchall()}

    # Board confidence histogram in 5-point bands. This is THE number the
    # simplification is predicted to move (removing evidence lowers it), so it
    # is captured as a distribution rather than a mean.
    cur.execute(
        """SELECT (floor(((sd.desk_data->'final_decision'->>'confidence')::float)/5)*5)::int band,
                  count(*)
           FROM shared_desk sd
           WHERE sd.created_at >= %s
             AND sd.desk_data->'final_decision'->>'confidence' IS NOT NULL
           GROUP BY 1 ORDER BY 1""", [INSTRUMENTED_FROM])
    out["board_confidence_bands"] = {str(b): n for b, n in cur.fetchall()}

    cur.execute(
        """SELECT count(*) FILTER (WHERE (sd.desk_data->'final_decision'->>'confidence')::float >= 70),
                  count(*)
           FROM shared_desk sd
           WHERE sd.created_at >= %s
             AND sd.desk_data->'final_decision'->>'confidence' IS NOT NULL""",
        [INSTRUMENTED_FROM])
    ge70, tot = cur.fetchone()
    out["board_confidence_at_or_above_floor"] = {"n_ge_70": ge70, "n_total": tot}

    # Artifact presence. Control 2 of the HOLD decomposition: if presence FALLS
    # while confidence falls, an input was removed (regression). If presence
    # holds or rises, the confidence drop is honest.
    cur.execute(
        "SELECT desk_data FROM shared_desk WHERE created_at >= %s", [INSTRUMENTED_FROM])
    counts: Counter = Counter()
    desks = 0
    for (d,) in cur.fetchall():
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                continue
        if not isinstance(d, dict):
            continue
        desks += 1
        for k, v in d.items():
            if isinstance(v, dict) and v:
                counts[k] += 1
    out["desks_sampled"] = desks
    out["artifact_presence_pct"] = {
        k: round(100.0 * n / desks, 1) for k, n in sorted(counts.items())
    } if desks else {}

    # Cost. Stage 3 predicts ~30% off both of these.
    cur.execute(
        """SELECT agent_name, count(*), round(avg(elapsed_ms)/1000.0, 1)
           FROM v3_agent_telemetry WHERE created_at >= %s
           GROUP BY 1 ORDER BY 3 DESC NULLS LAST""", [INSTRUMENTED_FROM])
    rows = cur.fetchall()
    out["agent_seconds"] = {str(a): {"n": n, "avg_s": float(s or 0)} for a, n, s in rows}
    out["sum_avg_seconds_per_ticker"] = round(sum(float(s or 0) for _, _, s in rows), 1)

    cur.execute(
        "SELECT count(*) FROM v3_guardrail_firings WHERE created_at >= %s",
        [INSTRUMENTED_FROM])
    out["guardrail_firings_total"] = cur.fetchone()[0]
    return out


# ── structural counts ────────────────────────────────────────────────────────


def _sh(cmd: str, cwd: Path = ROOT) -> str:
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          text=True).stdout.strip()


def structural() -> dict:
    out: dict = {}
    py = "find app -name '*.py' -not -path '*__pycache__*'"
    out["app_py_files"] = int(_sh(f"{py} | wc -l") or 0)
    out["app_loc"] = int((_sh(f"cat $({py}) | wc -l") or "0"))

    orch = ROOT / "app/v3/orchestrator.py"
    if orch.exists():
        text = orch.read_text(encoding="utf-8")
        out["orchestrator_loc"] = len(text.splitlines())
        out["orchestrator_if"] = sum(
            1 for ln in text.splitlines() if ln.strip().startswith("if "))
        out["orchestrator_try"] = text.count("try:")
    for rel in ("app/cognition/debate/debate_coordinator.py",
                "app/cognition/debate/tournament.py"):
        p = ROOT / rel
        out[Path(rel).name] = len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0

    # Cross-repo duplication — the carrier count Stage 2 must move.
    client = CLIENT_CHECKOUT
    ident = div = 0
    if client.exists():
        for f in (ROOT / "app").rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            rel = f.relative_to(ROOT)
            other = client / rel
            if other.exists():
                try:
                    if f.read_bytes() == other.read_bytes():
                        ident += 1
                    else:
                        div += 1
                except OSError:
                    pass
    out["crossrepo_identical"] = ident
    out["crossrepo_diverged"] = div
    out["crossrepo_total"] = ident + div
    return out


def capture(label: str) -> dict:
    conn = _db()
    try:
        snap = {
            "label": label,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "git_head": _sh("git rev-parse --short HEAD"),
            "behavioural": behavioural(conn.cursor()),
            "structural": structural(),
        }
    finally:
        conn.close()
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    return snap


def _render(snap: dict) -> None:
    b, s = snap["behavioural"], snap["structural"]
    print(f"  git={snap['git_head']}  window from {b['window_from']}  desks={b['desks_sampled']}")
    print(f"  actions            : {b['action_distribution']}")
    print(f"  policy_action      : {b['policy_action_distribution']}")
    print(f"  provenance         : {b['provenance_distribution']}")
    f = b["board_confidence_at_or_above_floor"]
    print(f"  board conf >= 70   : {f['n_ge_70']} / {f['n_total']}")
    print(f"  agent seconds/tkr  : {b['sum_avg_seconds_per_ticker']}")
    print(f"  app LOC / files    : {s['app_loc']} / {s['app_py_files']}")
    print(f"  orchestrator LOC   : {s.get('orchestrator_loc')} ({s.get('orchestrator_if')} if)")
    print(f"  cross-repo dup     : {s['crossrepo_total']} ({s['crossrepo_diverged']} diverged)")


def diff(a: str, b: str) -> int:
    pa, pb = OUT_DIR / f"{a}.json", OUT_DIR / f"{b}.json"
    if not pa.exists() or not pb.exists():
        print(f"missing snapshot: {pa if not pa.exists() else pb}")
        return 1
    sa = json.loads(pa.read_text(encoding="utf-8"))
    sb = json.loads(pb.read_text(encoding="utf-8"))

    print(f"\n=== {a} -> {b} ===\n")
    print("STRUCTURAL (movement here is the POINT)")
    for k in sorted(set(sa["structural"]) | set(sb["structural"])):
        va, vb = sa["structural"].get(k), sb["structural"].get(k)
        if va != vb:
            print(f"  {k:26} {va} -> {vb}")

    print("\nBEHAVIOURAL (movement here needs a PREDICTION)")
    changed = False
    for key in ("action_distribution", "policy_action_distribution",
                "provenance_distribution", "board_confidence_bands",
                "artifact_presence_pct"):
        va, vb = sa["behavioural"].get(key, {}), sb["behavioural"].get(key, {})
        if va != vb:
            changed = True
            print(f"  {key}:")
            for k in sorted(set(va) | set(vb)):
                if va.get(k) != vb.get(k):
                    print(f"    {k:28} {va.get(k)} -> {vb.get(k)}")
    fa = sa["behavioural"]["board_confidence_at_or_above_floor"]
    fb = sb["behavioural"]["board_confidence_at_or_above_floor"]
    if fa != fb:
        changed = True
        print(f"  board conf >=70: {fa['n_ge_70']}/{fa['n_total']} -> "
              f"{fb['n_ge_70']}/{fb['n_total']}")
    if not changed:
        print("  (no behavioural change — a clean structural-only stage)")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", help="capture a snapshot under this name")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.diff:
        return diff(*args.diff)
    if args.list:
        for p in sorted(OUT_DIR.glob("*.json")) if OUT_DIR.exists() else []:
            print(" ", p.stem)
        return 0
    if not args.label:
        ap.print_help()
        return 1

    snap = capture(args.label)
    print(f"\ncaptured '{args.label}' -> {OUT_DIR / (args.label + '.json')}")
    _render(snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
