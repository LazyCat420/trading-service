#!/usr/bin/env python3
"""Does the debate beat asking the same model k times with no debate?

    python3 scripts/self_consistency_bench.py --limit 40 --k 5
    python3 scripts/self_consistency_bench.py --limit 10 --k 1 --keep-debate  # harness check
    python3 scripts/self_consistency_bench.py --limit 40 --k 5 --json out.json

THE QUESTION, AND WHY IT IS THIS ONE
====================================
`scripts/score_panel.py` names self-consistency — same model, FULL data packet,
k independent samples, no debate, no partition — as "the one that decides
whether the panel ships", and then prints, unconditionally, that it is not in
its report. It never has been: there is no `--k`, no sampling loop, and nothing
in `scratch/`, `reports/` or `docs/` holding a result. This is the runner.

It matters because the debate is not cheap and its record is mixed. The
tournament was RETIRED on measurement (28.2% of pipeline tokens, Brier 0.3090
against a 0.2266 base rate). The linear bull -> bear -> defense -> judge that
replaced it fixed a real defect — the bear won 72-94% of debates when the bull
could not answer, now ~38% — but "we fixed the broken version" is not "it beats
not doing it".

WHAT MAKES IT A SANDBOX
=======================
1. **It never saves.** The desk is rebuilt in memory, mutated, and dropped.
   `run_v3_agent` records telemetry onto the desk object, not the database, and
   nothing here calls `save_desk`. The quant persistence hooks are the only
   writers in that path and they are quant-only.
2. **It never claims a cycle.** No `pipeline_state`, no `START_CYCLE`. Cycle
   ids are stamped `sc-*` so any row that does escape is identifiable.
3. **It never trades.**

Note for anyone copying `bench_stage.py`'s header: that script's read-only
guard is `SET default_transaction_read_only`, which is **psycopg and therefore
dead** since the cutover. The safety here is that no write path is called, not
a database mode.

GRADING
=======
Both arms are graded by `outcome_tracker._classify`, the production rule, and
scored correct by `decision_audit._CORRECT` — the same direction-aware set the
desk is graded by. That matters on a long-only book: a name that FELL after a
HOLD is a hold that was RIGHT, and a rule that misses this is the one that
reported 32% hold accuracy when the honest figure was 58%.

The verdict comes from `sequential.paired_disagreement_test` — an anytime-valid
e-process, so this can be re-run as outcomes accrue without spending alpha on
each peek. Only pairs where the two arms DISAGREE are informative, so the
effective n is far below the desk count and will usually say "undecided". That
is the honest answer at this sample size, not a failure of the runner.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.autoresearch.outcome_tracker import _classify  # noqa: E402
from app.autoresearch.sequential import paired_disagreement_test  # noqa: E402
# The module whose whole job is grading a champion against a challenger. Its
# `_champion_correct` returns None for FLAT rather than False, which is the
# behaviour that matters here: scoring an unscoreable outcome as WRONG would
# bias whichever arm produced more of them.
from app.routers.challenger_router import _champion_correct  # noqa: E402
from app.db import mongo_query  # noqa: E402
from app.v3.shared_desk import SharedDesk  # noqa: E402

#: Everything the debate produced, plus both decision artifacts. The two
#: decision artifacts are the LIVE ANSWER — leaving either in would let the
#: model read the verdict it is being asked to reproduce, and the whole
#: comparison would measure transcription.
DEBATE_ARTIFACTS = (
    "bull_argument",
    "bear_rebuttal",
    "bull_defense",
    "debate_judge",
    "tournament_result",
    "final_decision",     # the Board verdict: "Baseline = the Board's verdict"
    "trade_decision",     # the live answer itself
    "delta_report",       # a one-agent re-look that also carries an action
)

#: Keys inside `cycle_metadata` that exist only because the debate ran.
#: `decision_score` is deliberately NOT here — it is the free deterministic
#: baseline, computed without any agent, and present before the debate starts.
_DEBATE_META = ("bear_substitute", "defense_failed_open", "wake_pool")

#: The SECTION HEADERS `get_compressed_context` renders for the debate, taken
#: from `shared_desk.py`. The leak check anchors on these and not on the words
#: "bull"/"bear"/"board", which appear in ordinary research prose — a first
#: draft matching those reported a leak on every desk while the context held
#: nothing but the four research sections.
_DEBATE_SECTIONS = (
    "## Bull Thesis",
    "## Bear Rebuttal",
    "## Bull Final Defense",
    "## Debate Judge Verdict",
    "## Tournament Debate Verdict",
    "## Board of Directors Verdict",
)

_OUTCOME_EXCLUDE = {None, "", "DEGRADED_ARTIFACT", "CANCELED"}


def _as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _resolved_outcomes() -> dict:
    """(cycle_id, TICKER) -> the resolved outcome row, production cycles only."""
    out = {}
    for r in mongo_query.find_dicts("decision_outcomes", {}):
        if r.get("outcome") in _OUTCOME_EXCLUDE:
            continue
        cid = str(r.get("cycle_id") or "")
        if not cid.startswith("cycle-v3-"):
            continue
        out[(cid, str(r.get("ticker") or "").upper())] = r
    return out


def _replayable(limit: int) -> list[tuple]:
    """Desks that HAD a debate and whose decision has since resolved.

    Requiring the debate is what makes the comparison fair: a desk that never
    debated has no debate arm to score, and including it would compare the
    baseline against nothing while calling the result a debate score.
    """
    res = _resolved_outcomes()
    rows = []
    for d in mongo_query.find_dicts("shared_desk", {}):
        key = (str(d.get("cycle_id") or ""), str(d.get("ticker") or "").upper())
        row = res.get(key)
        if row is None:
            continue
        data = _as_dict(d.get("desk_data"))
        if not (data.get("bull_argument") and data.get("bear_rebuttal")):
            continue
        rows.append((key, data, row))
    # Newest first, so a truncated run samples the current prompt surface
    # rather than a random slice of five weeks of prompt edits.
    rows.sort(key=lambda r: str(r[2].get("created_at") or ""), reverse=True)
    return rows[:limit] if limit else rows


def _strip(data: dict, keep_debate: bool) -> SharedDesk:
    desk = SharedDesk.from_dict(copy.deepcopy(data))
    if keep_debate:
        return desk
    for name in DEBATE_ARTIFACTS:
        setattr(desk, name, None)
    meta = desk.cycle_metadata if isinstance(desk.cycle_metadata, dict) else {}
    for k in _DEBATE_META:
        meta.pop(k, None)
    desk.cycle_metadata = meta
    # `phase_outcomes` and `agent_telemetry` name which agents ran and how they
    # scored. Neither is rendered into the context, but both would travel into
    # any future prompt change, so they go too.
    desk.phase_outcomes = {}
    desk.agent_telemetry = []
    desk.artifact_tags = {}
    return desk


def _leaks(desk: SharedDesk, live_action: str, live_conf) -> list[str]:
    """Did the live verdict survive the strip?

    A stripped desk that still names the action is measuring transcription, not
    judgement. This reads the ACTUAL context the agent will be given, not the
    attributes it was built from — the same reason a probe belongs on the node
    that owns the value rather than near it.
    """
    ctx = desk.get_compressed_context(include_debate=False) or ""
    return [f"context still renders {h.strip()!r}"
            for h in _DEBATE_SECTIONS if h in ctx]


async def _sample(desk_data: dict, k: int, keep_debate: bool, cycle_id: str,
                  live_action: str, live_conf) -> dict:
    from app.v3.agents import decision_agent
    from app.v3.agent_runner import run_v3_agent

    actions, confs, failures = [], [], 0
    leak_report: list[str] = []
    for i in range(k):
        desk = _strip(desk_data, keep_debate)
        if i == 0 and not keep_debate:
            leak_report = _leaks(desk, live_action, live_conf)
        try:
            await run_v3_agent(desk, decision_agent,
                               cycle_id=cycle_id, bot_id="self-consistency")
        except Exception as exc:  # noqa: BLE001 - a failed sample is a datum
            failures += 1
            leak_report.append(f"sample {i} raised {type(exc).__name__}: {exc}")
            continue
        art = _as_dict(getattr(desk, "trade_decision", None))
        act = str(art.get("action") or "").strip().upper()
        if act:
            actions.append(act)
            if isinstance(art.get("confidence"), (int, float)):
                confs.append(float(art["confidence"]))
        else:
            failures += 1

    tally = Counter(actions)
    return {
        "actions": actions,
        "majority": tally.most_common(1)[0][0] if tally else None,
        "agreement": (tally.most_common(1)[0][1] / len(actions)) if actions else 0.0,
        "confidence": (sum(confs) / len(confs)) if confs else None,
        "failures": failures,
        "leaks": leak_report,
    }


def _correct(action: str | None, pnl) -> bool | None:
    """Was this action right? None when the outcome cannot score it.

    `_classify` turns an action plus a realised move into the production
    outcome label, and `_champion_correct` grades that label. Both are the
    shipped implementations, deliberately: a locally-written rule here is how
    the desk once read 32% hold accuracy against an honest 58%, by counting a
    name that FELL after a HOLD as a miss on a book that cannot short.
    """
    if not action or pnl is None:
        return None
    try:
        return _champion_correct(action, _classify(action, float(pnl)))
    except Exception:  # noqa: BLE001
        return None


async def _run(a) -> dict:
    rows = _replayable(a.limit)
    if not rows:
        return {"error": "no replayable desks — a resolved outcome AND a "
                         "stored desk that debated are both required"}

    cycle_id = f"sc-{uuid.uuid4().hex[:8]}"
    pairs, per_desk = [], []
    identical = 0

    for (cid, tk), data, outcome in rows:
        live = _as_dict(data.get("trade_decision")) or _as_dict(data.get("final_decision"))
        live_action = str(live.get("action") or outcome.get("action") or "").upper()
        live_conf = live.get("confidence")
        pnl = outcome.get("pnl_pct")

        got = await _sample(data, a.k, a.keep_debate, cycle_id, live_action, live_conf)
        if a.k > 1 and len(set(got["actions"])) == 1 and len(got["actions"]) == a.k:
            identical += 1

        sc_ok = _correct(got["majority"], pnl)
        live_ok = _correct(live_action, pnl)
        per_desk.append({
            "cycle_id": cid, "ticker": tk,
            "live_action": live_action, "live_confidence": live_conf,
            "sc_action": got["majority"], "sc_agreement": got["agreement"],
            "sc_confidence": got["confidence"], "sc_samples": got["actions"],
            "pnl_pct": pnl, "outcome": outcome.get("outcome"),
            "live_correct": live_ok, "sc_correct": sc_ok,
            "failures": got["failures"], "leaks": got["leaks"],
        })
        print(f"  {tk:6s} {cid[-10:]}  live={live_action:4s}"
              f" sc={str(got['majority']):4s}"
              f" agree={got['agreement']:.0%}"
              f" pnl={pnl if pnl is None else round(float(pnl), 2)}"
              f"  live_ok={live_ok} sc_ok={sc_ok}", flush=True)
        if got["leaks"]:
            print(f"         LEAK: {'; '.join(got['leaks'][:3])}", flush=True)

        if live_ok is not None and sc_ok is not None:
            # paired_disagreement_test takes (champion, challenger); the live
            # debate path is the champion because it is what ships.
            pairs.append((live_ok, sc_ok))

    verdict = paired_disagreement_test(pairs) if pairs else None
    return {
        "cycle_id": cycle_id, "k": a.k, "keep_debate": a.keep_debate,
        "desks": len(rows), "scoreable_pairs": len(pairs),
        "identical_sample_sets": identical,
        "verdict": verdict, "per_desk": per_desk,
    }


def _report(out: dict, a) -> int:
    if out.get("error"):
        print(f"VACUITY: {out['error']}")
        return 1

    print(f"\nSELF-CONSISTENCY vs THE DEBATE   k={out['k']}"
          f"{'  [KEEP-DEBATE HARNESS CHECK]' if out['keep_debate'] else ''}")
    print(f"desks replayed: {out['desks']}   scoreable pairs: {out['scoreable_pairs']}")

    leaky = [d for d in out["per_desk"] if d["leaks"]]
    if leaky:
        print(f"\n⚠ LEAKAGE on {len(leaky)}/{out['desks']} desks — the stripped")
        print("  context still carries the debate. Until that is 0 this measures")
        print("  transcription, not judgement, and the verdict below is void.")

    if out["keep_debate"]:
        # The harness check: replaying with the debate INTACT must reproduce
        # the live action. If it cannot, the stripped comparison is meaningless
        # because the replay itself is not faithful.
        agree = sum(1 for d in out["per_desk"]
                    if d["sc_action"] and d["sc_action"] == d["live_action"])
        n = sum(1 for d in out["per_desk"] if d["sc_action"])
        print(f"\nHARNESS CHECK — replay with the debate intact reproduces the")
        print(f"live action on {agree}/{n}"
              f"{f' ({100.0 * agree / n:.0f}%)' if n else ''}.")
        print("  A low number here condemns the REPLAY, not the desk: it means")
        print("  the same inputs do not reproduce the same verdict, so nothing")
        print("  measured against a modified packet can be attributed.")
        return 0

    if a.k > 1:
        ident = out["identical_sample_sets"]
        print(f"\nSAMPLE DIVERSITY — {ident}/{out['desks']} desks returned k"
              f" IDENTICAL samples.")
        if ident == out["desks"]:
            print("  ALL of them. At temperature 0 self-consistency degenerates")
            print("  to a single sample and this is not the baseline it claims")
            print("  to be. Fix the sampling before reading anything below.")

    live_ok = sum(1 for d in out["per_desk"] if d["live_correct"])
    sc_ok = sum(1 for d in out["per_desk"] if d["sc_correct"])
    n = out["scoreable_pairs"]
    if n:
        print(f"\nACCURACY (direction-aware, the production rule)")
        print(f"  the debate path .............. {live_ok}/{n} ({100.0 * live_ok / n:.0f}%)")
        print(f"  self-consistency (k={out['k']}) ...... {sc_ok}/{n} ({100.0 * sc_ok / n:.0f}%)")

    v = out.get("verdict")
    print("\nVERDICT — anytime-valid paired e-process (only DISAGREEMENTS count)")
    if not v:
        print("  no scoreable pairs.")
        return 1
    print(f"  informative pairs ..... {v['informative_pairs']}"
          f"   (champion {v['champion_wins']} / challenger {v['challenger_wins']},"
          f" ties {v['ties']})")
    print(f"  e-value ............... {v['e_value']:.3g}")
    print(f"  leader ................ {v['leader']}")
    print(f"  {v['verdict']}")
    print("\n  champion = the live bull->bear->defense->judge path.")
    print("  challenger = the same model, full packet, no debate.")
    print("  An e-value below 20 is NOT evidence the debate wins — it is a")
    print("  sample too small to say, and it can be re-run as outcomes accrue")
    print("  because the e-process does not spend alpha on a peek.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20,
                    help="desks to replay, newest first (0 = all)")
    ap.add_argument("--k", type=int, default=5, help="samples per desk")
    ap.add_argument("--keep-debate", action="store_true",
                    help="do NOT strip — the harness fidelity check")
    ap.add_argument("--json", help="write the full per-desk record here")
    a = ap.parse_args()

    out = asyncio.run(_run(a))
    if a.json and not out.get("error"):
        Path(a.json).write_text(json.dumps(out, indent=2, default=str))
        print(f"\nwrote {a.json}")
    return _report(out, a)


if __name__ == "__main__":
    raise SystemExit(main())
