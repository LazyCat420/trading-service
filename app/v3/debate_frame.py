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
import re
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


#: Field names a gap sentence may be ABOUT. Three desks each write their own
#: prose about the same missing column, so the raw list triple-counts: on
#: 2026-09-03 DELL's 16 gaps were 9 distinct things, with ROE/debt-to-equity
#: named three times and the short-float conflict three times.
#:
#: Word-bounded on purpose — `roa` must not match "roadmap". A sentence that
#: matches nothing keys on its own first 60 characters, so two desks phrasing
#: the same novel gap differently still count twice; that is the safe
#: direction, since the count only ever OPENS a debate.
_GAP_FIELD_KEYS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (key, re.compile(pattern))
    for key, pattern in (
        ("debt_to_equity", r"\bdebt[ _-]?to[ _-]?equity\b|\bd/e\b|\bdebt_to_equity\b"),
        ("short_float_pct", r"\bshort[ _-]?float\b|\bshort_float_pct\b"),
        ("price_to_book", r"\bp/b\b|\bprice[ _-]?to[ _-]?book\b"),
        ("forward_pe", r"\bforward[ _-]?p/?e\b"),
        ("pe_ratio", r"\b(?:ttm[ _-]?)?p/e\b|\bpe[ _-]?ratio\b"),
        ("peg_ratio", r"\bpeg\b"),
        ("current_ratio", r"\bcurrent[ _-]?ratio\b"),
        ("quick_ratio", r"\bquick[ _-]?ratio\b"),
        ("gross_margin", r"\bgross[ _-]?margin\b"),
        ("oper_margin", r"\boperating[ _-]?margin\b|\boper[ _-]?margin\b"),
        ("profit_margin", r"\bnet[ _-]?margin\b|\bprofit[ _-]?margin\b"),
        ("eps_growth_qoq", r"\beps[ _-]?growth\b|\beps_growth_qoq\b"),
        ("target_price", r"\btarget[ _-]?price\b"),
        ("reverse_dcf", r"\breverse[ _-]?dcf\b"),
        ("roic", r"\broic\b"),
        ("roe", r"\broe\b"),
        ("roa", r"\broa\b"),
    )
)

_GAP_PREFIX = re.compile(r"^\s*(?:\[(?:BLOCKING|MATERIAL|MINOR)\]\s*)?(?:datagap\s*:\s*)?",
                         re.IGNORECASE)


def distinct_data_gaps(gaps: Any) -> dict:
    """Count what is MISSING, not how many sentences mention it.

    The count used to be `len()` over the concatenated `data_gaps` lists of
    three artifacts, and it decides whether the desk argues DATA_SUFFICIENCY at
    all. Every desk writes its own prose about the same absent column, so the
    number scaled with how many analysts spoke rather than with how much was
    unknown: DELL 2026-09-03 recorded 16, of which ROE/debt-to-equity was three
    sentences, the short-float conflict three, and the reverse-DCF note two.

    `Estimate:` entries are counted separately and excluded. They are the
    quant's own working notes ("SMA-20 taken from desk notes, not recomputed"),
    written into the same list because there is nowhere else to put them; they
    are not absent data and must not open a data debate.

    Returns {"raw", "distinct", "estimates", "keys"} — the raw number is kept
    so the frame can show both and a reader can see the compression.
    """
    raw_list = [g for g in (gaps or []) if g]
    estimates = 0
    keys: list[str] = []
    seen: set[str] = set()

    for gap in raw_list:
        text = _GAP_PREFIX.sub("", str(gap)).strip().lower()
        text = " ".join(text.split())
        if not text:
            continue
        if text.startswith("estimate:"):
            estimates += 1
            continue
        # EARLIEST match wins, not first-in-table. A gap sentence names its
        # subject and then explains around it — "ROIC NOT ON FILE, moat pillar
        # relies on gross margin trend instead" is a gap about roic, and
        # table-order matching keyed it to gross_margin and merged it with the
        # genuinely separate gross-margin gap.
        hits = [
            (m.start(), i, k)
            for i, (k, pattern) in enumerate(_GAP_FIELD_KEYS)
            if (m := pattern.search(text))
        ]
        key = min(hits)[2] if hits else text[:60]
        if key not in seen:
            seen.add(key)
            keys.append(key)

    return {
        "raw": len(raw_list),
        "distinct": len(keys),
        "estimates": estimates,
        "keys": keys,
    }


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

    # ── Held position: this is an EXIT decision, not an entry one ──
    # Outranks everything else because it changes what the other questions
    # MEAN. "Is the entry good?" is not a question about capital already
    # committed. Measured 2026-08-05: every re-look of a held name reasoned in
    # entry framing ("wait for trend confirmation before re-engaging" — HOOD,
    # on a position the bot owned) and so returned HOLD, which for a held name
    # silently means KEEP. Zero SELLs in 14 days.
    position = metadata.get("position") if isinstance(metadata, dict) else None
    position = position if isinstance(position, dict) else {}
    if position.get("held") or (isinstance(metadata, dict) and metadata.get("held")):
        pnl = position.get("unrealized_pnl_pct")
        days = position.get("holding_days")
        detail = []
        if isinstance(pnl, (int, float)):
            detail.append(f"P&L {pnl:+.1f}%")
        if isinstance(days, (int, float)):
            detail.append(f"held {days:g} days")
        add(
            120, "POSITION_REVIEW",
            "WE ALREADY OWN THIS. The question is not whether to buy — it is "
            "whether the thesis that opened this position still holds. Argue "
            "KEEP versus EXIT: the Bull carries the case for keeping (or "
            "adding), the Bear carries the case for exiting. Judge the "
            "position on its thesis, not on its P&L and not on whether you "
            "would open it again today. 'Wait for confirmation' is not a "
            "verdict here — the capital is already committed either way.",
            "an open position" + (f" ({', '.join(detail)})" if detail else ""),
        )

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
    gap_stats = distinct_data_gaps(
        list(note.get("data_gaps") or [])
        + list(fundamental.get("data_gaps") or [])
        + list(quant.get("data_gaps") or [])
    )
    if (
        band == "NOT_SCOREABLE"
        or (isinstance(coverage, (int, float)) and coverage < 40)
        or gap_stats["distinct"] >= 6
    ):
        add(
            90, "DATA_SUFFICIENCY",
            "Is there enough verified evidence to take a position at all? "
            "Argue whether the gaps are peripheral or load-bearing. "
            "'Insufficient data' is a legitimate verdict here, not a cop-out.",
            score.get("not_scoreable_reason")
            or (f"pillar coverage {coverage}% is below the 40% line"
                if isinstance(coverage, (int, float)) and coverage < 40
                else (f"{gap_stats['distinct']} distinct data gaps "
                  f"({gap_stats['raw']} raw mentions across three desks"
                  + (f", {gap_stats['estimates']} estimate notes excluded"
                     if gap_stats["estimates"] else "")
                  + ")")),
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
    # Never for a name we already own: "is the entry acceptable?" is not a
    # question about capital that is already committed, and asking it is how
    # the desk talked itself into "wait before re-engaging" on a live position.
    held_now = bool(position.get("held") or (isinstance(metadata, dict) and metadata.get("held")))
    if isinstance(rr, (int, float)) and rr < _MIN_RR and constructive and not held_now:
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
        # Surfaced so a reader (and a test) can see the compression without
        # re-parsing the rendered `because` string.
        "data_gaps": gap_stats,
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
