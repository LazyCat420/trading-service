#!/usr/bin/env python3
"""Per-agent accuracy scorecard — the measurement target for agent tuning.

Every V3 agent has telemetry (latency, loops, a heuristic `quality_score`) but
nothing scored an agent against what the market actually did. `quality_score`
grades the *shape* of an artifact, not whether it was right, so tuning against
it optimizes a proxy.

This joins the two halves that were never joined:

    decision_outcomes   — resolved P&L per (cycle_id, ticker), 7-day horizon
    shared_desk         — every agent's artifact for that same (cycle_id, ticker)

and scores each agent's directional stance against the realized move.

Metrics per agent
-----------------
hit_rate    directional calls that matched the realized direction
edge_pct    mean(stance * long-side move) — the return of blindly following
            this agent, in percent per decision. THE headline number.
brier       calibration: mean((confidence/100 - correct)^2) over decisive
            calls. Lower is better; 0.25 is what you get by saying 50% always.
conf_gap    avg confidence when right minus avg confidence when wrong. A
            well-calibrated agent is more confident when it is right; <= 0
            means its confidence carries no information.

A ±1% deadband matches outcome_tracker's WIN/LOSS thresholds: moves inside it
are FLAT, and a NEUTRAL/HOLD stance is scored correct there.

Read-only. Usage:
    python scripts/agent_scorecard.py [--since 2026-06-18] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict

# ── Stance extraction ────────────────────────────────────────────────────────
# (desk artifact key, label) -> how to read a direction out of it. Agents speak
# different dialects: analysts emit thesis_direction, decision-makers emit
# action, the debate emits winning_side.
_DIRECTION_FIELDS = ("thesis_direction", "action", "winning_side")

_DIRECTION_MAP = {
    "BULLISH": 1, "BUY": 1, "BULL": 1, "LONG": 1,
    "BEARISH": -1, "SELL": -1, "BEAR": -1, "SHORT": -1,
    "NEUTRAL": 0, "HOLD": 0, "TIE": 0, "SPLIT": 0,
}

# Pipeline order — the order these agents run in a cycle.
AGENTS: list[tuple[str, str]] = [
    ("regime_classification", "regime_engine"),
    ("desk_note", "junior_analyst"),
    ("fundamental_report", "fundamental_analyst"),
    ("quant_report", "quant_analyst"),
    ("tournament_result", "tournament_debate"),
    ("debate_judge", "debate_judge"),
    ("final_decision", "board_of_directors"),
    ("trade_decision", "decision_synthesizer"),
]

DEADBAND_PCT = 1.0


def _stance(artifact: dict) -> int | None:
    """Directional stance in {-1, 0, 1}, or None when the agent makes no
    directional claim at all (the regime engine scores factors, not a
    direction; pre-2026-07-24 desk_notes carried no direction field either)."""
    if not isinstance(artifact, dict):
        return None

    # Horizon-matched grading (2026-07-24 audit): outcomes resolve on a 7-day
    # horizon, so for the fundamental desk the gradeable claim is its
    # near_term_read — not thesis_direction, which is an explicitly
    # multi-quarter business view. Scoring a YEARS thesis against a 7-day move
    # measures the wrong thing and reads as noise no matter how good the
    # analysis is.
    read = artifact.get("near_term_read")
    if isinstance(read, dict):
        key = str(read.get("direction", "")).strip().upper()
        if key in _DIRECTION_MAP:
            return _DIRECTION_MAP[key]

    for field in _DIRECTION_FIELDS:
        raw = artifact.get(field)
        if raw is None:
            continue
        key = str(raw).strip().upper()
        if key in _DIRECTION_MAP:
            return _DIRECTION_MAP[key]

    # The junior analyst's stance lives one level down, in the catalyst_call
    # added by the 2026-07-24 audit — before that it was 0-for-53 "decisive",
    # i.e. structurally incapable of being scored.
    call = artifact.get("catalyst_call")
    if isinstance(call, dict):
        key = str(call.get("direction", "")).strip().upper()
        if key in _DIRECTION_MAP:
            return _DIRECTION_MAP[key]
    return None


def _confidence(artifact: dict) -> float | None:
    # Calibration must score confidence in the CLAIM being graded. The junior's
    # top-level `confidence` rates its findings, not its direction, so when a
    # catalyst_call is present its conviction is the honest number.
    if isinstance(artifact, dict):
        call = artifact.get("catalyst_call")
        if isinstance(call, dict) and call.get("direction") is not None:
            try:
                conviction = float(call.get("conviction"))
                if 0.0 <= conviction <= 100.0:
                    return conviction
            except (TypeError, ValueError):
                pass

    for field in ("confidence", "final_confidence"):
        val = artifact.get(field)
        if val is None:
            continue
        try:
            conf = float(val)
        except (TypeError, ValueError):
            continue
        # Some artifacts emit 0-1, most emit 0-100.
        if 0.0 < conf <= 1.0:
            conf *= 100.0
        if 0.0 <= conf <= 100.0:
            return conf
    return None


def _wilson(hits: int, n: int) -> tuple[float, float]:
    """95% Wilson interval — an honest error bar on a small sample. With n<50
    a raw hit rate reads far more precise than it is."""
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin) * 100, min(1.0, centre + margin) * 100)


def fetch_rows(since: str) -> list[dict]:
    """Resolved outcomes joined to their desk. One row per (cycle, ticker)."""
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT d.cycle_id, d.ticker, d.action, d.confidence,
                   d.pnl_pct, d.outcome, d.created_at, s.desk_data
            FROM decision_outcomes d
            JOIN shared_desk s
              ON s.cycle_id = d.cycle_id AND s.ticker = d.ticker
            WHERE d.resolved_at IS NOT NULL
              AND d.pnl_pct IS NOT NULL
              AND d.created_at >= %s
            ORDER BY d.created_at ASC
            """,
            [since],
        ).fetchall()

    out = []
    for cycle_id, ticker, action, conf, pnl_pct, outcome, created_at, desk_data in rows:
        desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
        # decision_outcomes.pnl_pct is signed relative to the ACTION taken
        # (SELL inverts it). Undo that: every agent is scored against the same
        # long-side move, or a SELL desk and a BUY desk aren't comparable.
        move = -float(pnl_pct) if action == "SELL" else float(pnl_pct)
        out.append({
            "cycle_id": cycle_id,
            "ticker": ticker,
            "action": action,
            "confidence": conf,
            "move_pct": move,
            "outcome": outcome,
            "created_at": created_at,
            "desk": desk,
        })
    return out


def fetch_rows_from_prices(since: str, horizon: int = 7) -> list[dict]:
    """Every desk scored directly against price_history — no waiting.

    `decision_outcomes` was never the data limit, only the bookkeeping limit:
    it carries one row per *actionable* decision and is written by a separate
    resolver on a 7-day timer, which capped the scorecard at 53 samples. But a
    desk already knows its ticker and its date, and price_history knows what
    happened next. Measured 2026-07-24 that is **520 desks at +7 sessions and
    865 at +1** — a 10-16x larger sample, available immediately, and it
    includes the HOLD desks that never get an outcome row at all.

    Convention: entry is the first close on/after the desk date, exit is
    `horizon` sessions later. Applied identically to every agent, so
    cross-agent comparisons stay fair even where the fill is idealized.
    """
    from app.db.connection import get_db

    sessions = horizon + 1  # entry + horizon forward sessions
    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.cycle_id, s.ticker, s.created_at, s.desk_data
            FROM shared_desk s
            WHERE s.created_at >= %s
            ORDER BY s.created_at ASC
            """,
            [since],
        ).fetchall()

        out = []
        for cycle_id, ticker, created_at, desk_data in rows:
            desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
            prices = db.execute(
                """
                SELECT close FROM price_history
                WHERE ticker = %s AND close IS NOT NULL AND date >= %s
                ORDER BY date ASC LIMIT %s
                """,
                [ticker, created_at.date() if hasattr(created_at, "date") else created_at,
                 sessions],
            ).fetchall()
            if len(prices) < sessions:
                continue  # window hasn't closed yet
            try:
                entry = float(prices[0][0])
                exit_ = float(prices[-1][0])
            except (TypeError, ValueError):
                continue
            # NaN survives the NOT NULL filter.
            if not entry or entry != entry or exit_ != exit_:
                continue

            decision = desk.get("trade_decision") or desk.get("final_decision") or {}
            out.append({
                "cycle_id": cycle_id,
                "ticker": ticker,
                "action": decision.get("action"),
                "confidence": decision.get("confidence"),
                "move_pct": (exit_ - entry) / entry * 100.0,
                "outcome": None,
                "created_at": created_at,
                "desk": desk,
            })
    return out


def classify_executability(desk: dict, action: str | None) -> str:
    """What a decision can actually DO to the book.

    Added 2026-07-24 after this scorecard produced a badly misleading result.
    It scored every desk equally and reported the board at -0.77 edge, which
    read as "the board destroys value". It does not: **69% of scored decisions
    cannot change the book at all**.

      177  HOLD on a ticker not held   — a pure no-op
      137  SELL on a ticker not held   — policy-blocked, never executes
       88  BUY                         — real
       46  HOLD on a held position     — real (keeps exposure)
        9  SELL on a held position     — real (exits)

    Scoring an unexecutable opinion against realized prices measures nothing,
    and a "SELL" the bot cannot place dragged the whole aggregate negative.
    Judge decision-making on `consequential` rows only.
    """
    act = str(action or "").strip().upper()
    held = bool((desk.get("cycle_metadata") or {}).get("held"))
    if act == "BUY":
        return "consequential"          # opens or adds
    if act == "SELL":
        return "consequential" if held else "blocked"
    if act == "HOLD":
        return "consequential" if held else "noop"
    return "unknown"


def score_agents(rows: list[dict]) -> dict:
    stats: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "decisive": 0, "hits": 0, "misses": 0,
        "flat_calls": 0, "flat_correct": 0,
        "edge_sum": 0.0, "brier_sum": 0.0, "brier_n": 0,
        "conf_right": [], "conf_wrong": [], "no_stance": 0,
    })

    for row in rows:
        realized = row["move_pct"]
        realized_dir = 1 if realized > DEADBAND_PCT else (-1 if realized < -DEADBAND_PCT else 0)

        for key, label in AGENTS:
            artifact = row["desk"].get(key)
            if not isinstance(artifact, dict) or not artifact:
                continue
            st = stats[label]
            st["n"] += 1

            stance = _stance(artifact)

            # HOLD on a position you ALREADY OWN is not a neutral forecast —
            # it is a decision to stay long, and it is right when the position
            # rises. Scoring it as "predicts flatness" punished every correct
            # hold: measured, those 46 desks rose 9.9% on average over the
            # window and scored 0% under the flat rule. Only the decision-layer
            # agents own the position; an analyst's NEUTRAL thesis still means
            # neutral.
            if (
                stance == 0
                and label in ("board_of_directors", "decision_synthesizer")
                and str(artifact.get("action") or "").strip().upper() == "HOLD"
                and bool((row["desk"].get("cycle_metadata") or {}).get("held"))
            ):
                stance = 1

            if stance is None:
                st["no_stance"] += 1
                continue

            conf = _confidence(artifact)

            if stance == 0:
                # A NEUTRAL/HOLD call claims "nothing decisive happens".
                st["flat_calls"] += 1
                if realized_dir == 0:
                    st["flat_correct"] += 1
                continue

            # Following this agent's direction earns the signed move.
            st["edge_sum"] += stance * realized

            if realized_dir == 0:
                # Market went nowhere — a directional call is neither
                # vindicated nor refuted. Counted in n, excluded from hit rate.
                continue

            st["decisive"] += 1
            correct = stance == realized_dir
            if correct:
                st["hits"] += 1
                if conf is not None:
                    st["conf_right"].append(conf)
            else:
                st["misses"] += 1
                if conf is not None:
                    st["conf_wrong"].append(conf)

            if conf is not None:
                st["brier_sum"] += (conf / 100.0 - (1.0 if correct else 0.0)) ** 2
                st["brier_n"] += 1

    report = {}
    for label, st in stats.items():
        decisive = st["decisive"]
        hit_rate = (st["hits"] / decisive * 100) if decisive else None
        lo, hi = _wilson(st["hits"], decisive)
        directional = st["decisive"] + max(0, st["n"] - st["decisive"] - st["flat_calls"] - st["no_stance"])
        conf_right = sum(st["conf_right"]) / len(st["conf_right"]) if st["conf_right"] else None
        conf_wrong = sum(st["conf_wrong"]) / len(st["conf_wrong"]) if st["conf_wrong"] else None
        report[label] = {
            "n": st["n"],
            "no_stance": st["no_stance"],
            "directional_calls": directional,
            "decisive": decisive,
            "hit_rate": hit_rate,
            "hit_rate_ci95": [lo, hi] if decisive else None,
            "edge_pct": (st["edge_sum"] / directional) if directional else None,
            "brier": (st["brier_sum"] / st["brier_n"]) if st["brier_n"] else None,
            "flat_calls": st["flat_calls"],
            "flat_accuracy": (st["flat_correct"] / st["flat_calls"] * 100) if st["flat_calls"] else None,
            "conf_when_right": conf_right,
            "conf_when_wrong": conf_wrong,
            "conf_gap": (conf_right - conf_wrong) if (conf_right is not None and conf_wrong is not None) else None,
        }
    return report


def score_handoffs(rows: list[dict]) -> dict:
    """Does the downstream agent's override of an upstream one pay?

    The interesting failure mode is a board that discards the debate verdict,
    or a synthesizer that rubber-stamps a board contradicting the research.
    """
    pairs = [
        ("tournament_result", "final_decision", "board overrides debate"),
        ("quant_report", "final_decision", "board overrides quant"),
        ("fundamental_report", "final_decision", "board overrides fundamental"),
        ("final_decision", "trade_decision", "synthesizer overrides board"),
    ]
    out = {}
    for up_key, down_key, label in pairs:
        agree = {"n": 0, "edge": 0.0, "hits": 0, "decisive": 0}
        override = {"n": 0, "edge": 0.0, "hits": 0, "decisive": 0}
        for row in rows:
            up = _stance(row["desk"].get(up_key) or {})
            down = _stance(row["desk"].get(down_key) or {})
            if up is None or down is None:
                continue
            realized = row["move_pct"]
            realized_dir = 1 if realized > DEADBAND_PCT else (-1 if realized < -DEADBAND_PCT else 0)
            bucket = agree if up == down else override
            bucket["n"] += 1
            bucket["edge"] += down * realized
            if down != 0 and realized_dir != 0:
                bucket["decisive"] += 1
                if down == realized_dir:
                    bucket["hits"] += 1
        out[label] = {
            side: {
                "n": b["n"],
                "edge_pct": (b["edge"] / b["n"]) if b["n"] else None,
                "hit_rate": (b["hits"] / b["decisive"] * 100) if b["decisive"] else None,
                "decisive": b["decisive"],
            }
            for side, b in (("agreed", agree), ("overrode", override))
        }
    return out


def _fmt(val, spec=".1f", dash="—"):
    return dash if val is None else format(val, spec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-06-18",
                    help="Only score decisions created on/after this date (default: shared_desk history start)")
    ap.add_argument("--json", dest="json_out", help="Also write the raw report to this path")
    ap.add_argument("--source", choices=("outcomes", "price"), default="outcomes",
                    help="outcomes = resolved decision_outcomes (the original, "
                         "bookkeeping-limited sample). price = score every desk "
                         "straight from price_history — ~10x the sample, no wait.")
    ap.add_argument("--horizon", type=int, default=7,
                    help="Forward trading sessions to score over (price source only)")
    ap.add_argument("--executable-only", action="store_true",
                    help="Score ONLY decisions that can change the book. 69%% of "
                         "desks are policy-blocked SELLs on unheld tickers or "
                         "HOLDs on nothing; including them measures opinions "
                         "rather than trades and drags every aggregate negative.")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if args.source == "price":
        rows = fetch_rows_from_prices(args.since, args.horizon)
        source_label = f"desks scored on the +{args.horizon}-session move"
    else:
        rows = fetch_rows(args.since)
        source_label = "resolved decisions"
    if not rows:
        print(f"No scoreable desks since {args.since} (source={args.source}).")
        return 1

    # Executability breakdown — printed ALWAYS, because the unqualified
    # aggregate is what produced the "board destroys value" misread.
    buckets: dict[str, int] = defaultdict(int)
    for r in rows:
        r["executability"] = classify_executability(r["desk"], r.get("action"))
        buckets[r["executability"]] += 1
    total = len(rows)
    consequential = buckets.get("consequential", 0)

    if args.executable_only:
        rows = [r for r in rows if r["executability"] == "consequential"]
        if not rows:
            print("No consequential decisions in this window.")
            return 1

    report = score_agents(rows)
    handoffs = score_handoffs(rows)

    if args.source == "price":
        wins = sum(1 for r in rows if r["move_pct"] > DEADBAND_PCT)
        losses = sum(1 for r in rows if r["move_pct"] < -DEADBAND_PCT)
        flats = len(rows) - wins - losses
    else:
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        losses = sum(1 for r in rows if r["outcome"] == "LOSS")
        flats = sum(1 for r in rows if r["outcome"] in ("FLAT", "HOLD_CORRECT", "HOLD_MISS"))

    print(f"\n{'='*104}")
    print(f"AGENT SCORECARD — {len(rows)} {source_label} since {args.since} "
          f"({wins}↑ / {losses}↓ / {flats}→)"
          + ("  [CONSEQUENTIAL ONLY]" if args.executable_only else ""))
    print(f"{'='*104}")
    print(f"executability of the {total} desks in this window: "
          f"consequential {consequential} ({consequential/total*100:.0f}%) | "
          f"policy-blocked SELL {buckets.get('blocked', 0)} | "
          f"HOLD no-op {buckets.get('noop', 0)} | unknown {buckets.get('unknown', 0)}")
    if not args.executable_only and consequential < total:
        print("  ⚠ figures below INCLUDE decisions that cannot change the book. "
              "Re-run with --executable-only to judge decision quality.")

    # THE NULL HYPOTHESIS. An agent that is long in a rising tape looks
    # brilliant against zero, so "positive edge" alone means nothing. This
    # baseline is what doing nothing clever — staying long every ticker the
    # desk looked at — would have earned over the same window. Any agent whose
    # edge does not clear this line is selling beta as alpha.
    if rows:
        naive = sum(r["move_pct"] for r in rows) / len(rows)
        up = sum(1 for r in rows if r["move_pct"] > DEADBAND_PCT)
        down = sum(1 for r in rows if r["move_pct"] < -DEADBAND_PCT)
        print(f"BASELINE — always-long over the same desks: {naive:+.2f}% "
              f"(tape: {up} up / {down} down / {len(rows)-up-down} flat). "
              f"Beat THIS, not zero.")
    print(f"{'='*104}")
    print(f"{'agent':<24} {'n':>4} {'dir':>5} {'dec':>5} {'hit%':>6} {'95% CI':>14} "
          f"{'edge%':>7} {'brier':>6} {'confΔ':>6} {'flat%':>6}")
    print("-" * 104)

    for _, label in AGENTS:
        r = report.get(label)
        if not r:
            continue
        ci = r["hit_rate_ci95"]
        ci_s = f"{ci[0]:.0f}–{ci[1]:.0f}" if ci else "—"
        print(f"{label:<24} {r['n']:>4} {r['directional_calls']:>5} {r['decisive']:>5} "
              f"{_fmt(r['hit_rate']):>6} {ci_s:>14} {_fmt(r['edge_pct'], '+.2f'):>7} "
              f"{_fmt(r['brier'], '.3f'):>6} {_fmt(r['conf_gap'], '+.1f'):>6} "
              f"{_fmt(r['flat_accuracy'], '.0f'):>6}")

    print("\nno directional claim (agent emits no scoreable stance):")
    for _, label in AGENTS:
        r = report.get(label)
        if r and r["no_stance"]:
            print(f"  {label}: {r['no_stance']}/{r['n']} artifacts")

    print(f"\n{'='*104}")
    print("HANDOFF QUALITY — does overriding the upstream agent pay?")
    print(f"{'='*104}")
    for label, sides in handoffs.items():
        a, o = sides["agreed"], sides["overrode"]
        print(f"{label:<34} agreed n={a['n']:<3} edge={_fmt(a['edge_pct'], '+.2f'):>6} "
              f"hit={_fmt(a['hit_rate'], '.0f'):>4}%   |   "
              f"overrode n={o['n']:<3} edge={_fmt(o['edge_pct'], '+.2f'):>6} "
              f"hit={_fmt(o['hit_rate'], '.0f'):>4}%")

    print(f"\nedge% = mean signed move from following that agent, per decision.")
    print(f"brier: lower is better; 0.25 == uninformative 50% confidence.")
    print(f"confΔ = avg confidence when right minus when wrong; <= 0 means confidence is noise.")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"since": args.since, "n_decisions": len(rows),
                       "agents": report, "handoffs": handoffs}, f, indent=2, default=str)
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main())
