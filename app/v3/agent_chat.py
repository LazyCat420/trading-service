"""The desk's conversation, and the one channel a human can speak into it.

WHY THIS EXISTS. The debate is the most interesting thing this system does and
it is invisible. Measured over the 7 days to 2026-08-08, the structured event
kinds that actually reached the UI were:

    agent_start 1603 · agent_done 1461 · board_convened 152 · watch_trip 47
    trade_executed 3 · contradiction_shadow 1

`debate_pitch`, `debate_clash`, `debate_vote` and `debate_verdict` fired
**zero** times. The client has had badges for all four since the War Room went
in; they are emitted only from the tournament/jury path, which no longer runs.
So the live debate — bull argues, bear rebuts, bull defends, judge rules —
reaches the operator as four one-line "agent → BULLISH @ 55%" summaries and
nothing else. What each agent actually SAID is written to `shared_desk`, and
that row does not exist until the whole ticker is finished.

Two things live here:

1. `emit_agent_message` — one structured `agent_message` event per debate turn,
   carrying the speaker, the ticker, and the text. This is what makes a
   transcript possible while the cycle is still running.

2. `pending_directives` / `mark_directives_consumed` — the inbound half. A
   human writes a directive from the UI; the next agent that builds a prompt
   for that ticker picks it up, and it is marked consumed so it lands exactly
   once.

TWO RULES THAT ARE LOAD-BEARING.

**The message field must not be named `content`, `full_text` or
`response_text`.** trading-client's SSE relay strips exactly those keys from
`event.data` before forwarding (`_HEAVY_DATA_KEYS` in its pipeline router), to
keep a 0.5s poll cheap. A transcript posted under one of those names arrives
empty at the browser and looks like an agent that said nothing.

**A directive is consumed once, by whoever reads it first.** It is not a
persistent prompt fragment. A directive that re-applied on every subsequent
agent would silently become permanent policy that nobody remembers setting —
the operator typed one sentence at the bear and would be steering the board an
hour later. Standing policy belongs in a persona, not in a chat box.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Longest agent utterance forwarded to the UI. The relay polls `/status`
#: twice a second and re-sends the whole event list on any change, so this
#: rides that payload on every tick until the cycle ends. 600 chars is a
#: readable chat paragraph; the full artifact is on the desk for anyone who
#: wants it.
MESSAGE_CHARS = 600

#: Longest directive accepted from a human. Injected verbatim into an agent
#: prompt that already runs ~12.5k tokens against a 2,048-token shed budget
#: (see the shed-overflow note in agent_runner), so this is deliberately a
#: sentence or two, not a document.
DIRECTIVE_CHARS = 1000

_TABLE_ENSURED = False


def _ensure_table() -> None:
    """Create `agent_directives` if it is not there yet.

    Same shape as the guardrail-firings table: created on first use rather
    than in a boot migration, because a failing boot migration is logged as a
    warning and the service starts anyway (see CLAUDE.md) — so a table that
    only exists if the migration ran is a table that might not exist.
    """
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    try:
        from app.db.connection import get_db

        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_directives (
                    id SERIAL PRIMARY KEY,
                    cycle_id TEXT,
                    ticker TEXT,
                    agent_name TEXT,
                    directive TEXT NOT NULL,
                    author TEXT DEFAULT 'operator',
                    status TEXT NOT NULL DEFAULT 'pending',
                    consumed_by TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    consumed_at TIMESTAMP WITH TIME ZONE
                )
            """)
            # The read is "pending directives for this cycle/ticker", run once
            # per agent prompt build — the hot path this index exists for.
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_directives_pending
                ON agent_directives (status, cycle_id, ticker)
            """)
        _TABLE_ENSURED = True
    except Exception as e:  # noqa: BLE001 — never block a cycle on this
        logger.warning("[AgentChat] could not ensure agent_directives: %s", e)


def emit_agent_message(
    emit: Any,
    *,
    speaker: str,
    ticker: str,
    text: str,
    role: str = "agent",
    stance: str | None = None,
    confidence: Any = None,
    extra: dict | None = None,
) -> None:
    """Publish one turn of the desk's conversation.

    `emit` is the orchestrator's event callback. Fail-open: a transcript is an
    observer, and an observer that can break the cycle it observes is worse
    than no transcript.
    """
    try:
        body = (text or "").strip()
        if not body:
            return
        if len(body) > MESSAGE_CHARS:
            body = body[: MESSAGE_CHARS - 1].rstrip() + "…"

        data = {
            "kind": "agent_message",
            "ticker": ticker,
            "speaker": speaker,
            "role": role,
            # NOT `content`/`full_text`/`response_text` — the client's SSE
            # relay strips those. See the module docstring.
            "message": body,
        }
        if stance:
            data["stance"] = stance
        if confidence is not None:
            data["confidence"] = confidence
        if extra:
            data.update(extra)

        emit(
            "analyzing",
            f"v3_chat_{speaker}_{ticker}",
            f"🗣️ {ticker} {speaker}: {body[:120]}",
            status="running",
            data=data,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[AgentChat] message not emitted (non-fatal): %s", e)


#: section -> (speaker label, role). Only sections whose artifact carries a
#: NARRATIVE belong here. A section is deliberately absent rather than mapped
#: to a placeholder: an empty chat line reads as an agent that had nothing to
#: say, which is a claim about the agent rather than about this table.
_SPEAKERS: dict[str, tuple[str, str]] = {
    "desk_note": ("Junior Analyst", "research"),
    "fundamental_report": ("Fundamental Analyst", "research"),
    "quant_report": ("Quant Analyst", "research"),
    "valuation_report": ("Valuation Analyst", "research"),
    "bull_argument": ("Bull", "debate"),
    "bear_rebuttal": ("Bear", "debate"),
    "bull_defense": ("Bull (defense)", "debate"),
    "debate_judge": ("Judge", "debate"),
    "final_decision": ("Board", "decision"),
    "trade_decision": ("Synthesizer", "decision"),
}


def chat_line_for(section: str, artifact: Any) -> dict | None:
    """Turn a completed artifact into one line of the transcript.

    Returns None when the section has no narrative to show — which is not a
    failure, it is most of them (regime classifications, skip markers,
    annotations).
    """
    if not isinstance(artifact, dict):
        return None
    spec = _SPEAKERS.get(section)
    if not spec:
        return None
    speaker, role = spec

    text = str(artifact.get("summary") or artifact.get("reasoning") or "").strip()
    if not text:
        return None

    # The stance each artifact type states in its own vocabulary. Read from
    # the artifact rather than inferred from the text: `winner` and
    # `thesis_direction` are the fields the desk itself acts on, so a
    # transcript that showed anything else would be showing the operator a
    # different debate than the one the Board read.
    stance = (
        artifact.get("thesis_direction")
        or artifact.get("winner")
        or artifact.get("action")
        or artifact.get("triage_recommendation")
    )
    confidence = (
        artifact.get("confidence")
        if artifact.get("confidence") is not None
        else artifact.get("final_confidence")
    )

    extra: dict = {}
    # The bear's substitute is the one field where the transcript can show an
    # operator something the summary text may not say outright: which ticker
    # it would rather own. Five states, never pooled — see substitute.py.
    pref = artifact.get("preferred_alternative")
    if isinstance(pref, dict) and pref.get("status"):
        extra["preferred_alternative"] = {
            "status": pref.get("status"),
            "ticker": pref.get("ticker"),
        }

    return {
        "speaker": speaker,
        "role": role,
        "text": text,
        "stance": stance,
        "confidence": confidence,
        "extra": extra,
    }


def pending_directives(
    *, cycle_id: str, ticker: str, agent_name: str
) -> list[dict]:
    """Directives waiting for this agent, oldest first.

    Matching is deliberately widening, not exact: a directive with no ticker
    applies to every ticker in the cycle, and one with no agent applies to
    every agent. That is what lets an operator say one thing to the whole desk
    without addressing eleven agents by name.

    A directive with no `cycle_id` applies to the CURRENT cycle only — it is
    resolved by the caller passing the live cycle_id, so a directive written
    while nothing is running does not ambush the next cycle hours later.
    """
    _ensure_table()
    try:
        from app.db.connection import get_db

        with get_db() as db:
            rows = db.execute(
                """
                SELECT id, directive, ticker, agent_name, author
                FROM agent_directives
                WHERE status = 'pending'
                  AND (cycle_id IS NULL OR cycle_id = %s)
                  AND (ticker IS NULL OR ticker = %s)
                  AND (agent_name IS NULL OR agent_name = %s)
                ORDER BY created_at
                """,
                [cycle_id, ticker, agent_name],
            ).fetchall()
        return [
            {"id": r[0], "directive": r[1], "ticker": r[2],
             "agent_name": r[3], "author": r[4]}
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("[AgentChat] directive read failed (ignored): %s", e)
        return []


def mark_directives_consumed(ids: list[int], *, consumed_by: str) -> None:
    """Retire directives that have been injected into a prompt.

    Called AFTER the prompt is built, so a directive is not burned by a run
    that then fails to start. It can therefore be delivered twice if the run
    dies between injection and this call — the honest trade, because the other
    ordering loses the directive entirely and the operator has no way to know.
    """
    if not ids:
        return
    try:
        from app.db.connection import get_db

        with get_db() as db:
            db.execute(
                """
                UPDATE agent_directives
                SET status = 'consumed', consumed_at = NOW(), consumed_by = %s
                WHERE id = ANY(%s)
                """,
                [consumed_by, list(ids)],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[AgentChat] directive retire failed (ignored): %s", e)


def directive_block(directives: list[dict]) -> str:
    """Render directives as a prompt section, or "" when there are none.

    Returns the empty string for an empty list rather than an empty header:
    a header with nothing under it reads to the model as "the operator said
    nothing, which is itself a signal", and it is not.
    """
    if not directives:
        return ""
    lines = [
        "\n## OPERATOR DIRECTIVE",
        "A human is watching this cycle and has addressed you directly. Treat "
        "this as an instruction from the desk head: it outranks your standing "
        "persona for THIS run only. If it conflicts with your risk limits or "
        "asks for something the data cannot support, say so in your artifact "
        "rather than complying silently.",
        "",
    ]
    for d in directives:
        body = (d.get("directive") or "").strip()[:DIRECTIVE_CHARS]
        if body:
            lines.append(f"- {body}")
    lines.append("")
    return "\n".join(lines)
