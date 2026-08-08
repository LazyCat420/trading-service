"""The other names in this cycle — the desk's first cross-ticker surface.

WHY THIS EXISTS. Measured 2026-08-08: `whiteboard_read` takes `ticker` as a
REQUIRED argument, and so does `whiteboard_write`. Every read is scoped to
`(ticker, cycle_id)`, so **an agent working on ABNB cannot see that the desk
also looked at PLTR this cycle, let alone what it concluded.** The whiteboard
itself is healthy — 2,865 reads against 867 writes, agents genuinely consult
each other — it simply has no cross-ticker axis.

That gap is what makes the desk's central failure unfixable in place. Over 30
days the desk analysed 146 distinct tickers and held a position in 1, so for
~99% of names the executable menu is `{BUY, HOLD}` and every bear thesis can
only surface as HOLD. 273 of 333 HOLDs were the agent's own verdict before any
gate ran. An agent asked "is this a buy?" about one name in isolation has a
cheap correct-sounding answer, and it gives it.

Given the alternatives, the question changes from an absolute to a relative
one — "would you rather own this, or one of those?" — which is the judgement
LLMs are actually good at, and which cannot be answered "HOLD" for everything.

WHY IT IS DETERMINISTIC. `orchestrator.py:292` records that a prose fix to this
exact surface (`dcc00af`, an explicit confidence rubric in the Board prompt)
made confidence measurably WORSE — mean 63.6 -> 59.8. The pattern that works
here is a number computed in code that the agent argues with. This block adds
no model call, no fetch and no cycle cost.

WHY IT CANNOT RACE. Tickers are processed concurrently
(`pipeline_service.py:1905`, `asyncio.gather` under a semaphore), so anything
built from other tickers' *verdicts* would race or deadlock. This is built from
`top_scorers` — the scoring engine's ranked, sector-capped list, fixed BEFORE
the gatekeeper runs and therefore before any ticker starts. Every desk in the
cycle sees the same list, and it is the same list the gatekeeper chose from.
"""

from __future__ import annotations

from typing import Any, Iterable

#: How many alternatives to show. The scoring engine already caps its list at
#: 20 and 2 per sector; showing all of them costs ~250 tokens and buries the
#: ranking it is supposed to convey. Twelve is the gatekeeper's own budget.
MAX_CANDIDATES = 12

#: Fields worth carrying, and they are the ones `top_scorers` ACTUALLY has.
#:
#: NOT `band`, and NOT the fundamentals composite from `decision_scores`. Those
#: are computed per-ticker inside each pipeline and therefore do not exist for
#: the other names when this block is built — reaching for them would either
#: race the concurrent fan-out or quietly render blanks. What the scoring
#: engine produces at this point is a DISCOVERY score (relative volume, price
#: change, SMA/RSI position, an untouched-ticker bonus), and the block says so
#: rather than borrowing the authority of a word it has not earned.
_FIELDS = ("ticker", "score", "chg", "rvol", "sector")


def build_candidate_set(
    top_scorers: Iterable[dict] | None,
    sector_by_ticker: dict | None = None,
) -> list[dict]:
    """Normalise the scoring engine's output into a comparable candidate list.

    Never raises and never returns None. An empty list is a real answer — an
    explicit-ticker run (a Watch Desk wake) bypasses discovery entirely and has
    no candidate pool, and that is not a failure.
    """
    sectors = sector_by_ticker or {}
    out: list[dict] = []
    for s in list(top_scorers or [])[:MAX_CANDIDATES]:
        if not isinstance(s, dict):
            continue
        tk = str(s.get("ticker") or "").upper().strip()
        if not tk:
            continue
        row = {k: s.get(k) for k in _FIELDS}
        row["ticker"] = tk
        if not row.get("sector"):
            meta = sectors.get(tk) or {}
            row["sector"] = meta.get("sector") if isinstance(meta, dict) else meta
        out.append(row)
    return out


def shown_rows(candidates: list[dict] | None, *, self_ticker: str = "") -> list[dict]:
    """The rows this desk is actually shown — THE ONE DEFINITION OF THE POOL.

    `build_candidate_block` renders from this and `shown_tickers` counts from
    it, so the set a validator checks a named substitute against cannot drift
    from the set the agent was given. A second copy of this filter is exactly
    the defect where a bear names a ticker it was shown and the validator
    rejects it — a rejection the agent can neither see nor fix.

    `self_ticker` is excluded — the agent already has its own name's full score
    block, and listing it twice invites the model to compare a detailed read
    against a one-line summary of itself.
    """
    me = (self_ticker or "").upper().strip()
    return [c for c in (candidates or [])
            if isinstance(c, dict) and c.get("ticker") and c["ticker"] != me]


def shown_tickers(candidates: list[dict] | None, *, self_ticker: str = "") -> list[str]:
    """The tickers of `shown_rows`, in the order the agent reads them."""
    return [str(c["ticker"]) for c in shown_rows(candidates, self_ticker=self_ticker)]


def build_candidate_block(candidates: list[dict] | None, *, self_ticker: str = "") -> str:
    """The injectable briefing section, or "" when there is nothing to show.

    Returns "" rather than a header with an empty table: a block that says
    "here are the alternatives" and then lists none actively misleads, and the
    caller can simply not inject it.
    """
    rows = shown_rows(candidates, self_ticker=self_ticker)
    if not rows:
        return ""

    def _num(v, fmt):
        return format(float(v), fmt) if isinstance(v, (int, float)) else "—"

    lines = [
        "## THE OTHER NAMES THIS CYCLE",
        ("The scoring engine's ranked, sector-capped shortlist, computed in code"
         " before any agent ran. These are the alternatives the desk could own"
         " instead of the name you are analysing."),
        "",
        "| ticker | screen | chg% | rvol | sector |",
        "|---|---:|---:|---:|---|",
    ]
    for c in rows:
        lines.append(
            f"| {c['ticker']} | {_num(c.get('score'), '.1f')} "
            f"| {_num(c.get('chg'), '+.1f')} | {_num(c.get('rvol'), '.1f')} "
            f"| {str(c.get('sector') or '—')[:22]} |"
        )
    lines += [
        "",
        # Stated as a constraint on reasoning, not as an instruction to produce
        # a particular answer. The failure mode being avoided is the model
        # reading "name an alternative" as "always name an alternative" and
        # inventing a preference it does not hold — which would be the same
        # defect as the HOLD it replaces, wearing a different label.
        ("**A negative view is only actionable on this book if it names"
         " something better.** The desk is long-only and holds one position, so"
         " 'do not own this' and 'own this' are the only executable readings of"
         " your case. If your thesis is negative, say which of the names above"
         " you would rather the desk owned, and why — or say plainly that none"
         " of them is better, which is also a real answer."),
        ("`screen` is a DISCOVERY score — relative volume, price change,"
         " SMA/RSI position and an untouched-ticker bonus. It is an attention"
         " ranking, NOT a fundamental verdict, and no agent has read these"
         " names this cycle. Do not treat a high screen number as a thesis."),
    ]
    return "\n".join(lines)


def candidate_context(desk: Any) -> str:
    """The rendered block for this desk, or "" — the single read used by callers."""
    try:
        meta = getattr(desk, "cycle_metadata", None) or {}
        return meta.get("cycle_candidates_context") or ""
    except Exception:  # noqa: BLE001
        return ""
