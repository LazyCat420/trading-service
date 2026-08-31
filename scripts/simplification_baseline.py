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

Exit codes: 0 on a capture, a `--list`, and a diff that was actually performed.
1 with no `--label`, when a snapshot named for `--diff` is missing, and when
`--diff` REFUSES because the two snapshots read different stores (see below).

READS MONGO (ported 2026-08-30)
------------------------------
The behavioural half used to read the SQL archive, frozen since the
2026-08-19 cutover: it still answered, so this script still printed
a snapshot, but every number in it stopped in August and a "no behavioural
change" verdict was guaranteed no matter what a stage did. Snapshots captured
before the port carry no `store` key and are NOT comparable to one captured
after it -- `--diff` prints the structural half and then refuses the
behavioural half rather than quietly differencing the two stores.

The switch also handed this baseline 67 documents the archive never had: the
unit suite wrote its fixtures into the production database on 2026-08-18,
before `tests/conftest.py::block_production_mongo` existed. Those six minutes
are cut out of the window and the snapshot reports how many rows that removed,
per collection, under `test_burst_excluded` -- see TEST_BURST_FROM.

WHAT IS DEAD IN HERE
--------------------
`structural()` still counts `app/cognition/debate/tournament.py` and
`debate_coordinator.py`, and neither file exists any more -- both counters have
been pinned at 0 since the tournament was retired (HANDOFF_tournament_retired
_2026-07-29.md). They are kept, at 0, because the pre-wave snapshots carry
their old line counts and dropping the keys would make those snapshots
undiffable; nothing else reads them.

There is also no usable BEFORE snapshot left in `.simplification_baselines/`:
`pre-stage1.json` and `post-stage4.json` are both pre-port captures of the SQL
archive, so every `--diff` against either now refuses. The wave needs a fresh
`--label pre-<stage>` against Mongo before the instrument is usable again.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".simplification_baselines"
MAIN_CHECKOUT = Path("/home/lazycat/github/projects/sun/trading-service")
CLIENT_CHECKOUT = Path("/home/lazycat/github/projects/sun/trading-client")

# The window that is fully instrumented: policy_action landed 07-23,
# decision_provenance 07-25. Before that the classifier would be labelling
# rows that carry no labels — see docs/BOARD_HOLD_DECOMPOSITION_2026-07-29.md.
INSTRUMENTED_FROM = "2026-07-26"

STORE = "mongo"

# The four collections the behavioural half reads, named ONCE.
#
# These are POSTGRES TABLE NAMES: `mongo_store._coll` resolves each one through
# `app/db/collections.collection_for`, which is what makes the physical rename
# affordable. A typo here does not raise. Mongo has no schema, a read of a name
# nothing wrote returns nothing, and this script would compile, run, print
# `desks=0`, empty bands, an empty artifact scan — and a `--diff` saying "no
# behavioural change — a clean structural-only stage". That is trap 7 and it is
# the single most dangerous failure this file has, so the names are pinned two
# ways in tests/unit/test_simplification_baseline_reads_mongo.py: against
# `collections.is_mapped()` with no database, and against the live store.
TRADES = "trade_results"
DESKS = "shared_desk"
TELEMETRY = "v3_agent_telemetry"
GUARDRAILS = "v3_guardrail_firings"
COLLECTIONS = (TRADES, DESKS, TELEMETRY, GUARDRAILS)

# ── the rows the TEST SUITE left in the production store ─────────────────────
#
# On 2026-08-18 the unit suite ran with nothing guarding Mongo and wrote its
# fixtures into the production database. `tests/conftest.py::block_production
# _mongo` was added that same day and records the measurement that prompted it
# ("get_doc_db() -> Database name=trading_bot ... That is production"). What it
# wrote before the guard landed is still there, and Postgres — which was still
# the production store that evening — never had it. So the store switch handed
# this baseline 67 documents that no archive-era capture ever counted, and they
# are not spread evenly: 56 of them are guardrail firings, 7% of the window's
# total, and three fabricated agents ('a', 'agent_0', 'agent_1', n=1 each) sit
# in the cost table beside the real ones.
#
# The census is a WALL-CLOCK INTERVAL and not a list of test cycle ids, because
# a list cannot see all of it. Measured 2026-08-30, across all four
# collections:
#
#     [19:44, 19:50) on 2026-08-18   mongo 67   archive 0
#     [19:00, 21:00) on 2026-08-18   mongo 82   archive 15
#
# Nothing production wrote in those six minutes; everything Mongo holds there
# arrived from the suite. Widening the interval starts eating real cycles, so
# the bound is tight on purpose. And the interval catches a row no id list
# would: `v3_agent_telemetry` id f814334c… is a fixture that COPIED a live
# cycle id, `cycle-v3-1786455000`, whose other 79 rows are a real 2026-08-11
# cycle present in both stores. Excluding that cycle id would have deleted a
# production cycle; excluding the minute it was written costs nothing.
#
# An interval also cannot drift the way a hand-maintained id list does: it is
# closed and in the past, so no future row can fall into it and no future test
# burst can hide inside it. A NEW burst would land outside it and show up as
# movement, which is what the instrument is for. `behavioural()` still reports
# what it removed per collection (`test_burst_excluded`) so the number is
# checkable against the 67 above rather than taken on trust.
TEST_BURST_FROM = datetime(2026, 8, 18, 19, 44)
TEST_BURST_TO = datetime(2026, 8, 18, 19, 50)


def _mongo():
    """The Mongo read seam, with the env loaded the way `_db()` used to load it."""
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    # A git worktree has no .env of its own — it lives in the main checkout.
    for env in (ROOT / ".env", MAIN_CHECKOUT / ".env"):
        if env.exists():
            load_dotenv(env)
            break
    from app.db import mongo_query

    return mongo_query


def _date_window() -> dict:
    """`WHERE created_at >= INSTRUMENTED_FROM`, as a Mongo filter.

    A `datetime` and not the ISO string the SQL passed as a parameter. NOT
    because the string fails today — it does not, and the first version of this
    comment said it did. `mongo_store.find_docs/aggregate/count_docs` route
    every filter through `app/db/date_fields.coerce_filter`, `created_at` is
    registered as a timestamp column on all four of these collections, and
    `as_timestamp` parses `"2026-07-26"` into `datetime(2026, 7, 26)`. Measured
    2026-08-30: `_window()` forced to return the bare string produces a
    byte-identical snapshot.

    The datetime is still the right value to pass, for a reason that does not
    depend on that seam. `date_fields` covers exactly the (collection, field)
    pairs `app/db/schema_manifest.json` declares, a registry this script does
    not own; the same literal aimed at a collection outside it reaches the
    server as a String, and BSON orders every String ABOVE every Date, so the
    filter would match nothing and every distribution here would come back `{}`
    with no error and no empty-result check to catch it. Passing the type the
    store actually holds is correct at the seam itself and stays correct if the
    registry ever loses this entry.

    Naive, because the client is not tz_aware and hands these back as naive UTC.
    """
    return {"created_at": {"$gte": datetime.strptime(INSTRUMENTED_FROM, "%Y-%m-%d")}}


def _window() -> dict:
    """`_date_window()` with the 2026-08-18 test burst cut out of it.

    See TEST_BURST_FROM. `$nor` and not a second `created_at` key: a filter
    document cannot carry the same field twice, and `date_fields.coerce_filter`
    walks `$and`/`$or`/`$nor` so the bound inside it is normalised exactly like
    the one outside.
    """
    return {
        **_date_window(),
        "$nor": [{"created_at": {"$gte": TEST_BURST_FROM, "$lt": TEST_BURST_TO}}],
    }


def _round1(x: float) -> float:
    """Postgres `round(numeric, 1)` — half AWAY FROM ZERO.

    Python's built-in `round()` is half-to-even, so `round(0.25, 1)` is 0.2
    where Postgres gave 0.3. It bites only on an exact .x5, which is why the
    two agreed on all 14 agents in the frozen window; it is still the wrong
    rule, and a baseline exists to be differenced against another one.
    """
    return float(Decimal(repr(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


# ── behavioural invariants ───────────────────────────────────────────────────


def _distribution(mq, collection: str, column: str) -> dict:
    """`SELECT col, count(*) FROM t WHERE created_at >= %s GROUP BY 1`.

    The keys are `str(value)`, so two distinct group keys can render to the
    same one — a NULL and a MISSING field are the pair to watch, since SQL put
    them in a single NULL group and Mongo need not. Measured on this server
    (7.0.34) it already does: `$group` on an absent field yields `{f: None}`,
    and `trade_results` reports one `None` group of 556 for its 550 nulls plus
    6 missing. So the fold is currently a no-op — and it stays, because the
    alternative failure is silent: a dict comprehension would let one group
    OVERWRITE the other, and a distribution whose total quietly drops by a few
    rows still looks exactly like a distribution.
    """
    rows = mq.group_rows(collection, _window(), [column], [("count", None)],
                         [("key", column), ("agg", 0)])
    out: dict[str, int] = {}
    for value, n in rows:
        out[str(value)] = out.get(str(value), 0) + n
    return out


def _desks(mq) -> list[dict]:
    """Every `desk_data` in the window, parsed — both shapes.

    `desk_data` is stored as a JSON **string** for desks written after the
    cutover and as a subdocument for the migrated ones (274 vs 1762 on
    2026-08-30). So a Mongo-side filter on
    `desk_data.final_decision.confidence` — the literal transcription of the
    old jsonb path — reads only the FROZEN half and silently drops every live
    desk, which is the one thing this baseline exists to see. Everything the
    SQL asked of desk_data is therefore computed here, in Python, over both
    shapes; that is also why one read now answers what were three queries.
    """
    out: list[dict] = []
    for (raw,) in mq.find_rows(DESKS, _window(), ["desk_data"]):
        d = raw
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _confidence(desks: list[dict]) -> tuple[dict, int, int]:
    """Board confidence in 5-point bands, plus the count at or above the floor.

    Was two SQL statements over the jsonb path; one pass over the parsed desks
    now, because the path does not exist for a desk stored as text.
    """
    bands: Counter = Counter()
    ge70 = 0
    total = 0
    for d in desks:
        fd = d.get("final_decision")
        raw = fd.get("confidence") if isinstance(fd, dict) else None
        if raw is None:
            continue
        try:
            c = float(raw)
        except (TypeError, ValueError):
            continue
        total += 1
        bands[int(math.floor(c / 5) * 5)] += 1
        if c >= 70:
            ge70 += 1
    return {str(b): n for b, n in sorted(bands.items())}, ge70, total


def _artifact_presence(desks: list[dict]) -> dict:
    """Control 2 of the HOLD decomposition: how often each artifact is PRESENT.

    An artifact is a populated sub-document — `{"quant_report": {...}}`. The
    `isinstance(v, dict)` half of the test is what makes this a count of
    artifacts and not a count of desk COLUMNS: every desk also carries
    `ticker`, `phase`, `desk_id` and friends, which are truthy scalars and
    would each read as a 100%-present "artifact", diluting every real
    percentage. The `and v` half is what makes an artifact that was ATTEMPTED
    and came back empty count as absent, which is the regression this control
    exists to see.
    """
    counts: Counter = Counter()
    for d in desks:
        for k, v in d.items():
            if isinstance(v, dict) and v:
                counts[k] += 1
    if not desks:
        return {}
    return {k: round(100.0 * n / len(desks), 1) for k, n in sorted(counts.items())}


def _agent_seconds(mq) -> list[tuple]:
    """`SELECT agent_name, count(*), round(avg(elapsed_ms)/1000.0,1)
        GROUP BY 1 ORDER BY 3 DESC NULLS LAST`."""
    rows = mq.group_rows(TELEMETRY, _window(), ["agent_name"],
                         [("count", None), ("avg", "elapsed_ms")],
                         [("key", "agent_name"), ("agg", 0), ("agg", 1)])
    out = [(name, n, None if avg_ms is None else _round1(float(avg_ms) / 1000.0))
           for name, n, avg_ms in rows]
    # DESC NULLS LAST: the first key puts the NULLs last under reverse=True.
    # It is a two-part key and not `r[2] or 0.0` because NULL and 0.0 are
    # different answers — on the live window two agents average a genuine 0.0
    # and two more have no timings at all, and collapsing the NULLs onto 0.0
    # ties all four and makes their order fall out of input order.
    out.sort(key=lambda r: (r[2] is not None, r[2] if r[2] is not None else 0.0),
             reverse=True)
    return out


def _test_burst_excluded(mq) -> dict:
    """How many documents the 2026-08-18 burst window removed, per collection.

    Part of the snapshot, and compared by `--diff`, so the exclusion is a
    number someone can check (67 = 4 + 3 + 4 + 56 on 2026-08-30) rather than a
    claim in a comment. If it ever moves, the interval is wrong — nothing can
    be written into a closed interval six weeks in the past.
    """
    dated = _date_window()
    kept = _window()
    return {c: mq.count(c, dated) - mq.count(c, kept) for c in COLLECTIONS}


def behavioural(mq) -> dict:
    out: dict = {"window_from": INSTRUMENTED_FROM, "store": STORE}

    out["action_distribution"] = _distribution(mq, TRADES, "action")
    out["policy_action_distribution"] = _distribution(mq, TRADES, "policy_action")
    out["provenance_distribution"] = _distribution(mq, TRADES, "decision_provenance")

    desks = _desks(mq)

    # Board confidence histogram in 5-point bands. This is THE number the
    # simplification is predicted to move (removing evidence lowers it), so it
    # is captured as a distribution rather than a mean.
    bands, ge70, tot = _confidence(desks)
    out["board_confidence_bands"] = bands
    out["board_confidence_at_or_above_floor"] = {"n_ge_70": ge70, "n_total": tot}

    # Artifact presence. Control 2 of the HOLD decomposition: if presence FALLS
    # while confidence falls, an input was removed (regression). If presence
    # holds or rises, the confidence drop is honest.
    out["desks_sampled"] = len(desks)
    out["artifact_presence_pct"] = _artifact_presence(desks)

    # Cost. Stage 3 predicts ~30% off both of these.
    rows = _agent_seconds(mq)
    out["agent_seconds"] = {str(a): {"n": n, "avg_s": float(s or 0)} for a, n, s in rows}
    out["sum_avg_seconds_per_ticker"] = round(sum(float(s or 0) for _, _, s in rows), 1)

    out["guardrail_firings_total"] = mq.agg_row(
        GUARDRAILS, _window(), [("count", None)])[0]

    out["test_burst_excluded"] = _test_burst_excluded(mq)
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
    # Both of these were deleted with the tournament (2026-07-29) and have been
    # 0 ever since — kept so the pre-wave snapshots stay diffable. See the
    # module docstring.
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
    mq = _mongo()
    snap = {
        "label": label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_head": _sh("git rev-parse --short HEAD"),
        "behavioural": behavioural(mq),
        "structural": structural(),
    }
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    return snap


def _render(snap: dict) -> None:
    b, s = snap["behavioural"], snap["structural"]
    print(f"  git={snap['git_head']}  window from {b['window_from']}  desks={b['desks_sampled']}")
    print(f"  store              : {b.get('store', 'pg-archive (pre-port snapshot)')}")
    excluded = b.get("test_burst_excluded") or {}
    print(f"  08-18 burst removed: {sum(excluded.values())} {dict(sorted(excluded.items()))}")
    print(f"  actions            : {b['action_distribution']}")
    print(f"  policy_action      : {b['policy_action_distribution']}")
    print(f"  provenance         : {b['provenance_distribution']}")
    f = b["board_confidence_at_or_above_floor"]
    print(f"  board conf >= 70   : {f['n_ge_70']} / {f['n_total']}")
    print(f"  guardrail firings  : {b['guardrail_firings_total']}")
    print(f"  agent seconds/tkr  : {b['sum_avg_seconds_per_ticker']}")
    print(f"  app LOC / files    : {s['app_loc']} / {s['app_py_files']}")
    print(f"  orchestrator LOC   : {s.get('orchestrator_loc')} ({s.get('orchestrator_if')} if)")
    print(f"  cross-repo dup     : {s['crossrepo_total']} ({s['crossrepo_diverged']} diverged)")


def _store_of(snap: dict) -> str:
    """Which store a snapshot's behavioural half came from.

    A snapshot with no `store` key was captured before the 2026-08-30 port and
    therefore read the frozen SQL archive.
    """
    return snap["behavioural"].get("store", "pg-archive")


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
    if _store_of(sa) != _store_of(sb):
        # The SQL archive froze at the 2026-08-19 cutover, so a snapshot taken
        # from it and one taken from Mongo describe different populations over
        # the same window. Differencing them measures the migration, not the
        # stage. Printing the delta under a warning banner is not enough: the
        # numbers are the part people read, an 86 -> 506 line reads as a
        # catastrophic regression, and a pair that happened to be equal would
        # print "no behavioural change" underneath the banner. So the section
        # is NOT produced, and the exit status says the diff did not happen.
        print(f"  !! REFUSED: {a} read {_store_of(sa)}, {b} read {_store_of(sb)}.")
        print("     The SQL archive froze at the 2026-08-19 cutover, so the two")
        print("     behavioural halves cover different populations of the same")
        print("     window and every line of a delta would be attributed to")
        print("     whatever stage ran in between. Structural counts above are")
        print("     unaffected (they are read off the filesystem).")
        print(f"     Re-capture the BEFORE side: --label {a}-mongo")
        print()
        return 1
    changed = False
    for key in ("action_distribution", "policy_action_distribution",
                "provenance_distribution", "board_confidence_bands",
                "artifact_presence_pct", "test_burst_excluded"):
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
