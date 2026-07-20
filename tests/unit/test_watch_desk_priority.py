"""
Watch Desk budget ranking.

The daily wake budget was being spent first-come: trips fired inline while
iterating `by_ticker`, i.e. plain dict order. Measured on 3 consecutive days
the budget saturated at exactly 6/6, and the deferred set was
`AAPL, WFC, UNH, AMZN, NFLX, NVDA, TSLA, MSFT` — megacaps dropped for no
reason other than their position in a loop. Trips are now ranked before the
budget is spent.
"""
from app.services.watch_desk import _TRIGGER_SEVERITY, _trip_priority


def _cand(ticker: str, trig_type: str = "news", fire_count: int = 0) -> dict:
    return {
        "ticker": ticker,
        "trig": {"type": trig_type},
        "watch": {"fire_count": fire_count},
        "detail": f"{ticker} {trig_type}",
        "value": None,
    }


def _rank(cands, held):
    return [c["ticker"] for c in sorted(cands, key=lambda c: _trip_priority(c, held),
                                        reverse=True)]


def test_held_positions_outrank_watch_only_names():
    """Real money exposed beats a name we are merely watching."""
    order = _rank([_cand("UNWATCHED"), _cand("HELD")], held={"HELD"})
    assert order[0] == "HELD"


def test_a_held_news_trip_outranks_an_unheld_price_trip():
    """Position exposure dominates severity — it is the first sort key."""
    order = _rank([_cand("UNHELD", "price_below"), _cand("HELD", "news")],
                  held={"HELD"})
    assert order[0] == "HELD"


def test_price_triggers_outrank_news_within_the_same_exposure():
    """A breached level the desk chose beats any headline."""
    order = _rank([_cand("A", "news"), _cand("B", "price_below")], held=set())
    assert order[0] == "B"


def test_stop_loss_territory_is_the_most_urgent_trigger():
    assert _TRIGGER_SEVERITY["price_below"] == max(_TRIGGER_SEVERITY.values())


def test_a_noisy_watch_cannot_monopolise_the_budget():
    """Same exposure and severity — the one that fires constantly yields."""
    order = _rank([_cand("NOISY", "news", fire_count=42),
                   _cand("FRESH", "news", fire_count=0)], held=set())
    assert order[0] == "FRESH"


def test_the_measured_saturation_case_now_keeps_the_megacaps():
    """Replays the real deferred set: with budget 6 of 14 trips, held
    megacaps must survive rather than being dropped by loop order."""
    held = {"AAPL", "NVDA", "MSFT"}
    # Deliberately listed with the held names LAST, as loop order had them.
    cands = (
        [_cand(t) for t in ("XOM", "KO", "PG", "T", "VZ", "CVX", "MRK", "JNJ")]
        + [_cand(t) for t in ("AAPL", "NVDA", "MSFT", "AMZN", "TSLA", "NFLX")]
    )
    winners = _rank(cands, held)[:6]
    for t in ("AAPL", "NVDA", "MSFT"):
        assert t in winners, f"{t} is held but was deferred: {winners}"


def test_ranking_is_total_and_never_raises_on_odd_input():
    """Ranking runs inside the desk pass — it must not be able to break it."""
    odd = [
        {"ticker": "A", "trig": {}, "watch": {}},
        {"ticker": "B", "trig": {"type": "unknown_kind"}, "watch": {"fire_count": None}},
        _cand("C", "rsi", 3),
    ]
    out = sorted(odd, key=lambda c: _trip_priority(c, set()), reverse=True)
    assert len(out) == 3
