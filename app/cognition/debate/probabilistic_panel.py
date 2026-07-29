"""Probabilistic panel — the tournament rebuild.

Replaces a 4-stage tournament (pitch → backtest filter → head-to-head → jury
majority vote) that emitted ``bull``/``bear``/``split`` and a confidence of
``avg_jury_score * 10``, cost 203s and 239k tokens per ticker (~31% of all
pipeline spend), and whose one stated justification — the jury veto — was
measured to have blocked **zero** decisions ever
(``docs/JURY_VETO_SCORECARD_2026-07-29.md``).

Four diagnosed defects in the old design, each addressed here:

1. **A categorical winner cannot be scored.** A 3-value label is a weak
   regressor against continuous P&L, cannot be Brier-scored and cannot be
   calibration-checked. Plurality voting also discards correct minority views
   (documented "consensus collapse", oracle gaps up to 32.3pp).
   → Agents emit a probability. Pooling is confidence-weighted in logit space.

2. **Adversarial role-play rewards persuasion over accuracy.** The forced
   bull/bear pattern benchmarked *worst* across nearly all datasets; adversarial
   agents cut accuracy 10–40% while raising confidence in wrong answers 30%+.
   → Nobody is assigned a side. Each agent reports what its own evidence says.

3. **The knockout stage gated nothing.** The tournament auto-saved its own
   equations as non-executable stubs, and the backtest runner passes anything
   ``unbacktestable`` straight through, so every pitch survived.
   → No equation stage. Deleted, not reimplemented.

4. **Only 3 of 4 views were independent.** ``Momentum_Quant`` and
   ``Volatility_Quant`` both mapped to the ``Technical`` filter category and
   received the identical single fact.
   → Four agents, four disjoint evidence slices, and a run whose partition
   silently fails is marked ``partitioned=False`` and treated as void.

The mechanism that makes any of this worth its tokens is **information
asymmetry**: with identical inputs, debate is a martingale (expected correctness
does not improve across rounds) and LLM errors are ~60% correlated, so N agents
on one packet produce N correlated opinions and call the agreement consensus.
Removing the asymmetry (``shared_evidence=True``) is the control that shows
whether the asymmetry is doing the work.

Two rounds, never three — the third buys agreement, not accuracy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.cognition.contracts.evidence import EvidencePacket
from app.cognition.debate.debate_coordinator import (
    _build_evidence_header,
    _cap_debate_text,
    filter_packet_for_persona,
    partition_report,
)
from app.cognition.debate.panel_math import (
    clamp_probability,
    disagreement,
    is_neutral,
    pool_probabilities,
    probability_to_action,
    probability_to_confidence,
)
from app.services.prism_agent_caller import llm, Priority
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)

HORIZON_SESSIONS = 7
MOVE_THRESHOLD_PCT = 1.0

_EVIDENCE_CHARS = 1800
_PEER_CHARS = 1400

#: Four analysts, four DISJOINT slices. The tournament had four personas over
#: three filter categories, so two of them read the same single fact — the
#: structural reason its "independent" pitches came back near-identical.
#:
#: ``filter_key`` indexes PERSONA_EVIDENCE_FILTER in debate_coordinator. The
#: temperatures are staggered deliberately: identical sampling params on a
#: shared model collapse the panel toward one answer, which is the failure this
#: design exists to avoid.
PANEL_ANALYSTS: dict[str, dict[str, Any]] = {
    "Fundamental_Analyst": {
        "filter_key": "Fundamental",
        "lens": "earnings power, margins, balance-sheet strength and what the "
                "business is worth independent of its price",
        "temperature": 0.35,
    },
    "Technical_Analyst": {
        "filter_key": "Technical",
        "lens": "price structure, trend, momentum and volatility — what the "
                "tape itself is doing",
        "temperature": 0.50,
    },
    "Macro_Analyst": {
        "filter_key": "Macro_Sentiment",
        "lens": "the macro regime, rates, sector flows and where this name sits "
                "in the cycle",
        "temperature": 0.65,
    },
    "Flow_Analyst": {
        "filter_key": "Positioning",
        "lens": "who is actually positioned — insider and congressional "
                "activity, institutional holdings, retail attention and news "
                "catalysts",
        "temperature": 0.55,
    },
}

_SYSTEM = """You are the {analyst} on a forecasting panel for {ticker}.

YOUR LENS: {lens}

You have been given ONLY the evidence relevant to your lens. Other analysts hold
different evidence and you cannot see theirs. That is deliberate: your value to
the panel is the view your slice supports, not a guess at the whole picture.

## YOUR TASK
Give a calibrated probability that {ticker} rises more than {threshold}% over the
next {horizon} trading sessions.

## HOW TO SET THE NUMBER
- 0.50 means you genuinely cannot tell from what you hold. Say it when it is true.
- 0.55-0.65 is a real but modest lean. Most honest calls land here.
- 0.70+ requires evidence in YOUR slice that specifically supports the move.
- 0.85+ should be rare. It claims you would be surprised to be wrong.
- Mirror those bands below 0.50 for a bearish view.

Do NOT hedge toward 0.5 to be safe — a panel of hedges carries no information and
is scored as such. Do NOT reach for a strong number your slice cannot support.
If your evidence is thin, the honest answer is a number near 0.5 AND a stated
reason, not a confident guess.

## YOUR EVIDENCE
{evidence}

## OUTPUT (raw JSON only — no markdown fences, start with {{ and end with }})
{{
  "probability": 0.62,
  "reasoning": "2-3 sentences. Cite the specific figure from YOUR evidence that drives the number.",
  "key_evidence": "the single most load-bearing data point you used",
  "what_would_change_my_mind": "the observation that would flip your direction"
}}"""

_REVISE = """You are the {analyst} on a forecasting panel for {ticker}.

YOUR LENS: {lens}

In round 1 you gave P(up > {threshold}% over {horizon} sessions) = {own_prob}
because: {own_reasoning}

## WHAT THE OTHER ANALYSTS SAW
They hold evidence you do not. Their reasoning is below — this is the only route
by which their private evidence can reach you.

{peer_views}

## YOUR TASK
Revise your probability, or keep it. Both are legitimate.

- Update when a peer cites evidence you did not hold and it bears on your lens.
- Do NOT update merely because you are outnumbered. Agreement is not evidence,
  and a panel that converges by deference is worth nothing.
- If a peer's reasoning conflicts with a figure you hold directly, trust yours
  and say why.

## YOUR EVIDENCE (unchanged)
{evidence}

## OUTPUT (raw JSON only)
{{
  "probability": 0.62,
  "reasoning": "what moved you, or why you held",
  "changed": true,
  "peer_that_moved_me": "analyst name, or null"
}}"""


def _parse(text: str) -> dict | None:
    """Parse an analyst response. Returns None if there is no usable probability.

    A missing/garbage probability returns None rather than defaulting to 0.5:
    an abstention and a parse failure are different events, and silently
    converting the second into the first is how a broken agent becomes an
    invisible vote for "no view".
    """
    try:
        parsed = parse_json_response(text)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or "probability" not in parsed:
        return None
    raw = parsed.get("probability")
    try:
        p = float(raw)
    except (TypeError, ValueError):
        return None
    if p != p or p in (float("inf"), float("-inf")):
        return None
    # Tolerate a model that answers in percent.
    if p > 1.0:
        if p <= 100.0:
            p = p / 100.0
        else:
            return None
    parsed["probability"] = clamp_probability(p)
    return parsed


async def _run_analyst(
    analyst: str,
    cfg: dict,
    packet: EvidencePacket,
    *,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    shared_evidence: bool,
) -> dict | None:
    """Round 1. Returns None if the analyst produced nothing usable."""
    if shared_evidence:
        # The rho=1.0 control: every analyst reads the full packet. If Brier is
        # unchanged against this, the asymmetry is not what is doing the work
        # and the panel is just an ensemble.
        view = packet
    else:
        view = filter_packet_for_persona(packet, cfg["filter_key"])

    evidence = _cap_debate_text(
        _build_evidence_header(view), _EVIDENCE_CHARS, f"panel-{analyst}")

    system = _SYSTEM.format(
        analyst=analyst.replace("_", " "), ticker=ticker, lens=cfg["lens"],
        threshold=MOVE_THRESHOLD_PCT, horizon=HORIZON_SESSIONS, evidence=evidence)

    # Prism groups conversations by (agent_id, first-user-message hash), so
    # concurrent calls with identical user text collide with a 409. Prefixing
    # with the analyst name is what keeps four parallel calls distinct.
    user = (f"As the {analyst.replace('_', ' ')}, give your calibrated "
            f"probability for {ticker}.")

    try:
        text, tokens, _ms = await llm.chat(
            system=system, user=user, temperature=cfg["temperature"],
            max_tokens=512, priority=Priority.NORMAL,
            agent_name=f"panel_{analyst.lower()}",
            ticker=ticker, cycle_id=cycle_id, bot_id=bot_id,
        )
    except Exception as e:  # noqa: BLE001 — one analyst must not kill the panel
        logger.warning("[PANEL] %s failed for %s: %s", analyst, ticker, e)
        return None

    parsed = _parse(text)
    if parsed is None:
        logger.warning("[PANEL] %s returned no usable probability for %s", analyst, ticker)
        return None

    parsed.update({"analyst": analyst, "tokens": tokens,
                   "facts_seen": len(view.structured_facts)})
    return parsed


async def _revise_analyst(
    view: dict,
    peers: list[dict],
    packet: EvidencePacket,
    cfg: dict,
    *,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    shared_evidence: bool,
) -> dict:
    """Round 2. Falls back to the round-1 view on any failure."""
    analyst = view["analyst"]
    if not peers:
        return view

    peer_text = _cap_debate_text(
        "\n\n".join(
            f"**{p['analyst'].replace('_', ' ')}** — P={p['probability']:.2f}\n"
            f"{p.get('reasoning', '')}\n"
            f"Key evidence: {p.get('key_evidence', 'n/a')}"
            for p in peers
        ),
        _PEER_CHARS, f"panel-peers-{analyst}",
    )

    source = packet if shared_evidence else filter_packet_for_persona(
        packet, cfg["filter_key"])
    evidence = _cap_debate_text(
        _build_evidence_header(source), _EVIDENCE_CHARS, f"panel-{analyst}-r2")

    system = _REVISE.format(
        analyst=analyst.replace("_", " "), ticker=ticker, lens=cfg["lens"],
        threshold=MOVE_THRESHOLD_PCT, horizon=HORIZON_SESSIONS,
        own_prob=f"{view['probability']:.2f}",
        own_reasoning=view.get("reasoning", ""),
        peer_views=peer_text, evidence=evidence)

    user = (f"As the {analyst.replace('_', ' ')}, revise or hold your "
            f"probability for {ticker} after reading your peers.")

    try:
        text, tokens, _ms = await llm.chat(
            system=system, user=user, temperature=cfg["temperature"],
            max_tokens=384, priority=Priority.NORMAL,
            agent_name=f"panel_{analyst.lower()}_r2",
            ticker=ticker, cycle_id=cycle_id, bot_id=bot_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[PANEL] %s revision failed for %s: %s", analyst, ticker, e)
        return view

    revised = _parse(text)
    if revised is None:
        return view

    return {
        **view,
        "probability": revised["probability"],
        "round1_probability": view["probability"],
        "reasoning": revised.get("reasoning", view.get("reasoning", "")),
        "changed": revised.get("changed"),
        "peer_that_moved_me": revised.get("peer_that_moved_me"),
        "tokens": view.get("tokens", 0) + tokens,
    }


async def run_probabilistic_panel(
    *,
    ticker: str,
    packet: EvidencePacket,
    cycle_id: str,
    bot_id: str = "",
    shared_evidence: bool = False,
    rounds: int = 2,
) -> dict:
    """Run the panel and return a ``tournament_result``-shaped artifact.

    ``shared_evidence=True`` is the asymmetry-off control.
    ``rounds=1`` skips revision — the no-deliberation control, which together
    with the self-consistency baseline is what decides whether deliberation is
    worth its tokens at all.
    """
    t0 = time.monotonic()
    before = dict(partition_report()["fallbacks"])

    views = [v for v in await asyncio.gather(*(
        _run_analyst(name, cfg, packet, ticker=ticker, cycle_id=cycle_id,
                     bot_id=bot_id, shared_evidence=shared_evidence)
        for name, cfg in PANEL_ANALYSTS.items()
    )) if v is not None]

    if not views:
        return _empty_result(ticker, t0, "no analyst produced a usable probability")

    round1 = [dict(v) for v in views]

    if rounds >= 2 and len(views) >= 2:
        views = list(await asyncio.gather(*(
            _revise_analyst(
                v, [p for p in views if p["analyst"] != v["analyst"]],
                packet, PANEL_ANALYSTS[v["analyst"]], ticker=ticker,
                cycle_id=cycle_id, bot_id=bot_id, shared_evidence=shared_evidence)
            for v in views
        )))

    probs = [v["probability"] for v in views]
    pooled = pool_probabilities(probs)
    spread = disagreement(probs)

    # A run whose partition silently self-disabled is not a panel — it is N
    # agents reading one packet, which is exactly the state that makes debate a
    # martingale. Recorded so the scorer can void it rather than average it in.
    after = partition_report()["fallbacks"]
    fell_back = {k: after[k] - before.get(k, 0)
                 for k in after if after[k] - before.get(k, 0) > 0}
    partitioned = (not fell_back) and not shared_evidence

    action = probability_to_action(pooled)
    confidence = probability_to_confidence(pooled)
    elapsed = round(time.monotonic() - t0, 2)

    summary = (
        f"Panel of {len(views)} on disjoint evidence: P(up>{MOVE_THRESHOLD_PCT}% "
        f"in {HORIZON_SESSIONS}d) = {pooled:.2f} (spread {spread:.2f}). "
        + "; ".join(f"{v['analyst'].split('_')[0]} {v['probability']:.2f}" for v in views)
    )
    if is_neutral(pooled):
        summary += " — pooled view is neutral; the panel has no directional read."

    return {
        "probability": round(pooled, 4),
        "action": action,
        "confidence": confidence,
        # Kept in the vocabulary agent_scorecard._DIRECTION_MAP already speaks.
        "winning_side": "bull" if action == "BUY" else ("bear" if action == "SELL" else "split"),
        "vetoed": False,      # the panel has no veto; the jury one blocked 0 decisions ever
        "risk_flags": [],
        "rationale": summary,
        "summary": summary,
        "disagreement": round(spread, 4),
        "views": views,
        "round1_views": round1,
        "partitioned": partitioned,
        "partition_fallbacks": fell_back,
        "shared_evidence_control": shared_evidence,
        "rounds": rounds,
        "analysts_responded": len(views),
        "analysts_expected": len(PANEL_ANALYSTS),
        "total_tokens": sum(int(v.get("tokens") or 0) for v in views),
        "elapsed_seconds": elapsed,
        "engine": "probabilistic_panel",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _empty_result(ticker: str, t0: float, why: str) -> dict:
    """Every analyst failed. Return a neutral, clearly-labelled artifact rather
    than a fabricated verdict — a panel that could not run must not read as a
    panel that saw no signal."""
    logger.error("[PANEL] %s produced no result: %s", ticker, why)
    return {
        "probability": 0.5, "action": "HOLD", "confidence": 0,
        "winning_side": "fallback", "vetoed": False, "risk_flags": [],
        "rationale": f"Panel unavailable: {why}",
        "summary": f"Panel unavailable: {why}",
        "disagreement": 0.0, "views": [], "round1_views": [],
        "partitioned": False, "partition_fallbacks": {},
        "shared_evidence_control": False, "rounds": 0,
        "analysts_responded": 0, "analysts_expected": len(PANEL_ANALYSTS),
        "total_tokens": 0, "elapsed_seconds": round(time.monotonic() - t0, 2),
        "engine": "probabilistic_panel", "degraded": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
