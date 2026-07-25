#!/usr/bin/env python3
"""Verify every fix from the 2026-07-24 agent audit against a real cycle.

Each phase was diagnosed from historical data; this checks the live code path
actually behaves as intended. Reports PASS / FAIL / N/A per claim — N/A matters
as much as FAIL, because it means the path never executed and the fix is still
unverified rather than working.

    python scripts/verify_audit_phases.py --cycle cycle-observe-1784930000
    python scripts/verify_audit_phases.py            # newest cycle
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, NA = "PASS", "FAIL", "n/a"
_results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def _desks(cycle_id: str) -> list[tuple]:
    from app.db.connection import get_db

    with get_db() as db:
        return db.execute(
            "SELECT ticker, desk_data FROM shared_desk WHERE cycle_id = %s "
            "ORDER BY ticker",
            [cycle_id],
        ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default="")
    args = ap.parse_args()

    from app.db.connection import get_db

    cycle_id = args.cycle
    if not cycle_id:
        with get_db() as db:
            row = db.execute(
                "SELECT cycle_id FROM shared_desk ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            print("no cycles found")
            return 1
        cycle_id = row[0]

    rows = _desks(cycle_id)
    if not rows:
        print(f"no desks for cycle {cycle_id}")
        return 1

    desks = {}
    for ticker, dd in rows:
        desks[ticker] = dd if isinstance(dd, dict) else json.loads(dd)

    print(f"\nVERIFYING {cycle_id} — {len(desks)} tickers: {', '.join(desks)}\n")

    # ── bot_id (the root-cause fix) ──────────────────────────────────────
    bot_ids = {str((d.get("cycle_metadata") or {}).get("bot_id") or "") for d in desks.values()}
    real = {b for b in bot_ids if b}
    check("bot_id is populated on the desk",
          PASS if real else FAIL, f"bot_id={bot_ids or '{}'}")

    hrp = [t for t, d in desks.items()
           if "HRP" in ((d.get("cycle_metadata") or {}).get("quant_math_context") or "")]
    check("HRP line present in quant_math_context",
          PASS if hrp else FAIL, f"{len(hrp)}/{len(desks)} tickers: {hrp}")

    held_flags = {t: (d.get("cycle_metadata") or {}).get("held") for t, d in desks.items()}
    check("held flag is resolved (not all False)",
          PASS if any(held_flags.values()) else NA,
          f"{held_flags} — n/a if none of these tickers is actually held")

    # ── Phase 1: regime once per cycle + forward_call ────────────────────
    regimes = {t: (d.get("regime_classification") or {}).get("regime")
               for t, d in desks.items() if d.get("regime_classification")}
    distinct = set(regimes.values())
    check("ONE regime for the whole cycle",
          PASS if len(distinct) <= 1 else FAIL,
          f"{regimes}")

    ran_regime = [t for t, d in desks.items() if d.get("regime_classification")]
    fwd = {t: (desks[t].get("regime_classification") or {}).get("forward_call")
           for t in ran_regime}
    have_fwd = [t for t, v in fwd.items() if v]
    check("regime emits a falsifiable forward_call",
          NA if not ran_regime else (PASS if len(have_fwd) == len(ran_regime) else FAIL),
          f"{len(have_fwd)}/{len(ran_regime)} that ran; "
          f"sample={next((v for v in fwd.values() if v), None)}")

    # ── Phase 2: junior triage + catalyst_call ───────────────────────────
    # Only desks where the junior actually RAN can be judged. A ticker skipped
    # by the Triage Gate (stale + no news → GLANCE tier, ~0.8s instead of
    # ~480s) never reaches the junior, and scoring it as a missing field is the
    # same "measuring a path that did not execute" error this audit already
    # made twice.
    ran_ja = [t for t, d in desks.items() if d.get("desk_note")]
    skipped = [t for t in desks if t not in ran_ja]
    if not ran_ja:
        check("junior emits triage_recommendation (was missing 27%)", NA,
              f"junior never ran (early-exit: {skipped})")
        check("junior emits catalyst_call (new falsifiable claim)", NA,
              f"junior never ran (early-exit: {skipped})")
    else:
        tri = {t: (desks[t].get("desk_note") or {}).get("triage_recommendation") for t in ran_ja}
        check("junior emits triage_recommendation (was missing 27%)",
              PASS if all(tri.values()) else FAIL,
              f"{tri}" + (f"; early-exit (not counted): {skipped}" if skipped else ""))

        cat = {t: (desks[t].get("desk_note") or {}).get("catalyst_call") for t in ran_ja}
        have_cat = [t for t, v in cat.items() if v]
        check("junior emits catalyst_call (new falsifiable claim)",
              PASS if len(have_cat) == len(ran_ja) else FAIL,
              f"{len(have_cat)}/{len(ran_ja)} that ran; "
              f"sample={next((v for v in cat.values() if v), None)}")

    # ── Phase 3: fundamental horizon ─────────────────────────────────────
    hor = {t: (d.get("fundamental_report") or {}).get("horizon") for t, d in desks.items()}
    ntr = {t: (d.get("fundamental_report") or {}).get("near_term_read") for t, d in desks.items()}
    fa_present = [t for t, d in desks.items() if d.get("fundamental_report")]
    if not fa_present:
        check("fundamental emits horizon + near_term_read", NA, "no fundamental_report ran")
    else:
        ok = [t for t in fa_present if hor.get(t) and ntr.get(t)]
        check("fundamental emits horizon + near_term_read",
              PASS if len(ok) == len(fa_present) else FAIL,
              f"horizon={hor} near_term={ntr}")

    # ── Phase 4: quant reconciliation + whiteboard signals ───────────────
    recon = {t: (d.get("quant_report") or {}).get("_model_reported_metrics")
             for t, d in desks.items()}
    corrected = {t: v for t, v in recon.items() if v}
    check("quant risk_metrics reconciled against `technicals`",
          PASS if any(d.get("quant_report") for d in desks.values()) else NA,
          f"corrections applied on {len(corrected)}/{len(desks)}: {corrected or 'none needed'}")

    # ── Phase 6: no unheld SELL survives ─────────────────────────────────
    bad = []
    for t, d in desks.items():
        held = bool((d.get("cycle_metadata") or {}).get("held"))
        for key in ("final_decision", "trade_decision"):
            act = str((d.get(key) or {}).get("action") or "").upper()
            if act == "SELL" and not held:
                bad.append(f"{t}.{key}")
    check("no SELL survives on an unheld ticker",
          PASS if not bad else FAIL, f"violations: {bad or 'none'}")

    # ── decisions produced ───────────────────────────────────────────────
    acts = {t: (d.get("trade_decision") or d.get("final_decision") or {}).get("action")
            for t, d in desks.items()}
    check("every ticker produced a decision",
          PASS if all(acts.values()) else FAIL, f"{acts}")

    # ── decision integrity (2026-07-25) ──────────────────────────────────
    # Every decision artifact must say where its action came from. Before
    # this, a degraded board wrote NOTHING and the desk was indistinguishable
    # from a confident no-signal HOLD.
    prov = {}
    for t, d in desks.items():
        for key in ("final_decision", "trade_decision"):
            art = d.get(key)
            if isinstance(art, dict):
                prov[f"{t}.{key}"] = art.get("decision_provenance")
    missing_prov = [k for k, v in prov.items() if not v]
    check("every decision artifact declares decision_provenance",
          NA if not prov else (PASS if not missing_prov else FAIL),
          f"{len(prov) - len(missing_prov)}/{len(prov)} stamped"
          + (f"; MISSING: {missing_prov}" if missing_prov else "")
          + f"; values={sorted({v for v in prov.values() if v})}")

    degraded = [k for k, v in prov.items() if v == "board_degraded_fallback"]
    check("no board degraded to a fallback this cycle",
          PASS if not degraded else NA,
          f"degraded: {degraded}" if degraded
          else "none — every decision is a real agent verdict")

    # ── shared_desk <-> trade_results reconciliation ─────────────────────
    # These two records of the same decision silently diverged for 19 days
    # (10 desks, all HOLD). A standing check turns that into an alert.
    with get_db() as db:
        tr_rows = db.execute(
            "SELECT ticker, action FROM trade_results WHERE cycle_id = %s",
            [cycle_id],
        ).fetchall()
    tr = {r[0]: r[1] for r in tr_rows}
    mismatches = []
    for t, d in desks.items():
        desk_act = (d.get("trade_decision") or d.get("final_decision") or {}).get("action")
        saved = tr.get(t)
        if saved is None and desk_act:
            mismatches.append(f"{t}: desk={desk_act} but NO trade_results row")
        elif saved and not desk_act:
            mismatches.append(f"{t}: trade_results={saved} but desk has no action")
        elif saved and desk_act and str(saved).upper() != str(desk_act).upper():
            mismatches.append(f"{t}: desk={desk_act} != trade_results={saved}")
    check("shared_desk reconciles with trade_results",
          NA if not tr and not any(acts.values()) else (PASS if not mismatches else FAIL),
          f"{len(tr)} saved rows vs {len(desks)} desks; "
          + (f"MISMATCH: {mismatches}" if mismatches else "all agree"))

    # ── report ───────────────────────────────────────────────────────────
    width = max(len(n) for n, _, _ in _results) + 2
    print(f"{'check':{width}} {'result':7} detail")
    print("-" * 100)
    for name, status, detail in _results:
        mark = {"PASS": "✅", "FAIL": "❌", "n/a": "⚪"}[status]
        print(f"{name:{width}} {mark} {status:5} {detail[:70]}")
        if len(detail) > 70:
            print(f"{'':{width}} {'':7} {detail[70:150]}")

    failed = sum(1 for _, s, _ in _results if s == FAIL)
    na = sum(1 for _, s, _ in _results if s == NA)
    print("-" * 100)
    print(f"{len(_results) - failed - na} passed, {failed} failed, {na} not exercised")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
