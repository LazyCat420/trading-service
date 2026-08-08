"""The bear's substitute — a negative view the desk can actually execute.

WHY THIS EXISTS. `cycle_candidates.py` (58888ec) gave the bear the cycle's
other names, and **nothing required it to use them.** The surface without the
obligation changes no decision: the bear still argues "do not own this", which
on a long-only one-position book has exactly one executable reading (HOLD), and
the desk's 94% HOLD rate is unmoved. Measured over 14 days: when this desk
acted it was right 80% of the time; when it held, 32%. Holding is the expensive
half of its behaviour, and the cheapest fix is to make a bear thesis say what
the desk should own INSTEAD.

WHAT IT ENFORCES, and what it deliberately does not.

  * **A structured field, not prose.** `preferred_alternative` is a schema
    field on `bear_rebuttal`. A preference the desk cannot parse is a
    preference it cannot act on, and the previous surface proved that asking
    nicely in prose produces prose.

  * **NULL IS A REAL ANSWER.** "None of them is better" is accepted, recorded
    as `DECLINED`, and costs the artifact nothing. A validator that rejected
    null would manufacture preferences the agent does not hold — the same
    defect as the HOLD it replaces, wearing a different label. This is the one
    rule in this module that must not be tightened without a measurement.

  * **The name must come from the pool the agent was SHOWN.** A ticker from
    parametric memory is unscored, unpriced, possibly unlisted, and no agent
    has read it this cycle. It is recorded as `OFF_POOL` — never as a
    substitute — so that reaching outside stays visible in the numbers instead
    of quietly becoming an actionable preference. Fail-closed: the desk would
    rather hold than route capital at a name nothing on the desk has priced.

  * **It does not change `action`, `confidence`, or any policy gate.** Like
    `hold_reason` and `debate_frame` before it, this is a label plus an input
    to whoever decides. The confidence gate is still scoring worse than its own
    base rate (Brier 0.2592 vs 0.2527); widening what the desk may *do* while
    that number is unvalidated would be acting on a signal known to be
    unreliable.

FIVE STATES, because they are five different things and pooling them is how
the HOLD became unreadable in the first place:

    NAMED       the bear named a name from the pool — an executable preference
    DECLINED    it was asked and said none is better — a real, honest answer
    UNANSWERED  it was asked and did not answer — a coverage failure
    OFF_POOL    it named something it was not shown — unscored, not actionable
    NOT_ASKED   there was no pool (a Watch Desk wake bypasses discovery)

`NOT_ASKED` is the one that will dominate early. A wake names one ticker
explicitly and never runs discovery, so no candidate pool exists and the block
renders "". Five of the last six cycles were wakes. **A run of NOT_ASKED means
this was never exercised, NOT that the bear declines.** Reading the two as one
number is how an unexercised feature gets called inert.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

NAMED = "NAMED"
DECLINED = "DECLINED"
UNANSWERED = "UNANSWERED"
OFF_POOL = "OFF_POOL"
NOT_ASKED = "NOT_ASKED"

#: The artifact field. Named on the bear's schema and in its system prompt.
FIELD = "preferred_alternative"

#: Where the normalised record lives on `desk.cycle_metadata`, for the deciders.
_META_KEY = "bear_substitute"

#: Written by the orchestrator beside the rendered block, under the same
#: condition — so "the bear was asked" is by construction the same fact as
#: "the block rendered", and cannot drift into a second definition.
POOL_KEY = "cycle_candidate_tickers"

#: Strings a model reaches for when it means "none". Checked case-folded after
#: symbol stripping. Deliberately does NOT include "HOLD" or "NEUTRAL": those
#: are stances, and treating a stance as a declension would silently discard a
#: malformed answer as if it were an honest one.
_NULL_WORDS = frozenset({
    "", "-", "—", "none", "null", "n/a", "na", "nil", "nothing", "no",
    "no alternative", "none of them", "none of the above", "not applicable",
    "no preference", "false",
})


def _clean_ticker(raw: Any) -> str:
    """A model writes `$PLTR`, `PLTR `, `pltr` and `PLTR.US` for one thing."""
    tk = str(raw or "").strip().upper()
    tk = tk.lstrip("$").strip()
    if "." in tk:                      # PLTR.US / BRK.B — keep the root only
        tk = tk.split(".", 1)[0].strip()
    return tk


def read_pool(desk: Any) -> list[str]:
    """The tickers this desk's bear was shown. Empty means it was not asked."""
    try:
        meta = getattr(desk, "cycle_metadata", None) or {}
        pool = meta.get(POOL_KEY) or []
        return [t for t in (_clean_ticker(p) for p in pool) if t]
    except Exception:  # noqa: BLE001
        return []


def _unwrap(field: Any) -> tuple[Any, str, bool]:
    """(ticker_value, reason, answered) from the several shapes models emit.

    Accepts the object form, a bare string, and a bare null. A model that
    answers `"preferred_alternative": "PLTR"` has answered the question; making
    it re-emit an object to be heard would discard a correct answer on a
    formatting technicality.
    """
    if field is None:
        # An EXPLICIT null. Distinct from the key being absent — the agent
        # answered, and the answer was "none".
        return None, "", True
    if isinstance(field, str):
        return field, "", True
    if isinstance(field, dict):
        ticker = field.get("ticker")
        if ticker is None and "ticker" not in field:
            # An object with a reason but no ticker key at all: the agent wrote
            # about the question without answering it.
            for alt in ("symbol", "name", "alternative"):
                if alt in field:
                    ticker = field.get(alt)
                    break
        reason = str(field.get("reason") or field.get("why") or "").strip()
        return ticker, reason, True
    if isinstance(field, list):
        # "Rank the alternatives" is not what was asked, but a one-element list
        # is unambiguously an answer; anything longer is not a preference.
        if len(field) == 1:
            return _unwrap(field[0])[0], "", True
        return None, "", False
    return None, "", False


def read_substitute(artifact: Any, *, pool: list[str]) -> dict:
    """Classify the bear's answer against the pool it was actually shown.

    Never raises. Returns a record with a `status` from the five above,
    `ticker` set only when the status is NAMED, and `rejected_ticker` set only
    when the status is OFF_POOL — so no reader can mistake a name the desk
    refused for one it endorsed.
    """
    known = set(pool)
    record: dict = {
        "status": NOT_ASKED,
        "ticker": None,
        "reason": "",
        "rejected_ticker": None,
        "pool_size": len(known),
    }
    if not known:
        return record

    if not isinstance(artifact, dict) or FIELD not in artifact:
        record["status"] = UNANSWERED
        return record

    raw, reason, answered = _unwrap(artifact.get(FIELD))
    record["reason"] = reason[:600]
    if not answered:
        record["status"] = UNANSWERED
        return record

    tk = _clean_ticker(raw)
    if tk.lower() in _NULL_WORDS or raw is None:
        record["status"] = DECLINED
        return record

    if tk not in known:
        record["status"] = OFF_POOL
        record["rejected_ticker"] = tk
        return record

    record["status"] = NAMED
    record["ticker"] = tk
    return record


def canonical_field(record: dict) -> dict:
    """The normalised value written back onto the artifact.

    Written for EVERY status, including the ones that named nothing, so a
    reader of the stored artifact never has to distinguish "the agent declined"
    from "this ran before the field existed" by the absence of a key.
    """
    return {
        "ticker": record.get("ticker"),
        "reason": record.get("reason") or "",
        "status": record.get("status"),
        "rejected_ticker": record.get("rejected_ticker"),
    }


def apply_substitute(artifact: dict, *, desk: Any) -> dict:
    """Normalise the bear's answer, publish it to the deciders, return the artifact.

    NON-FATAL BY CONSTRUCTION. A label must never cost a rebuttal: every
    failure path returns the artifact unchanged. The caller runs this inside
    the artifact pipeline where an exception would discard a complete bear
    case.
    """
    if not isinstance(artifact, dict):
        return artifact
    try:
        record = read_substitute(artifact, pool=read_pool(desk))
        artifact[FIELD] = canonical_field(record)
        artifact["_substitute_status"] = record["status"]
        try:
            desk.cycle_metadata[_META_KEY] = record
        except Exception:  # noqa: BLE001
            pass  # a desk without metadata still gets the normalised artifact
        logger.info(
            "[Substitute] %s: bear %s%s (pool=%d)",
            getattr(desk, "ticker", "?"), record["status"],
            f" -> {record['ticker']}" if record.get("ticker")
            else (f" (rejected {record['rejected_ticker']})"
                  if record.get("rejected_ticker") else ""),
            record["pool_size"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[Substitute] normalisation failed (non-fatal): %s", e)
    return artifact


def read_record(desk: Any) -> dict | None:
    """The normalised record for this desk, or None if the bear has not run."""
    try:
        meta = getattr(desk, "cycle_metadata", None) or {}
        rec = meta.get(_META_KEY)
        return rec if isinstance(rec, dict) else None
    except Exception:  # noqa: BLE001
        return None


def substitute_block(record: dict | None) -> str:
    """The deciders' briefing line, or "" when there is nothing to tell them.

    Only NAMED and OFF_POOL render. `DECLINED` deliberately does not: telling
    the board "the bear was asked for an alternative and had none" reads as
    corroboration of the name, which is not what a declension means — it means
    the bear had no better idea, not that this one is good. Rendering it would
    turn an absence of information into a bullish input.
    """
    if not isinstance(record, dict):
        return ""
    status = record.get("status")

    if status == NAMED:
        why = record.get("reason") or "(no reason given)"
        return "\n".join([
            "## THE BEAR NAMED A SUBSTITUTE",
            (f"The Bear Analyst was shown this cycle's other candidates and said"
             f" the desk should own **{record['ticker']}** instead of the name"
             f" you are deciding on."),
            "",
            f"> {why}",
            "",
            ("This is the bear's negative view in its only executable form. It"
             " is a preference between two names, NOT a scored comparison: the"
             " named ticker carries a DISCOVERY screen rank and nothing more —"
             " no agent has researched it this cycle. Weigh it as one analyst's"
             " relative judgement, not as a buy recommendation for that name."),
        ])

    if status == OFF_POOL:
        return "\n".join([
            "## THE BEAR NAMED A SUBSTITUTE THE DESK CANNOT PRICE",
            (f"It preferred **{record['rejected_ticker']}**, which was not among"
             f" the candidates it was shown. The desk has no score, no price and"
             f" no research on that name this cycle, so it is recorded and NOT"
             f" treated as an alternative."),
            "",
            ("Read this as an unquantified bear signal on the name you are"
             " deciding, and nothing more."),
        ])

    return ""


def substitute_context(desk: Any) -> str:
    """The rendered block for this desk, or "" — the single read used by callers."""
    try:
        return substitute_block(read_record(desk))
    except Exception:  # noqa: BLE001
        return ""
