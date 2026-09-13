"""The cached boundary pattern must not change a single extraction.

`_boundary_search` is called ~11,500 times per article (once per
`company_registry` alias) and used to compile its pattern every time — 173,481
`re.compile` calls per 15 articles. Caching the compiled pattern is worth 2.25x
on real articles, but only if it is provably a pure speedup: this module is the
phantom-ticker defence, and a caching bug here would let "firstly" match FIRST
or "commonwealth" match COMMON, which is exactly what the word boundaries exist
to stop.

So the tests below are equivalence tests against the ORIGINAL implementation,
kept here verbatim as `_uncached_reference`, plus the boundary properties in
their own right. If the cache is ever replaced, these still hold.
"""

from __future__ import annotations

import re

import pytest

from app.processors import ticker_extractor as TE


def _uncached_reference(needle: str, haystack_lower: str):
    """The pre-2026-09-13 body of `_boundary_search`, verbatim."""
    if not needle:
        return None
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack_lower)


#: Aliases with regex metacharacters, repeats, and the substring traps the
#: docstring names. `re.escape` is the thing most likely to be lost in a
#: refactor, so half of these are only interesting if it is applied.
_NEEDLES = [
    "first", "common", "apple", "apple inc.", "at&t", "p&g", "s&p 500",
    "3m", "e*trade", "berkshire hathaway", "a", "", "c++", "f(x)",
    "coca-cola", "u.s. bancorp", "[bracket]", "back\\slash", "dot.com",
]

_HAYSTACKS = [
    "firstly the common commonwealth apple applesauce at&t p&g s&p 500 3m",
    "apple inc. beat estimates; e*trade and coca-cola fell, u.s. bancorp rose",
    "no interesting tokens here at all",
    "",
    "dot.com dotcom [bracket] back\\slash c++ f(x) 3m 33m",
    "the first quarter was common for apple",
]


@pytest.mark.parametrize("needle", _NEEDLES)
@pytest.mark.parametrize("haystack", _HAYSTACKS)
def test_cached_search_matches_the_uncached_reference(needle, haystack):
    """Same span, or same None. This is the whole safety argument."""
    got = TE._boundary_search(needle, haystack)
    want = _uncached_reference(needle, haystack)
    assert (got is None) == (want is None), (
        f"cached and reference disagree on presence for {needle!r} in {haystack!r}"
    )
    if want is not None:
        assert got.span() == want.span(), (
            f"cached and reference disagree on SPAN for {needle!r}: "
            f"{got.span()} vs {want.span()}"
        )


def test_the_fixture_is_not_vacuous():
    """Non-vacuity: the corpus must contain both matches and non-matches.

    Two equally-broken implementations agree on everything. A pair of tables
    that never match would pass the equivalence test above while proving
    nothing, which is the shape of a green test that checks nothing.
    """
    hits = sum(
        1 for n in _NEEDLES for h in _HAYSTACKS
        if _uncached_reference(n, h) is not None
    )
    misses = sum(
        1 for n in _NEEDLES for h in _HAYSTACKS
        if _uncached_reference(n, h) is None
    )
    assert hits >= 10, f"corpus finds only {hits} matches — it cannot detect a false negative"
    assert misses >= 10, f"corpus finds only {misses} non-matches — it cannot detect a false positive"


def test_the_boundary_property_itself_still_holds():
    """The defect the word boundaries exist to stop, asserted directly."""
    assert TE._boundary_search("first", "firstly the results came in") is None
    assert TE._boundary_search("common", "commonwealth bank of australia") is None
    assert TE._boundary_search("first", "first republic bank") is not None
    assert TE._boundary_search("common", "common stock issued") is not None


def test_the_cache_is_keyed_on_the_needle_not_the_haystack():
    """A cache keyed on the wrong argument returns a stale match object.

    Sabotage control: two different haystacks, one needle. If the cache ever
    memoised the RESULT instead of the PATTERN, the second call would return
    the first call's span.
    """
    a = TE._boundary_search("apple", "apple leads the tape")
    b = TE._boundary_search("apple", "the tape says apple")
    assert a is not None and b is not None
    assert a.span() == (0, 5)
    assert b.span() == (14, 19), (
        "the second search returned the first one's span — the cache is "
        "memoising results, not compiled patterns"
    )


def test_an_empty_needle_never_matches_and_is_not_cached_as_a_pattern():
    """`""` short-circuits before the cache; `re.compile("")` matches everything."""
    assert TE._boundary_search("", "anything at all") is None
    assert _uncached_reference("", "anything at all") is None


def test_the_cache_actually_caches():
    """Otherwise this is a rename, not a speedup.

    Deliberately asserts a PROPERTY (repeat lookups are free) rather than a
    pinned hit count, which would go red for being fixed.
    """
    TE._boundary_pattern.cache_clear()
    for _ in range(50):
        TE._boundary_search("apple", "apple leads the tape")
    info = TE._boundary_pattern.cache_info()
    assert info.misses == 1, f"compiled {info.misses} times for one needle"
    assert info.hits == 49, f"only {info.hits} hits across 50 identical lookups"
