"""An explicit ticker request skips the funnel, not validation.

Passing `tickers=[...]` to start_cycle raises _ExplicitTickersPinned and jumps
the whole discovery/scoring/freshness/gatekeeper funnel -- correct, because an
operator naming a ticker means that ticker, and the funnel would otherwise
treat it as a mere candidate and replace it.

What was NOT correct is that the jump also skipped the funnel's SANITY filters.
Every discovered candidate is checked against FALSE_TICKERS (model-invented and
known-bad symbols) and is_us_tradeable; an explicitly requested one was checked
against neither, so a typo or an invented symbol reached the analysts and spent
a full desk on something that cannot be priced or traded.

Read from source: driving start_cycle needs a live DB, a scheduler and an LLM,
and the property here is structural.
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "app" / "services" / "pipeline_service.py"


def _explicit_branch() -> str:
    src = _SRC.read_text()
    start = src.index("                if tickers:")
    end = src.index("raise _ExplicitTickersPinned()", start)
    return src[start:end]


def test_the_explicit_branch_still_exists():
    """Vacuity guard — if the branch is renamed, this file must fail, not pass."""
    assert "raise _ExplicitTickersPinned()" in _SRC.read_text()
    assert _explicit_branch().strip(), "explicit-ticker branch came back empty"


def test_explicit_tickers_are_filtered_against_false_tickers():
    branch = _explicit_branch()
    assert "FALSE_TICKERS" in branch, (
        "explicit tickers no longer check FALSE_TICKERS — a model-invented "
        "symbol reaches the analysts again"
    )


def test_explicit_tickers_are_checked_for_us_tradeability():
    branch = _explicit_branch()
    assert "is_us_tradeable" in branch, (
        "explicit tickers no longer check is_us_tradeable"
    )


def test_an_all_rejected_request_raises_instead_of_falling_through():
    """The dangerous shape: every requested ticker rejected, list now empty,
    execution falls through into discovery and analyses a DIFFERENT set than
    the operator asked for. It must raise instead."""
    branch = _explicit_branch()
    assert "raise ValueError" in branch, (
        "an explicit request whose tickers are all rejected must raise; "
        "falling through silently runs discovery and analyses other tickers"
    )
    # and the raise must be guarded by the empty check, not unconditional
    assert re.search(r"if not tickers:", branch), (
        "the empty-list guard is gone"
    )
