"""What each whiteboard section IS — the one place that decides.

WHY THIS EXISTS. Three files independently maintained overlapping lists of
section names, and nothing kept them in step:

    app/agents/whiteboard.py     `_SECTION_PRIORITY`   — 15 hand-ordered names
    app/v3/shared_desk.py        `_VALID_ARTIFACT_TYPES` — 13 typed artifacts
    app/tools/whiteboard_tools.py `_ORCHESTRATOR_SECTIONS` — 11 reserved names

Measured 2026-08-08: `bull_defense`, `delta_report` and `trade_decision` are
artifacts that were never added to `_SECTION_PRIORITY`, so they sorted after
every listed section and were cut from every board that overflowed —
`bull_defense`, 73 written, **0 ever delivered**. That is not a bug in the
ordering, it is the predictable cost of a list a new artifact type does not
automatically join.

So sections are no longer ranked by name. They are ranked by **class**, and the
class of an artifact is derived from the desk's own registry:

    COLLABORATION  only the whiteboard carries it — losing it loses the data
    DESK_CARRIED   the SharedDesk delivers it too — losing it costs only budget
    CONTROL        orchestrator plumbing, read directly, never in a summary

A new artifact type added to `_VALID_ARTIFACT_TYPES` becomes DESK_CARRIED with
no edit here, and a new collaboration section sorts ahead of the duplicates by
default. The failure mode above cannot recur.

THE MEASUREMENT THAT DRIVES THE POLICY. Over 331 boards (14 days):

    whole board as written    median 26,354 chars   max 64,277
    duplicated on the desk    median 24,413 chars   max 60,939
    whiteboard-ONLY payload   median  2,101 chars   max  7,109

The board holds ~26k and the delivery cap is 8k, so 93% of boards overflowed
and only the top 4-5 sections ever rendered. **But the part only the whiteboard
has is tiny.** Dropping the duplicates from an agent's prompt fits 331 of 331
boards inside the existing 8,000-char cap — no raise needed, and the block
gets *smaller*, not bigger. Raising the cap instead would have been the wrong
fix: 16,000 chars still delivers only 10.9% of boards whole, and 60,000 is
needed for 96%.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COLLABORATION = "collaboration"
DESK_CARRIED = "desk_carried"
CONTROL = "control"

#: Sections that exist ONLY here. No SharedDesk artifact carries them, so when
#: one is truncated away the information is gone from the cycle entirely.
#: `market_context` is the junior analyst's "MANDATORY, exactly once" hand-off;
#: it reached 0 of 39 downstream readers under the old ordering, where it sat
#: at priority 15 of 15.
_COLLABORATION_SECTIONS = frozenset({
    "market_context",
    "risk_flags",
    "signals",
    "consensus",
    "trade_plan",
})

#: Orchestrator plumbing. Read by `get_section` directly and never wanted in a
#: prose summary — it is a work queue, not analysis.
_CONTROL_SECTIONS = frozenset({"task_queue"})

#: Ordering WITHIN the collaboration class. Short by construction: this class is
#: small and every member of it renders, so the order is about readability, not
#: about who survives. Unlisted collaboration sections follow, alphabetically.
_COLLABORATION_ORDER = ("risk_flags", "signals", "market_context", "consensus", "trade_plan")

#: Ordering WITHIN the desk-carried class, most decision-relevant first. These
#: are the sections that get cut when a full board is rendered, and cutting
#: them costs only duplicated budget — the desk delivers them regardless.
_DESK_ORDER = (
    "final_decision", "trade_decision", "tournament_result", "debate_judge",
    "regime_classification", "bull_argument", "bear_rebuttal", "bull_defense",
    "quant_report", "fundamental_report", "valuation_report", "delta_report",
    "desk_note",
)


def _desk_carried() -> frozenset:
    """The artifact types the SharedDesk delivers by its own path.

    Imported lazily and never at module import time: `shared_desk` pulls in the
    v3 package, and `whiteboard` is imported from inside it. Fails OPEN to the
    hardcoded order below — a classification failure must never empty a board,
    and treating an unknown section as collaboration keeps it rendering.
    """
    try:
        from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

        return frozenset(_VALID_ARTIFACT_TYPES)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[WhiteboardSections] desk artifact registry unavailable (%s) — "
            "falling back to the static list", e,
        )
        return frozenset(_DESK_ORDER)


def classify(section: str) -> str:
    """Which class a section belongs to. Unknown names are COLLABORATION.

    Failing toward COLLABORATION is deliberate: an unrecognised section is one
    nothing else carries, so treating it as a duplicate would silently drop
    data. The cost of guessing wrong in this direction is a few hundred wasted
    characters; the cost in the other direction is the data.
    """
    s = (section or "").strip().lower()
    if s in _CONTROL_SECTIONS:
        return CONTROL
    if s in _desk_carried():
        return DESK_CARRIED
    return COLLABORATION


def sort_key(section: str):
    """Rank by CLASS first, then by position within the class.

    Collaboration always outranks a duplicate, so the block's unique payload can
    never be pushed off the end by a second copy of something the desk already
    delivered — the exact failure this module was written to end.
    """
    s = (section or "").strip().lower()
    cls = classify(s)
    if cls == COLLABORATION:
        try:
            return (0, _COLLABORATION_ORDER.index(s), "")
        except ValueError:
            return (1, 0, s)          # unlisted collaboration: after the known ones
    if cls == DESK_CARRIED:
        try:
            return (2, _DESK_ORDER.index(s), "")
        except ValueError:
            return (3, 0, s)          # a NEW artifact type: still ahead of control
    return (4, 0, s)


FULL = "full"
ANNOTATIONS_ONLY = "annotations_only"
SKIP = "skip"


def render_mode(section: str, *, for_agent_prompt: bool, has_annotations: bool) -> str:
    """How much of this section belongs in the summary being built.

    `for_agent_prompt` is the injected block every v3 agent receives. There the
    desk-carried BODIES are dropped: `get_compressed_context()` already renders
    them into their own `_KEEP` prompt block, and the second copy was 87% of
    this block's budget (280,907 of 323,080 chars over 40 boards).

    **Their ANNOTATIONS still render.** The desk carries the artifact; it does
    not carry the notes other agents wrote on it, and nothing else does either.
    324 of 518 annotations attach to `desk_note` (227) and `fundamental_report`
    (97), so dropping the body and its notes together would trade one data loss
    for another — and it would silence the exact channel the analyst prompts
    call load-bearing: *"Unwritten disagreement reads as consensus."*

    An explicit `whiteboard_read`/`whiteboard_summarize` call gets everything.
    It asked for the board, and answering with a filtered view of it would be a
    different kind of lie than the one this change fixes.
    """
    cls = classify(section)
    if cls == CONTROL:
        return SKIP
    if for_agent_prompt and cls == DESK_CARRIED:
        return ANNOTATIONS_ONLY if has_annotations else SKIP
    return FULL
