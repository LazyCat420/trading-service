"""Per-agent prompt-contract compliance for one cycle's desks (read-only).

Each V3 agent's system prompt makes hard promises about its artifact
(required blocks, enums, falsifiable calls) and its whiteboard duties.
This instrument replays those promises against what a cycle actually
stored — the audit question is always "what did the agent DO vs what was
it told" (ch.97 follow-on; first run scored cycle-v3-1787729519 at 29/29).

Usage (inside the trading-service container):
    python scripts/agent_contract_report.py <cycle_id> [ticker]
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from app.db import mongo_store  # noqa: E402


def _checks(desk: dict, wb_sections: set[str]) -> list[tuple[str, str, bool, str]]:
    def g(name):
        return desk.get(name) or {}

    rc, dn, fr = g("regime_classification"), g("desk_note"), g("fundamental_report")
    qr, vr = g("quant_report"), g("valuation_report")
    bull, bear, bd = g("bull_argument"), g("bear_rebuttal"), g("bull_defense")
    dj, fd, td = g("debate_judge"), g("final_decision"), g("trade_decision")
    dt = td.get("dynamic_trigger") or {}
    pa = bear.get("preferred_alternative")

    out = [
        ("regime", "forward_call present", bool(rc.get("forward_call")), str(rc.get("forward_call"))),
        ("regime", "regime enum", rc.get("regime") in ("HIGH_VOLATILITY", "DEEP_DISCOUNT", "CONTRADICTORY"), str(rc.get("regime"))),
        ("junior", "triage_recommendation present", bool(dn.get("triage_recommendation")), str(dn.get("triage_recommendation"))),
        ("junior", "catalyst_call present", bool(dn.get("catalyst_call")), str(dn.get("catalyst_call"))),
        ("junior", "data_gaps is list", isinstance(dn.get("data_gaps"), list), str(dn.get("data_gaps"))),
        ("junior", "whiteboard market_context written", "market_context" in wb_sections, ""),
        ("fundamental", "5 pillars", len(fr.get("pillars") or {}) >= 5, str(list(fr.get("pillars") or {}))),
        ("fundamental", "near_term_read present", bool(fr.get("near_term_read")), str(fr.get("near_term_read"))),
        ("fundamental", "positioning_read present", "positioning_read" in fr, str(fr.get("positioning_read"))),
        ("fundamental", "whiteboard risk_flags written", "risk_flags" in wb_sections, ""),
        ("quant", "overlays present", bool(qr.get("overlays")), str(qr.get("overlays"))),
        ("quant", "risk_metrics present", bool(qr.get("risk_metrics")), str(list(qr.get("risk_metrics") or {}))),
        ("quant", "whiteboard signals posted", "signals" in wb_sections, ""),
        ("valuation", "verdict present", bool(vr.get("verdict")), str(vr.get("verdict"))),
        ("valuation", "what_would_change_my_mind", bool(vr.get("what_would_change_my_mind")), str(vr.get("what_would_change_my_mind"))),
        ("valuation", "doctrine_rules_applied", bool(vr.get("doctrine_rules_applied")), str(vr.get("doctrine_rules_applied"))),
        ("bull", "invalidation in catalyst_timeline", bool((bull.get("catalyst_timeline") or {}).get("invalidation")), str((bull.get("catalyst_timeline") or {}).get("invalidation"))),
        ("bear", "independent_risks list", isinstance(bear.get("independent_risks"), list), str(len(bear.get("independent_risks") or []))),
        ("bear", "preferred_alternative shape", isinstance(pa, dict) and "ticker" in pa, str(pa)),
        ("defense", "concessions present", "concessions" in bd, str(bd.get("concessions"))),
        ("defense", "final_confidence <= bull confidence", (bd.get("final_confidence") or 0) <= (bull.get("confidence") or 100), f"{bd.get('final_confidence')} vs {bull.get('confidence')}"),
        ("judge", "proposition_verdicts present", bool(dj.get("proposition_verdicts")), str(dj.get("proposition_verdicts"))),
        ("judge", "weaknesses_of_winner present", bool(dj.get("weaknesses_of_winner")), str(dj.get("weaknesses_of_winner"))),
        ("judge", "winner enum", dj.get("winner") in ("bull", "bear", "tie"), str(dj.get("winner"))),
        ("board", "bear_verdict_response when debate had winner", bool(fd.get("bear_verdict_response")) if dj.get("winner") else True, str(fd.get("bear_verdict_response"))),
        ("board", "conviction_vector.data_quality", "data_quality" in (fd.get("conviction_vector") or {}), str((fd.get("conviction_vector") or {}).get("data_quality"))),
        ("synth", "signal_weights non-empty", bool(td.get("signal_weights")), str(td.get("signal_weights"))),
        ("synth", "internal_consensus_score present", td.get("internal_consensus_score") is not None, str(td.get("internal_consensus_score"))),
        ("synth", "dynamic_trigger has value when typed", (dt.get("value") is not None) if dt.get("type") else True, str(dt)),
    ]
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cid = sys.argv[1]
    only = sys.argv[2].upper() if len(sys.argv) > 2 else None

    q = {"cycle_id": cid}
    if only:
        q["ticker"] = only
    rows = mongo_store.find_docs("shared_desk", q)
    if not rows:
        print(f"no desks for {cid}")
        return 1

    total_fail = 0
    for row in rows:
        desk = json.loads(row.get("desk_data") or "{}")
        ticker = row.get("ticker")
        wb_sections = {
            d.get("section")
            for d in mongo_store.find_docs(
                "whiteboard_entries", {"cycle_id": cid, "ticker": ticker},
                projection={"section": 1},
            )
        }
        checks = _checks(desk, wb_sections)
        # Skipped tiers (glance/delta REAFFIRM) never ran the full panel —
        # an absent artifact there is routing, not a broken promise.
        ran_full = bool(desk.get("desk_note"))
        fails = [(a, r, ev) for a, r, ok, ev in checks if not ok]
        if not ran_full:
            print(f"\n== {ticker}: skipped tier (no desk_note) — {len(checks)} checks not applicable")
            continue
        print(f"\n== {ticker}: {len(checks) - len(fails)}/{len(checks)} PASS")
        for a, r, ev in fails:
            total_fail += 1
            print(f"  FAIL {a}: {r}  | {ev[:100]}")
    print(f"\ntotal FAIL: {total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
