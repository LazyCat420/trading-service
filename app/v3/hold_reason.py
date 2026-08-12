"""WATCH vs AVOID — split the HOLD that means two different things.

The production book holds one position and cannot short, so for ~100% of the
names it analyses the only executable action is BUY. Every bearish thesis,
however well argued, can therefore only come out as HOLD. Measured 2026-08-07:
the Board says so itself in 58% of its HOLDs —

    "No open position and no shorting means the only executable action is
     BUY, which the research unanimously rejected"                    — CCI

So `HOLD` currently pools two opposite readings:

    WATCH   the thesis is constructive; the desk is not entering *here*
            (entry quality, timing, risk/reward, needs confirmation)
    AVOID   the thesis is negative; the desk would exit or short if it could

Pooling them is why the 94%-HOLD figure cannot be read as a measurement of
judgement. This module separates them.

REWORKED 2026-08-08 — THE SUBSTITUTE IS NOW THE PRIMARY AXIS. The first
version inferred AVOID from three deterministic negative signals. That was the
best available reading while the bear had no way to express a negative view,
but it labelled a state of mind rather than an action: a desk could be labelled
AVOID and still leave nobody able to say what to do about it. `substitute.py`
gives the bear a structured alternative, so the two labels now mean:

    AVOID   the bear named a name from the pool it would rather the desk owned
    WATCH   it was asked and did not — no better use of the capital was found

That is a distinction the desk can act on, and it is the one chapter 23 of the
documentation specifies. **The three signals are still computed on every call**
and still travel in `signals`, for two reasons: they are the only axis
available on routes where no bear runs (the delta tier is one agent), and
keeping both measurable is what makes this change reversible if the substitute
axis turns out to be worse. `basis` always says which axis produced the label.

**It changes nothing about what the desk trades.** The action, the confidence
and the policy gates are untouched; this is a label emitted alongside the
decision. That is deliberate: the confidence gate currently scores Brier 0.2592
against a 0.2527 base rate — worse than knowing nothing — so widening what the
desk may *do* before that number is validated would be acting on a signal known
to be unreliable.

NO ADDED CYCLE COST, for the same reason as `debate_frame`: every input below
is a value already computed and already on the desk — including the bear's
substitute, which the bear emitted in the artifact it was going to emit anyway.
No extra LLM call is made here, and the trigger stays auditable after the fact:
a reader can recompute why a name was labelled AVOID.

The classification is deterministic *given the desk*; one of its inputs is now
a model's answer rather than a computed number, which the first version
deliberately avoided. The trade was made knowingly — an inference about what an
agent meant is what this module replaced with what the agent actually said.
Asking the Board for a third label would still be wrong, because that edits the
prompt surface of the agent whose confidence is being measured.

Deliberately NOT handled here: a BUY the policy gate blocked. That desk wanted
to buy, so it is neither a watch nor an avoid, and `overridden_from` already
labels it — see the blocked-trade item in the open-items chapter. Adding it to
this axis would conflate "the desk declined" with "the desk was refused".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The UNHELD vocabulary — "should the desk enter?"
WATCH = "WATCH"
AVOID = "AVOID"

#: The HELD vocabulary, added 2026-08-12. A `HOLD` on a name the book OWNS was
#: being labelled `WATCH` — "the thesis is constructive; the desk is not
#: entering *here*" — which is not a statement anyone can make about capital
#: already committed. Measured over the label's whole life: **26 of 28 labelled
#: HOLDs on held names read WATCH, and the other 2 read AVOID**, which is just
#: as wrong ("do not enter" a position we are already in).
#:
#: `EXIT_SIGNALLED`, and NOT `SELL_PROPOSED`: nothing proposed a SELL. The
#: emitted action was HOLD, and there were ZERO SELL actions across 149 desks
#: in the five days this was measured. Naming a label after an event that has
#: never occurred is how a reader ends up grepping for a proposal that does not
#: exist.
KEEP = "KEEP"
EXIT_SIGNALLED = "EXIT_SIGNALLED"

#: The third state, and it is not decoration. `cycle_metadata["held"]` is
#: ABSENT when `_build_cycle_metadata`'s portfolio fetch raises — ~1 desk in
#: 149. Defaulting that to the unheld branch is precisely how "holdings
#: unknown" became "not held" in the 07-23 audit, and three unheld SELLs
#: reached the executor as silent no-ops. This label fails closed instead: it
#: says the classifier could not tell, which is a fact, rather than guessing.
UNKNOWN_POSITION = "UNKNOWN_POSITION"

#: Mirrors `debate_frame._BEARISH`. Duplicated as a NAMED constant rather than
#: imported so that widening one module's notion of "bearish" cannot silently
#: retune the other — if this changes, it must change deliberately.
_BEARISH = {"BEARISH", "SELL", "SHORT"}

#: The composite band that states the deterministic layer's own verdict. Band
#: names come from `app.quant.decision_score`.
_AVOID_BAND = "AVOID"

#: THE ARTIFACTS THAT ACTUALLY CARRY `thesis_direction`.
#:
#: Signal 3 shipped reading `final_decision`, `trade_decision` and
#: `decision_synthesis`. Measured 2026-08-12 across 149 desks: `final_decision`
#: carries the key on **0 of 141**, `trade_decision` on **0 of 105**, and
#: `decision_synthesis` **is not an artifact that exists**. The field is
#: declared on the RESEARCH artifacts — `fundamental_report` (111 of 111) and
#: `quant_report` (105 of 105) — see `artifacts.py:118` and `:279`.
#:
#: So signal 3 fired **0 times in 132 HOLDs**: a discriminator wired to three
#: carriers that never had the field. The decision-layer names are KEPT at the
#: front of the list — if a decision artifact ever states a direction it is the
#: desk's own word and outranks the research reads — but the research carriers
#: now follow them so the signal can fire at all.
#:
#: BLAST RADIUS, measured before the change: 6 of 132 HOLDs gain the signal, all
#: on UNHELD names, and exactly 1 label flips WATCH -> AVOID. This repairs a
#: dead code path; it does not move the held branch, and it was never going to.
#: `tests/unit/test_hold_reason.py::test_thesis_direction_carriers_exist` pins
#: the list against the live schemas so it cannot silently rot again.
_DIRECTION_CARRIERS = (
    "final_decision",
    "trade_decision",
    "fundamental_report",
    "quant_report",
)


def _artifact(desk: Any, name: str) -> dict:
    """Artifacts are None until their agent runs; callers want a dict."""
    value = getattr(desk, name, None)
    return value if isinstance(value, dict) else {}


def _direction(report: dict, key: str = "thesis_direction") -> str:
    return str(report.get(key) or "").strip().upper()


def _debate_winner(desk: Any) -> str:
    """The judge records the winner under either of two keys."""
    judge = _artifact(desk, "debate_judge")
    return str(
        judge.get("winning_side") or judge.get("winner") or ""
    ).strip().upper()


def resolve_held(desk: Any, held: bool | None = None) -> bool | None:
    """True / False / None — is this a name the book OWNS?

    `None` is a real answer and must survive: it means the portfolio fetch
    raised at desk-build time and nobody knows. See `UNKNOWN_POSITION`.

    NEVER reads `cycle_metadata["portfolio_context"]`. That key holds a
    formatted PROSE STRING (`orchestrator.py:3661` and `:3689`), so
    `portfolio_context.get("held")` raises `AttributeError` on a `str` — which
    `_attach_hold_reason`'s blanket `except` would swallow, dropping the label
    entirely instead of failing loudly. `cycle_metadata["position"]` is the
    structured copy, written at `orchestrator.py:3682` for exactly this use.
    """
    if isinstance(held, bool):
        return held
    meta = getattr(desk, "cycle_metadata", None)
    if not isinstance(meta, dict):
        return None
    flag = meta.get("held")
    if isinstance(flag, bool):
        return flag
    position = meta.get("position")
    if isinstance(position, dict) and isinstance(position.get("held"), bool):
        return position["held"]
    return None


def classify_hold(desk: Any, action: str | None,
                  held: bool | None = None) -> dict | None:
    """Label a HOLD. Returns None for any other action.

    Returning None rather than a default is the point: BUY and SELL are
    executable and need no sub-label, and inventing one for them would put a
    meaningless value in a column that later reads as data.

    THREE BRANCHES, because a HOLD means three different things:

        held is False  WATCH / AVOID           "should we enter?"
        held is True   KEEP / EXIT_SIGNALLED   "should we stay in?"
        held is None   UNKNOWN_POSITION        the desk could not tell

    `held` may be passed explicitly; otherwise it is resolved from the desk.
    """
    if str(action or "").strip().upper() != "HOLD":
        return None

    held = resolve_held(desk, held)
    signals: list[str] = []

    # 1. The debate reached a bearish verdict. On a book that cannot short,
    #    this is the single clearest statement that the desk is negative — it
    #    is the exact case the all-HOLD finding is about.
    winner = _debate_winner(desk)
    if winner and ("BEAR" in winner):
        signals.append("debate:bear_won")

    # 2. The deterministic layer's own verdict, which owes nothing to any
    #    model. Present even when every agent failed.
    score = desk.cycle_metadata.get("decision_score") if isinstance(
        getattr(desk, "cycle_metadata", None), dict
    ) else None
    if isinstance(score, dict) and str(score.get("band") or "").upper() == _AVOID_BAND:
        signals.append("baseline:avoid_band")

    # 3. A stated bearish direction. See `_DIRECTION_CARRIERS` — the decision
    #    artifacts are checked first and the research artifacts, which are the
    #    ones that actually declare the field, follow.
    for name in _DIRECTION_CARRIERS:
        if _direction(_artifact(desk, name)) in _BEARISH:
            signals.append(f"{name}:bearish")
            break

    # 4. THE SUBSTITUTE, which outranks all three above when it exists. The
    #    bear was shown the cycle's other names and either named one or did
    #    not; that is a statement about what the desk should DO, where the
    #    signals are statements about how it feels.
    #
    #    Only NAMED and DECLINED decide. OFF_POOL (a name the desk cannot
    #    price) and UNANSWERED (the bear ignored the question) are engagement
    #    failures, not answers — labelling from them would read a broken
    #    response as a considered one, so they fall through to the signals
    #    exactly as if no bear had run.
    from app.v3.substitute import DECLINED, NAMED, read_record

    sub = read_record(desk) or {}
    status = sub.get("status")

    # ── THE POSITION BRANCH, which outranks everything below ────────────────
    #
    # Everything after this point answers "should the desk ENTER?". That is not
    # a question about capital already committed, and answering it anyway is
    # the whole of Open Item 46.
    if held is None:
        # Fail closed. Do NOT fall through to the unheld branch: guessing
        # "not held" when holdings are unknown is the 07-23 defect.
        return {
            "hold_reason": UNKNOWN_POSITION,
            "signals": signals,
            "basis": "position_unknown",
            "held": None,
            "substitute_status": status,
            "substitute_ticker": None,
        }

    if held is True:
        # A NAMED substitute or any negative signal means the desk is carrying
        # a position its own evidence argues against — and it emitted HOLD.
        #
        # DECLINED does NOT rescue a broken thesis here, and that is the one
        # place the two branches genuinely disagree rather than just rename.
        # On the unheld branch "nothing better exists" is a real reason not to
        # act. On a held name it is not: exiting to CASH is always available on
        # a long-only book, so "no better name" says nothing about whether this
        # one should still be owned.
        exit_signals = list(signals)
        if status == NAMED:
            exit_signals.append("substitute:named")
        if exit_signals:
            return {
                "hold_reason": EXIT_SIGNALLED,
                "signals": signals,
                "basis": ("substitute:named" if status == NAMED
                          else "negative_signal"),
                "held": True,
                "substitute_status": status,
                "substitute_ticker": sub.get("ticker") if status == NAMED else None,
            }
        return {
            "hold_reason": KEEP,
            "signals": signals,
            # Same honesty as `no_negative_signal` below: KEEP is the ABSENCE
            # of an exit signal, not a positive verdict on the position. A desk
            # whose agents all failed produces KEEP, and that must not read as
            # "the desk re-underwrote this name today".
            "basis": "no_exit_signal",
            "held": True,
            "substitute_status": status,
            "substitute_ticker": None,
        }

    # ── held is False: the original entry vocabulary, unchanged ─────────────
    if status == NAMED:
        return {
            "hold_reason": AVOID,
            "signals": signals,
            "basis": "substitute:named",
            "held": False,
            "substitute_status": status,
            "substitute_ticker": sub.get("ticker"),
        }
    if status == DECLINED:
        return {
            "hold_reason": WATCH,
            "signals": signals,
            "basis": "substitute:declined",
            "held": False,
            "substitute_status": status,
            "substitute_ticker": None,
        }

    label = AVOID if signals else WATCH
    return {
        "hold_reason": label,
        "signals": signals,
        # Stated explicitly so a reader never has to infer it from an empty
        # list: WATCH is the *absence* of a negative signal, not a positive
        # constructive verdict. A desk whose agents all failed produces WATCH,
        # and that must not read as "the desk likes this name".
        "basis": "negative_signal" if signals else "no_negative_signal",
        "held": False,
        # Carried even on the fallback path so a reader can tell "no bear ran"
        # from "the bear was asked and broke" — the two look identical in the
        # label and are very different populations.
        "substitute_status": status,
        "substitute_ticker": None,
    }
