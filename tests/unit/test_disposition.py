"""The HOLD disposition — what happens next, derived from a stored row.

Every case here is a state observed in production between 2026-08-01 and
08-20, not an invented one; the counts in the docstrings are from that census.
"""

import pytest

from app.v3 import disposition as D


def _hold(**kw):
    """A minimal HOLD row in the shape `analysis_results.result_json` stores."""
    row = {"action": "HOLD", "policy_action": "HOLD_NO_SIGNAL"}
    est = kw.pop("estimate", None)
    row.update(kw)
    if est is not None:
        row["estimate"] = est
    return row


def _trigger(setup):
    return {"dynamic_trigger": {"type": setup, "value": 100.0}}


CATALYST = {"catalyst": "Q4 earnings 8/11 AMC", "already_priced_in": False}
PRICED_IN = {"catalyst": "Q4 earnings 8/11 AMC", "already_priced_in": True}


# ── the label is only for HOLDs ──────────────────────────────────────────────

@pytest.mark.parametrize("action", ["BUY", "SELL", "", None, "hold "])
def test_only_holds_get_a_disposition(action):
    """BUY and SELL are executable and need no follow-up label. Inventing one
    puts a meaningless value where a reader later finds data."""
    row = _hold()
    row["action"] = action
    got = D.derive_disposition(row)
    if str(action or "").strip().upper() == "HOLD":
        assert got is not None
    else:
        assert got is None


# ── fail-closed first ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rationale", [
    "V3 Pipeline aborted after 3 agent failures",
    "Circuit breaker tripped; no verdict",
])
def test_an_abort_is_an_incident_not_a_decision(rationale):
    """23 of 350 HOLDs in the window. The row is shaped exactly like a
    considered HOLD — same action, same policy_action — so the abort marker in
    the rationale is the only tell."""
    got = D.derive_disposition(_hold(rationale=rationale))
    assert got["disposition"] == D.ANALYSIS_INVALID
    assert got["disposition_basis"] == "pipeline:aborted"


def test_an_abort_outranks_a_position_label():
    """Precedence, and the reason it is ordered this way: a degraded run on a
    name the book owns is still a degraded run. Labelling it MONITOR_POSITION
    would file an incident as a considered decision."""
    got = D.derive_disposition(_hold(
        rationale="V3 Pipeline aborted", hold_reason="KEEP"))
    assert got["disposition"] == D.ANALYSIS_INVALID


def test_unknown_position_fails_closed():
    """`hold_reason` fails closed when the portfolio fetch raised, and so does
    this: a follow-up armed on an unknown book could arm ENTRY triggers on a
    name already held."""
    got = D.derive_disposition(_hold(hold_reason="UNKNOWN_POSITION"))
    assert got["disposition"] == D.ANALYSIS_INVALID


@pytest.mark.parametrize("gate", sorted(D._INVALID_GATES))
def test_gates_that_mean_no_verdict(gate):
    got = D.derive_disposition(_hold(policy_action=gate))
    assert got["disposition"] == D.ANALYSIS_INVALID


# ── the evidence vs the view ─────────────────────────────────────────────────

@pytest.mark.parametrize("gate", sorted(D._DATA_GAP_GATES))
def test_a_data_gap_is_a_repair_task(gate):
    """"We could not tell" and "we looked and declined" are different states
    that a single WATCH label was pooling."""
    got = D.derive_disposition(_hold(policy_action=gate))
    assert got["disposition"] == D.WATCH_DATA_GAP


@pytest.mark.parametrize("gate", sorted(D._BLOCKED_GATES))
def test_a_refused_buy_is_the_nearest_thing_to_a_trade(gate):
    """The desk WANTED to act. The blocking condition is named, so it is
    re-checkable — this is the follow-up closest to an actual trade, and it
    must not read the same as a Board that chose to pass."""
    got = D.derive_disposition(_hold(policy_action=gate))
    assert got["disposition"] == D.WATCH_ENTRY
    assert got["disposition_basis"] == f"policy:{gate.lower()}"


def test_a_refused_buy_outranks_the_hold_reason_label():
    got = D.derive_disposition(_hold(
        policy_action="HOLD_POLICY_BLOCKED_LOW_CONFIDENCE", hold_reason="WATCH"))
    assert got["disposition"] == D.WATCH_ENTRY


# ── the position branch ──────────────────────────────────────────────────────

def test_held_names_get_position_vocabulary():
    assert D.derive_disposition(_hold(hold_reason="KEEP"))["disposition"] \
        == D.MONITOR_POSITION
    assert D.derive_disposition(_hold(hold_reason="EXIT_SIGNALLED"))["disposition"] \
        == D.EXIT_CANDIDATE


def test_a_held_name_never_gets_entry_vocabulary():
    """The defect open item 46 was opened for: 26 of 28 labelled HOLDs on held
    names read WATCH, which is not a statement anyone can make about capital
    already committed. A trigger on the row must not drag it back."""
    for label in ("KEEP", "EXIT_SIGNALLED"):
        got = D.derive_disposition(
            _hold(hold_reason=label, estimate=_trigger("sma_50_drop")),
            catalyst=CATALYST)
        assert got["disposition"] not in D.WATCH_FAMILY


# ── the negative thesis ──────────────────────────────────────────────────────

def test_a_named_substitute_earns_its_own_label():
    """AVOID_WITH_SUBSTITUTE is strictly better than AVOID: the rejection
    points somewhere instead of just closing the file. 65 of 350."""
    got = D.derive_disposition(_hold(hold_reason="AVOID", hold_substitute="GOOG"))
    assert got["disposition"] == D.AVOID_WITH_SUBSTITUTE
    assert D.derive_disposition(_hold(hold_reason="AVOID"))["disposition"] == D.AVOID


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_a_blank_substitute_is_not_a_substitute(empty):
    got = D.derive_disposition(_hold(hold_reason="AVOID", hold_substitute=empty))
    assert got["disposition"] == D.AVOID


# ── what is it waiting for ───────────────────────────────────────────────────

@pytest.mark.parametrize("setup,expected", [
    ("sma_50_drop", D.WATCH_PULLBACK),          # 140 in the window, the mode
    ("sma_200_drop", D.WATCH_PULLBACK),
    ("rsi_14_oversold", D.WATCH_PULLBACK),
    ("trailing_drop", D.WATCH_PULLBACK),
    ("support_bounce", D.WATCH_PULLBACK),
    ("sma_50_reclaim", D.WATCH_BREAKOUT),       # 23, and INERT to the evaluator
    ("sma_50_breakout", D.WATCH_BREAKOUT),
    ("sma_200_cross_above", D.WATCH_BREAKOUT),  # two up-tokens, still one side
    ("resistance_breakout", D.WATCH_BREAKOUT),
    ("sma_50_rise", D.WATCH_BREAKOUT),
])
def test_trigger_direction(setup, expected):
    assert D.classify_trigger(setup) == expected
    got = D.derive_disposition(_hold(estimate=_trigger(setup)))
    assert got["disposition"] == expected


@pytest.mark.parametrize("ambiguous", ["sma_50_break", "sma_200_break", "sma_50_cross"])
def test_an_ambiguous_setup_says_nothing(ambiguous):
    """`break` alone does not say which way. These are real live values (8 of
    them in the window). Landing them on whichever branch is tested first would
    invent a direction the desk never stated."""
    assert D.classify_trigger(ambiguous) is None
    got = D.derive_disposition(_hold(estimate=_trigger(ambiguous)))
    assert got["disposition"] == D.DEFER_LOW_EDGE


@pytest.mark.parametrize("setup", ["", "   ", None])
def test_no_trigger_and_no_catalyst_is_an_expiry_not_a_watch(setup):
    """48 of 350. This label exists so "no edge" stops borrowing the vocabulary
    of a watch that is actually waiting for something."""
    got = D.derive_disposition(_hold(estimate=_trigger(setup)))
    assert got["disposition"] == D.DEFER_LOW_EDGE
    assert D.derive_disposition(_hold())["disposition"] == D.DEFER_LOW_EDGE


def test_a_catalyst_must_not_be_already_priced_in():
    """145 of 145 desk notes NAME a catalyst — naming one is near-universal and
    therefore says nothing on its own. `already_priced_in` is the half that
    carries the information; 70 of 145 mark it false."""
    assert D.derive_disposition(_hold(), catalyst=CATALYST)["disposition"] \
        == D.WATCH_CATALYST
    assert D.derive_disposition(_hold(), catalyst=PRICED_IN)["disposition"] \
        == D.DEFER_LOW_EDGE


@pytest.mark.parametrize("junk", [None, {}, {"catalyst": ""}, "earnings", 3])
def test_a_missing_catalyst_costs_only_that_branch(junk):
    got = D.derive_disposition(_hold(estimate=_trigger("sma_50_drop")), catalyst=junk)
    assert got["disposition"] == D.WATCH_PULLBACK


# ── THE REGRESSION: one label, but the contract keeps every condition ────────

def test_a_catalyst_does_not_swallow_the_price_condition():
    """THE BUG THIS FILE EXISTS FOR. An earlier draft returned only the winning
    label, and `WATCH_CATALYST` outranked the price branch on 85 HOLDs — 75 of
    which had ALSO stated a price condition. A name waiting on an earnings date
    AND a pullback to the SMA-50 needs the event review and the price trigger;
    keeping only the label armed one and lost the other with nothing reporting
    a loss. 115 rows in the window carry both."""
    got = D.derive_disposition(
        _hold(estimate=_trigger("sma_50_drop")), catalyst=CATALYST)
    assert got["disposition"] == D.WATCH_CATALYST      # the event names it
    assert got["trigger_direction"] == D.WATCH_PULLBACK  # ...and the price SURVIVES
    assert got["trigger_setup"] == "sma_50_drop"
    assert got["catalyst_pending"] is True


def test_every_disposition_carries_the_full_payload():
    """No branch may return a bare label — a follow-up built from one would be
    missing whatever that branch did not happen to look at."""
    rows = [
        (_hold(rationale="V3 Pipeline aborted"), None),
        (_hold(policy_action="HOLD_NO_PRICE_DATA"), None),
        (_hold(policy_action="HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"), None),
        (_hold(hold_reason="KEEP"), None),
        (_hold(hold_reason="EXIT_SIGNALLED"), None),
        (_hold(hold_reason="AVOID", hold_substitute="GOOG"), None),
        (_hold(hold_reason="AVOID"), None),
        (_hold(estimate=_trigger("sma_50_drop")), CATALYST),
        (_hold(estimate=_trigger("sma_50_reclaim")), None),
        (_hold(), None),
    ]
    seen = set()
    for row, cat in rows:
        got = D.derive_disposition(row, catalyst=cat)
        assert set(got) >= {"disposition", "disposition_basis", "trigger_setup",
                            "trigger_direction", "catalyst_pending"}, got
        seen.add(got["disposition"])
    # Ten of the eleven, each reached through the real branch that produces it.
    # WATCH_PULLBACK is the one left, covered by test_trigger_direction.
    assert len(seen) == 10, seen
    assert D.WATCH_PULLBACK not in seen and set(D.ALL) - seen == {D.WATCH_PULLBACK}


# ── the vocabulary itself ────────────────────────────────────────────────────

def test_the_label_set_is_closed_and_unique():
    """Anything this can emit must be in `ALL`, or a consumer enumerating the
    set drops the row silently — the failure mode that made a new outcome label
    cost nine consumers."""
    assert len(D.ALL) == len(set(D.ALL)) == 11
    assert D.WATCH_FAMILY <= set(D.ALL)
    assert D.ANALYSIS_INVALID not in D.WATCH_FAMILY, \
        "an incident is not a watch; pooling them is what this module undoes"


def test_gate_sets_do_not_overlap():
    """A policy label in two sets would resolve by branch order rather than by
    meaning."""
    assert not (D._DATA_GAP_GATES & D._BLOCKED_GATES)
    assert not (D._INVALID_GATES & D._BLOCKED_GATES)
    assert not (D._INVALID_GATES & D._DATA_GAP_GATES)
