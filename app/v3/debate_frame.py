"""Adaptive debate framing — decide WHAT this desk should argue about.

The debate used to be unconditional: bull pitches, bear rebuts, judge rules,
with byte-identical prompts for every ticker in every regime. Measured
2026-08-05, that produced a bear win rate of 72-94% across 288 debates, and in
a long-only book a bear win can only become HOLD.

Turn order was one half of the problem (see `bull_defense`). This module is the
other half: a generic format applied to a specific situation argues the wrong
question. Two real cases from `cycle-v3-1785962005`:

  VNRX  below the $1 minimum bid, negative book value, ~$6.7M quarterly burn.
        The live question is SOLVENCY. Whether the growth story is nice is
        irrelevant until the company can fund itself.
  UBS   forward P/E 13.15, PEG 0.94, new $3B buyback — a thesis the desk
        BELIEVED — rejected at 0.68:1 risk/reward, 3% off the 52-week high.
        The live question is ENTRY QUALITY, not company quality.

Both were argued as "is this a buy". Framing them identically is how a
well-reasoned desk reaches the same verdict for opposite reasons.

DELIBERATELY DETERMINISTIC. Every frame fires off a value already computed and
already on the desk — a structural gate, a risk/reward ratio, a valuation
verdict, a thesis direction. No LLM call, no added cycle cost, and the trigger
is auditable after the fact: a reader can recompute why a desk argued what it
argued. An LLM framer would be one more unfalsifiable opinion in a chain that
already has enough of them.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Most desks support 2-3 live questions. Beyond that the propositions stop
#: being a focus and become a checklist, which is what the generic format
#: already was.
MAX_FRAMES = 3

#: Mirrors decision_score._MIN_RR. Duplicated as a NAMED constant rather than
#: imported so a change there cannot silently retune the debate — if the floor
#: moves, this must be updated deliberately.
_MIN_RR = 2.0

_BULLISH = {"BULLISH", "BUY", "LONG"}
_BEARISH = {"BEARISH", "SELL", "SHORT"}


def _artifact(desk: Any, name: str) -> dict:
    """Artifacts are None until their agent runs; callers want a dict."""
    value = getattr(desk, name, None)
    return value if isinstance(value, dict) else {}


def _gate_verdicts(score: dict) -> dict[str, dict]:
    gates = score.get("gates")
    if not isinstance(gates, list):
        return {}
    return {
        g.get("name"): g
        for g in gates
        if isinstance(g, dict) and g.get("name")
    }


def _direction(report: dict, key: str = "thesis_direction") -> str:
    return str(report.get(key) or "").strip().upper()


def derive_debate_frame(desk: Any) -> dict:
    """Return the propositions THIS desk should argue, ranked.

    Pure over the desk's artifacts — no I/O, no model call. Always returns at
    least one frame: a desk with nothing on it still gets the durability
    question, which is what the old unconditional debate asked.
    """
    note = _artifact(desk, "desk_note")
    fundamental = _artifact(desk, "fundamental_report")
    quant = _artifact(desk, "quant_report")
    valuation = _artifact(desk, "valuation_report")

    metadata = getattr(desk, "cycle_metadata", None)
    score = metadata.get("decision_score") if isinstance(metadata, dict) else None
    score = score if isinstance(score, dict) else {}
    gates = _gate_verdicts(score)

    candidates: list[tuple[int, str, str, str]] = []

    def add(priority: int, key: str, proposition: str, because: str) -> None:
        candidates.append((priority, key, proposition, because))

    # ── Solvency: nothing else matters until the company can fund itself ──
    failed_structural = [
        (name, g) for name, g in gates.items()
        if name in ("liquidity", "leverage", "profitability")
        and g.get("verdict") == "FAIL"
    ]
    if failed_structural:
        detail = "; ".join(
            f"{name} {g.get('detail', 'FAIL')}" for name, g in failed_structural
        )
        add(
            100, "SOLVENCY",
            "Can this company fund itself through the horizon without "
            "dilution, distress, or a going-concern event? A directional "
            "thesis is only live if the answer is yes — argue the balance "
            "sheet first and the story second.",
            f"structural gate FAIL — {detail}",
        )

    # ── Data sufficiency: "not enough on file" is a legitimate verdict ──
    band = str(score.get("band") or "").strip().upper()
    coverage = score.get("coverage_pct")
    gaps = [
        g for g in (
            list(note.get("data_gaps") or [])
            + list(fundamental.get("data_gaps") or [])
            + list(quant.get("data_gaps") or [])
        ) if g
    ]
    if band == "NOT_SCOREABLE" or (isinstance(coverage, (int, float)) and coverage < 40):
        add(
            90, "DATA_SUFFICIENCY",
            "Is there enough verified evidence to take a position at all? "
            "Argue whether the gaps are peripheral or load-bearing. "
            "'Insufficient data' is a legitimate verdict here, not a cop-out.",
            score.get("not_scoreable_reason")
            or f"pillar coverage {coverage}% is below the 40% line",
        )

    # ── Cross-desk disagreement: name it instead of averaging it away ──
    f_dir, q_dir = _direction(fundamental), _direction(quant)
    if (f_dir in _BULLISH and q_dir in _BEARISH) or (f_dir in _BEARISH and q_dir in _BULLISH):
        add(
            85, "DESK_DISAGREEMENT",
            f"The desks disagree on direction: fundamental reads {f_dir}, "
            f"quant reads {q_dir}. Argue WHICH READ GOVERNS for this name over "
            "the next ~7 sessions — not a compromise between them.",
            f"fundamental {f_dir} vs quant {q_dir}",
        )

    # ── Entry quality: right company, wrong price is a distinct question ──
    rr_block = score.get("risk_reward") if isinstance(score.get("risk_reward"), dict) else {}
    rr = rr_block.get("ratio")
    constructive = f_dir in _BULLISH or q_dir in _BULLISH
    if isinstance(rr, (int, float)) and rr < _MIN_RR and constructive:
        add(
            80, "ENTRY_QUALITY",
            f"The direction may be right; the ENTRY is the question. Computed "
            f"risk/reward is {rr:g}:1 against a {_MIN_RR:g}:1 floor. Argue "
            "price, stop placement and what has to happen for the reward side "
            "to improve — the company's quality is not in dispute here.",
            f"risk/reward {rr:g}:1 below the {_MIN_RR:g}:1 floor while the "
            "directional read is constructive",
        )

    # ── Valuation: is the mispricing real, or already explained? ──
    verdict = str(valuation.get("verdict") or "").strip().upper()
    mos = valuation.get("margin_of_safety_pct")
    if verdict in ("OVERVALUED", "UNDERVALUED"):
        mos_txt = f"{mos:g}%" if isinstance(mos, (int, float)) else "not computed"
        add(
            70, "VALUATION",
            f"The valuation desk calls this {verdict} (margin of safety "
            f"{mos_txt}). Argue whether that mispricing is REAL and durable, "
            "or already fully explained by the risks the market can see.",
            f"valuation verdict {verdict}",
        )

    # ── Tape: breakdown or bounce is a different argument from either ──
    metrics = quant.get("risk_metrics") if isinstance(quant.get("risk_metrics"), dict) else {}
    rsi = metrics.get("rsi")
    sma200 = str(metrics.get("sma_200_status") or "").strip().upper()
    vol_regime = str(metrics.get("volatility_regime") or "").strip().upper()
    if isinstance(rsi, (int, float)) and (rsi <= 35 or rsi >= 70):
        stretched = "oversold" if rsi <= 35 else "overbought"
        add(
            60, "TREND_VS_REVERSION",
            f"The tape is {stretched} (RSI {rsi:g}, price {sma200 or 'unknown'} "
            f"its SMA-200, volatility {vol_regime or 'unknown'}). Argue whether "
            "this is continuation to respect or exhaustion to fade. An "
            f"{stretched} reading inside a strong trend is not automatically an "
            "entry.",
            f"RSI {rsi:g} with SMA-200 status {sma200 or 'unknown'}",
        )

    # ── Catalyst: only when one was actually named ──
    catalyst = note.get("catalyst_call") if isinstance(note.get("catalyst_call"), dict) else {}
    named = str(catalyst.get("catalyst") or "").strip()
    if named and not catalyst.get("already_priced_in"):
        add(
            50, "CATALYST",
            f"The desk's catalyst is: \"{named}\". Argue whether it is real, "
            "DATED, and not already in the price — and what the position is "
            "worth if it simply does not arrive.",
            "a named catalyst the junior desk believes is not yet priced",
        )

    # ── Fallback: the question the unconditional debate always asked ──
    if not candidates:
        add(
            10, "THESIS_DURABILITY",
            "Does the bull thesis survive the strongest bear evidence on file? "
            "Argue the specific claims in the research, not the general "
            "attractiveness of the company.",
            "no sharper question was derivable from the desk",
        )

    candidates.sort(key=lambda c: -c[0])
    chosen = candidates[:MAX_FRAMES]

    frames = [
        {"key": key, "proposition": proposition, "because": because}
        for _, key, proposition, because in chosen
    ]
    return {
        "frames": frames,
        "keys": [f["key"] for f in frames],
        "considered": len(candidates),
    }


def build_debate_frame_block(ticker: str, frame: dict) -> str:
    """Render the frame for injection into the debate agents' prompts."""
    frames = (frame or {}).get("frames") or []
    if not frames:
        return ""

    lines = [
        f"## THIS DEBATE'S QUESTIONS — {ticker}",
        "",
        "Derived from THIS desk's own numbers, not a generic template. This is "
        "not an open-ended \"should we buy\" debate: argue the propositions "
        "below, in order. They are what the evidence says is actually in "
        "question for this name.",
        "",
    ]
    for i, f in enumerate(frames, 1):
        lines.append(f"{i}. **[{f['key']}]** {f['proposition']}")
        lines.append(f"   *Why this is live:* {f['because']}")
        lines.append("")
    lines.append(
        "Material outside these questions is context and belongs in your "
        "summary — it does not decide the debate. If you believe the framing "
        "itself is wrong, say so explicitly and argue why; do not quietly "
        "argue a different question."
    )
    return "\n".join(lines)
