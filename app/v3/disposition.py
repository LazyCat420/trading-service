"""What happens NEXT after a HOLD — the disposition, derived, not asked for.

`hold_reason` answers "which kind of HOLD is this" on two axes (enter/stay,
held/unheld). It stops there, and it has no consumer: measured 2026-08-20, the
label reaches `analysis_results.result_json` and nothing reads it. So a HOLD is
still terminal — the desk says "not now" and nothing carries that forward.

This module answers the next question — *what would have to change, and when do
we look again* — as a label with a machine-checkable follow-up attached.

DERIVED, NEVER ASKED. Every input below is a value the desk already computed
and already persisted. No LLM call is made here and no prompt is edited, for
the reason `hold_reason` gives at length: asking the Board for another label
edits the prompt surface of the agent whose confidence is being measured, and
adds a label nothing has validated. A reader can recompute any disposition from
the stored row, including retroactively over history — which is what makes the
first census possible at all.

**IT CHANGES NOTHING ABOUT WHAT THE DESK TRADES.** Action, confidence and the
policy gates are untouched. This is a label emitted beside the decision.

THE INPUTS WERE CENSUSED FIRST — `scripts/followup_report.py --since 2026-08-01
--until 2026-08-20`, production cycles only, 338 HOLDs of 377 decisions:

    policy_action ................ 316   (every one of them HOLD_NO_SIGNAL)
    estimate.stop_loss ........... 303
    estimate.dynamic_trigger ..... 239
    hold_reason .................. 174   (shipped 08-12; aborts never get one)
    hold_substitute ............... 86
    desk_note.catalyst_call ...... 145 of 145 desk notes, 70 not already priced

Quote that window CLOSED. Left open it drifts as cycles land, and a number that
moved on its own is indistinguishable from one a code change moved.

That census is not decoration. Signal 3 of `hold_reason` shipped reading three
artifacts that never carried the field and fired 0 times in 132 HOLDs; the rule
learned from it is that a discriminator cannot split on an input that is absent,
so the inputs get counted before the label is wired. Two labels below are
reachable but RARE by construction, and the report says so rather than letting a
zero read as "never happens":

  * `WATCH_ENTRY` needs a policy-blocked BUY. In the censused window every
    single policy_action was `HOLD_NO_SIGNAL` — the Board chose HOLD itself and
    no gate rewrote anything — so this label is correct at 0 there. It is not
    dead: `HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` is live in `_apply_policy_gates`
    and fired 18 times all-era.
  * `WATCH_CATALYST` outranks the trigger-shaped labels only when the desk named
    a catalyst it does NOT consider already priced in (70 of 145).
"""

from __future__ import annotations

from typing import Any

#: An unheld name the desk would enter on a condition it stated.
WATCH_ENTRY = "WATCH_ENTRY"
WATCH_BREAKOUT = "WATCH_BREAKOUT"
WATCH_PULLBACK = "WATCH_PULLBACK"
WATCH_CATALYST = "WATCH_CATALYST"

#: The decision was blocked by missing or untrustworthy evidence, not by a view.
#: This is a REPAIR task, and it is separated from the watch family because
#: "we could not tell" and "we looked and declined" are different states that a
#: single WATCH label has been pooling.
WATCH_DATA_GAP = "WATCH_DATA_GAP"

#: The thesis is negative. `AVOID_WITH_SUBSTITUTE` is the strictly better state:
#: the bear named a name from the pool it would rather the desk owned, so the
#: rejection points somewhere instead of just closing the file.
AVOID = "AVOID"
AVOID_WITH_SUBSTITUTE = "AVOID_WITH_SUBSTITUTE"

#: Held names. The vocabulary is about capital already committed, never entry.
MONITOR_POSITION = "MONITOR_POSITION"
EXIT_CANDIDATE = "EXIT_CANDIDATE"

#: Nothing is sufficiently asymmetric and nothing was named to wait for. This
#: label exists so that "no edge" stops borrowing the vocabulary of a watch that
#: is actually waiting for something. It should EXPIRE, not be monitored.
DEFER_LOW_EDGE = "DEFER_LOW_EDGE"

#: NOT A TRADING DISPOSITION — an operational incident. A pipeline that aborted
#: still writes `action: "HOLD"`, and counting those as considered non-actions
#: is how 17 aborts once read as unlabelled HOLDs. Anything that reached no
#: verdict lands here and must be excluded from decision-quality metrics, never
#: pooled with a desk that decided.
ANALYSIS_INVALID = "ANALYSIS_INVALID"

ALL = (
    WATCH_ENTRY, WATCH_BREAKOUT, WATCH_PULLBACK, WATCH_CATALYST,
    WATCH_DATA_GAP, AVOID, AVOID_WITH_SUBSTITUTE, MONITOR_POSITION,
    EXIT_CANDIDATE, DEFER_LOW_EDGE, ANALYSIS_INVALID,
)

#: The watch family — dispositions that mean "we may still buy this".
WATCH_FAMILY = frozenset({
    WATCH_ENTRY, WATCH_BREAKOUT, WATCH_PULLBACK, WATCH_CATALYST, WATCH_DATA_GAP,
})

#: Policy labels that say the evidence, not the view, stopped the trade.
_DATA_GAP_GATES = frozenset({
    "HOLD_POLICY_BLOCKED_DATA_QUALITY",
    "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA",
    "HOLD_NO_PRICE_DATA",
})

#: Policy labels that say the desk WANTED to act and was refused. `hold_reason`
#: deliberately excludes these ("the desk declined" is not "the desk was
#: refused"); here they are the clearest possible WATCH_ENTRY, because the
#: blocking condition is named and re-checkable.
_BLOCKED_GATES = frozenset({
    "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
    "HOLD_POLICY_BLOCKED_MISSING_REGIME",
    "HOLD_POLICY_BLOCKED_DEGRADED_MODEL",
    "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT",
    "HOLD_POLICY_BLOCKED_JURY_VETO",
    "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK",
})

#: Policy labels that mean no desk reached a verdict.
_INVALID_GATES = frozenset({
    "HOLD_DEGRADED_NO_DECISION",
    "HOLD_UNPARSEABLE_ACTION",
})

#: Rationale text written by the abort paths. `_attach_hold_reason` is never
#: reached on them, so the row arrives here carrying `action: "HOLD"` and no
#: label at all — indistinguishable from a real HOLD on the fields alone.
_ABORT_MARKERS = ("V3 Pipeline aborted", "Circuit breaker tripped")

#: Which way a `dynamic_trigger` points. Taken from the live vocabulary, not
#: invented: `sma_50_drop` (140), `sma_50_reclaim` (23), `rsi_14_oversold` (16),
#: `sma_50_rise` (13) and `trailing_drop` (5) are the head of the distribution
#: over the HOLD triggers in the censused window.
#:
#: WAIT-FOR-LOWER — the desk likes the name and wants a better price.
_PULLBACK_TOKENS = ("drop", "below", "oversold", "support", "pullback", "dip")
#: WAIT-FOR-CONFIRMATION — the desk wants the tape to prove the thesis first.
#: `cross` is deliberately NOT here: a cross has two directions, and
#: `sma_200_cross_above` is already caught by `above`. Including it would have
#: read the bare `sma_50_cross` as bullish on no evidence.
_BREAKOUT_TOKENS = ("reclaim", "breakout", "above", "rise", "resistance")


def classify_trigger(setup: Any) -> str | None:
    """Which side of the price a `dynamic_trigger` is waiting on.

    Returns `WATCH_PULLBACK`, `WATCH_BREAKOUT`, or None when the setup names
    neither — `sma_50_break` and `sma_200_break` are real live values and
    "break" alone does not say which way, so they come back None and fall
    through rather than being assigned to whichever branch is tested first.

    This is a question about DIRECTION only. Whether the trigger can actually
    fire is a different question with a different answer — see
    `app.trading.order_triggers.dynamic_trigger_is_evaluable`, which rejects
    every `reclaim`/`breakout` spelling as written. A quarter of the Board's
    HOLD triggers were inert by that test, disproportionately the entry-side
    ones, until `normalize_dynamic_trigger_type` began repairing the
    unambiguous ones at creation.
    Direction is still worth reading on an inert trigger: it says what the desk
    meant, which is what the disposition is about.
    """
    text = str(setup or "").strip().lower()
    if not text:
        return None
    down = any(tok in text for tok in _PULLBACK_TOKENS)
    up = any(tok in text for tok in _BREAKOUT_TOKENS)
    if down and not up:
        return WATCH_PULLBACK
    if up and not down:
        return WATCH_BREAKOUT
    # Neither, or BOTH. `sma_200_cross_above` trips two up-tokens and resolved
    # above; a setup that trips one of each is genuinely ambiguous and must say
    # nothing rather than land on whichever branch happens to be tested first.
    return None


def _trigger_setup(result: dict) -> str | None:
    est = result.get("estimate")
    dt = est.get("dynamic_trigger") if isinstance(est, dict) else None
    if isinstance(dt, dict):
        return str(dt.get("type") or "").strip() or None
    if isinstance(dt, str):
        return dt.strip() or None
    return None


def _catalyst_pending(catalyst: Any) -> bool:
    """A catalyst the desk named and does NOT think is already in the price.

    `already_priced_in` is the load-bearing half. 145 of 145 desk notes name a
    catalyst — naming one is near-universal and therefore says nothing on its
    own — while 70 mark it as not yet priced, and only those describe something
    the desk is still waiting for.
    """
    if not isinstance(catalyst, dict):
        return False
    named = str(catalyst.get("catalyst") or "").strip()
    return bool(named) and not catalyst.get("already_priced_in")


def derive_disposition(result: dict, *, catalyst: Any = None,
                       held: bool | None = None) -> dict | None:
    """What to do next about this HOLD. None for any other action.

    Returning None rather than a default for BUY/SELL is the same choice
    `classify_hold` makes: an executed decision needs no follow-up label, and
    inventing one puts a meaningless value somewhere that later reads as data.

    `catalyst` is the desk's own `desk_note.catalyst_call`, passed in rather
    than read, so this function stays pure and can be recomputed over any
    stored row. Omitting it costs only the `WATCH_CATALYST` branch.

    `held` mirrors `classify_hold`'s contract: pass it when the caller has a
    better source than the row; otherwise the row's own `hold_reason_held`
    (written since 08-12) is read, and a KEEP/EXIT_SIGNALLED label implies it.

    Precedence is deliberate and runs fail-closed first:

        1. no verdict was reached        -> ANALYSIS_INVALID
        2. the book already owns it      -> MONITOR_POSITION / EXIT_CANDIDATE
        3. the evidence was the problem  -> WATCH_DATA_GAP
        4. the desk was refused          -> WATCH_ENTRY
        5. the thesis is negative        -> AVOID[_WITH_SUBSTITUTE]
        6. it is waiting for something   -> WATCH_CATALYST / _PULLBACK / _BREAKOUT
        7. it is waiting for nothing     -> DEFER_LOW_EDGE

    Step 1 still comes before the position branch on purpose: a degraded run
    on a name the book holds is still a degraded run, and labelling it
    MONITOR_POSITION would file an incident as a considered decision. But the
    position branch outranks every ENTRY vocabulary — the 2026-08-23 audit
    found 7/111 HELD names labelled WATCH_* (entry words for a name already
    owned), because gate labels and trigger fallthroughs were consulted before
    the book was. A held name's follow-up is position review, whatever else the
    row says; the outranked signal is preserved in `disposition_basis`.
    """
    if str(result.get("action") or "").strip().upper() != "HOLD":
        return None

    policy = str(result.get("policy_action") or "").strip().upper()
    label = str(result.get("hold_reason") or "").strip().upper()
    rationale = str(result.get("rationale") or "")

    if held is None:
        row_held = result.get("hold_reason_held")
        held = row_held if isinstance(row_held, bool) else None
    if held is None and label in ("KEEP", "EXIT_SIGNALLED"):
        held = True

    # THE LABEL IS A SUMMARY; THE PAYLOAD IS THE CONTRACT. Every conditional
    # the desk stated travels out whichever branch wins, because only one label
    # can be returned and a follow-up needs all of them.
    #
    # Measured when this was first run without it: `WATCH_CATALYST` outranked
    # the price branch on 83 HOLDs, and most of those had ALSO stated a price
    # condition which the label silently swallowed — 108 rows in the window
    # carry both (78 pullback, 30 breakout).
    # A name waiting on an earnings date AND a pullback to the SMA-50 needs the
    # event review and the price trigger; keeping only the winning label would
    # have armed one of the two and lost the other without anything reporting a
    # loss.
    setup = _trigger_setup(result)
    payload = {
        "trigger_setup": setup,
        "trigger_direction": classify_trigger(setup),
        "catalyst_pending": _catalyst_pending(catalyst),
    }

    def out(disposition: str, basis: str) -> dict:
        return dict(payload, disposition=disposition, disposition_basis=basis)

    # 1. Nothing decided. Checked first and on three independent tells, because
    #    an abort's row is otherwise shaped exactly like a considered HOLD.
    if any(marker in rationale for marker in _ABORT_MARKERS):
        return out(ANALYSIS_INVALID, "pipeline:aborted")
    if policy in _INVALID_GATES:
        return out(ANALYSIS_INVALID, f"policy:{policy.lower()}")
    if label == "UNKNOWN_POSITION":
        # The classifier could not resolve the book. `hold_reason` fails closed
        # here and so does this: a follow-up armed on an unknown position could
        # arm entry triggers on a name already owned.
        return out(ANALYSIS_INVALID, "position:unknown")

    # 2. Capital already committed — outranks every entry vocabulary below.
    #    `hold_reason`'s held branch is the authority where present; the
    #    outranked signal (gate, thesis, trigger) rides in the basis so
    #    nothing is lost, only renamed to position words.
    if held:
        if label == "EXIT_SIGNALLED":
            return out(EXIT_CANDIDATE, "hold_reason:exit_signalled")
        if label == "AVOID":
            # A negative thesis on a name the book OWNS is an exit signal,
            # not an avoidance — AVOID is entry vocabulary.
            return out(EXIT_CANDIDATE, "held-outranks:hold_reason:avoid")
        if label == "KEEP":
            return out(MONITOR_POSITION, "hold_reason:keep")
        if policy in _DATA_GAP_GATES or policy in _BLOCKED_GATES:
            return out(MONITOR_POSITION, f"held-outranks:policy:{policy.lower()}")
        if payload["catalyst_pending"]:
            return out(MONITOR_POSITION, "held-outranks:desk_note:catalyst_pending")
        if payload["trigger_direction"]:
            return out(MONITOR_POSITION, f"held-outranks:trigger:{setup}")
        return out(MONITOR_POSITION, "held:no_signal")

    # 3. The evidence, not the view.
    if policy in _DATA_GAP_GATES:
        return out(WATCH_DATA_GAP, f"policy:{policy.lower()}")

    # 4. The desk wanted to act and a gate refused it. The blocking condition
    #    is named, so this is the follow-up nearest to an actual trade.
    if policy in _BLOCKED_GATES:
        return out(WATCH_ENTRY, f"policy:{policy.lower()}")

    # Rows from before the held flag existed can still carry the labels the
    # held branch owns; honour them exactly as before.
    if label == "KEEP":
        return out(MONITOR_POSITION, "hold_reason:keep")
    if label == "EXIT_SIGNALLED":
        return out(EXIT_CANDIDATE, "hold_reason:exit_signalled")

    # 5. A negative thesis on a name the desk does not own. The substitute is
    #    what makes the rejection actionable, so it earns its own label.
    if label == "AVOID":
        if str(result.get("hold_substitute") or "").strip():
            return out(AVOID_WITH_SUBSTITUTE, "hold_reason:avoid+substitute")
        return out(AVOID, "hold_reason:avoid")

    # 6/7. Constructive, or at least not negative: what is it waiting for?
    #      Reached by WATCH and by rows with no `hold_reason` at all — the
    #      delta tier and everything written before 08-12 — which is why the
    #      branch keys off the trigger rather than requiring the label.
    #      A DATED EVENT OUTRANKS A PRICE LEVEL when both are present: the
    #      event is the thing that resolves the uncertainty, and the price
    #      condition survives in the payload either way, so nothing is lost by
    #      naming the label after the event.
    if payload["catalyst_pending"]:
        return out(WATCH_CATALYST, "desk_note:catalyst_pending")

    if payload["trigger_direction"]:
        return out(payload["trigger_direction"], f"trigger:{setup}")

    # Waiting for nothing nameable. Not a watch — an expiry.
    return out(DEFER_LOW_EDGE, "no_trigger:no_catalyst")
