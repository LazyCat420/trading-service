"""The fundamental baseline and its reconcile pass.

The 2026-07-28 fidelity audit found this desk emitted no numeric fields across
163 artifacts, so nothing reconciled it and the ratios in its prose were never
checked. Four of seven stated P/Es were wrong in one cycle — CARS by 83%,
because it quoted the FORWARD P/E as the trailing one. Mislabelling and
invention look identical downstream, and neither was catchable.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from app.quant.fundamental_block import (
    VERIFIED_NUMERIC_FIELDS,
    build_fundamental_block,
    latest_fundamentals,
    reconcile_fundamental_metrics,
)

# ── DELL as the store actually held it on 2026-09-03 ────────────────────────
#
# Three real rows, in the shape Mongo returns them (snapshot_date is a naive
# DATETIME, which is what killed the staleness gate). The desk read only the
# first of these and reported every ratio NOT ON FILE.
#
#   09-03  4 fields   the earnings-date stub, written mid-cycle at 19:07:27
#                     by the fundamental analyst's own get_upcoming_events call
#   09-02  41 fields  the real snapshot; price_to_book -227.55, roic absent
#   08-31  57 fields  finviz's supplement, the ONLY row carrying roic
_DELL_STUB = {
    "ticker": "DELL", "snapshot_date": datetime(2026, 9, 3),
    "source": "finnhub", "earnings_date": datetime(2026, 11, 23),
}
_DELL_FULL = {
    "ticker": "DELL", "snapshot_date": datetime(2026, 9, 2), "source": "finnhub",
    "pe_ratio": 23.1, "forward_pe": 17.56, "price_to_book": -227.55432,
    "current_ratio": 0.946, "gross_margin": 0.1909, "oper_margin": 0.0857,
    "roa": 0.0833, "roe": None, "roic": None, "debt_to_equity": None,
}
_DELL_FINVIZ = {
    "ticker": "DELL", "snapshot_date": datetime(2026, 8, 31), "source": "finviz",
    "pe_ratio": 22.4, "price_to_book": -215.91307, "roic": 0.3704,
    "current_ratio": 0.95, "roa": 0.0833,
}
_DELL_ROWS = [_DELL_STUB, _DELL_FULL, _DELL_FINVIZ]  # newest first, as sorted


def _store(rows):
    """Stand in for mongo_store.find_docs, honouring the $gte window only."""
    def find_docs(collection, query, sort=None, limit=None, **kw):
        assert collection == "fundamentals"
        out = list(rows)
        window = (query.get("snapshot_date") or {}).get("$gte")
        if window is not None:
            out = [
                r for r in out
                if (r["snapshot_date"].date()
                    if isinstance(r["snapshot_date"], datetime)
                    else r["snapshot_date"]) >= window
            ]
        return out[:limit] if limit else out
    return find_docs


class TestTheSnapshotIsCoalescedNotJustTheNewestRow:
    """Different vendors write different columns of the same table, and a
    supplement can create a row of its own. Reading one row therefore reports
    whatever the last writer happened to carry — measured over 1,152 tickers
    on 2026-09-03, the newest row lacked `roic` for 991 of them and an older
    row inside the window held it for 946."""

    def test_a_field_missing_from_the_newest_row_comes_from_an_older_one(self):
        with patch("app.db.mongo_store.find_docs", _store(_DELL_ROWS)):
            b = latest_fundamentals("DELL")

        assert b["roic"] == 0.3704
        assert b["field_as_of"]["roic"] == {
            "as_of": date(2026, 8, 31), "source": "finviz",
        }

    def test_the_newest_non_null_value_wins(self):
        """Coalescing must not resurrect a superseded number."""
        with patch("app.db.mongo_store.find_docs", _store(_DELL_ROWS)):
            b = latest_fundamentals("DELL")

        assert b["pe_ratio"] == 23.1, "the 08-31 value 22.4 must not win"
        assert "pe_ratio" not in b.get("field_as_of", {})

    def test_a_supplement_stub_does_not_become_the_snapshot(self):
        """The 4-field row is newest, but it dates nothing: it carries no
        verified field. Letting it anchor would report a row written minutes
        ago as today's fundamentals."""
        with patch("app.db.mongo_store.find_docs", _store(_DELL_ROWS)):
            b = latest_fundamentals("DELL")

        assert b["as_of"] == date(2026, 9, 2)
        assert b["source"] == "finnhub"
        # ...while the field the stub DID carry is still used.
        assert b["earnings_date"] == date(2026, 11, 23)

    def test_age_is_computed_from_a_mongo_datetime(self):
        """THE DEAD GATE: date.today() - datetime raises TypeError, the bare
        except set stale=False, and no row had ever been called stale."""
        old_row = dict(_DELL_FULL,
                       snapshot_date=datetime.now() - timedelta(days=90))
        with patch("app.db.mongo_store.find_docs", _store([old_row])):
            b = latest_fundamentals("DELL")

        assert b["age_days"] == 90
        assert b["stale"] is True

    def test_rows_outside_the_window_are_not_coalesced(self):
        """A carried-forward value is bounded by the staleness window; beyond
        it the field is honestly absent."""
        ancient = dict(_DELL_FINVIZ,
                       snapshot_date=datetime.now() - timedelta(days=100))
        recent = dict(_DELL_FULL, snapshot_date=datetime.now())
        with patch("app.db.mongo_store.find_docs", _store([recent, ancient])):
            b = latest_fundamentals("DELL")

        assert b.get("roic") is None
        assert "roic" not in b.get("field_as_of", {})

    def test_no_row_at_all_is_still_none(self):
        with patch("app.db.mongo_store.find_docs", _store([])):
            assert latest_fundamentals("NOPE") is None


class TestUndefinedIsNotMissing:
    """ROE and debt-to-equity have no meaning against negative book equity, and
    both vendors return null. Printing that as NOT ON FILE told the desk our
    collection had failed: on 2026-09-03 the bear argued DELL's "ROE and
    debt-to-equity NOT ON FILE for a second consecutive cycle" as a
    load-bearing gap, which it will be, every cycle, forever."""

    def test_negative_equity_makes_roe_and_de_not_applicable(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(price_to_book=-227.55, roe=None,
                                          debt_to_equity=None)):
            out = build_fundamental_block("DELL")

        assert "negative shareholders' equity" in out
        assert "N/A BY CONSTRUCTION" in out
        not_on_file = [ln for ln in out.splitlines() if "NOT ON FILE (report" in ln]
        assert not_on_file, out
        assert "roe" not in not_on_file[0] and "debt_to_equity" not in not_on_file[0]

    def test_a_positive_book_still_reports_roe_as_missing(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(price_to_book=3.2, roe=None,
                                          debt_to_equity=None)):
            out = build_fundamental_block("TEST")

        assert "negative shareholders' equity" not in out
        not_on_file = [ln for ln in out.splitlines() if "NOT ON FILE (report" in ln][0]
        assert "roe" in not_on_file and "debt_to_equity" in not_on_file

    def test_an_absent_price_to_book_is_not_negative_equity(self):
        """An ETF carries no ratios at all; absence must not read as negative."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(price_to_book=None, roe=None,
                                          debt_to_equity=None)):
            out = build_fundamental_block("QQQM")

        assert "negative shareholders' equity" not in out
        assert "roe" in [ln for ln in out.splitlines() if "NOT ON FILE (report" in ln][0]

    def test_a_vendor_supplied_value_prints_even_with_negative_equity(self):
        """Only ABSENCE is relabelled. A vendor that does compute a negative
        ROE is reporting a real number and it must survive."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(price_to_book=-227.55, roe=-0.85,
                                          debt_to_equity=None)):
            out = build_fundamental_block("DELL")

        assert "-85.00%" in out
        returns = [ln for ln in out.splitlines() if ln.startswith("- Returns:")][0]
        assert "N/A" not in returns


class TestCarriedForwardValuesCarryTheirDate:
    def test_the_block_names_them(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(
                       roic=0.3704,
                       field_as_of={"roic": {"as_of": date(2026, 8, 31),
                                             "source": "finviz"}})):
            out = build_fundamental_block("DELL")

        assert "CARRIED FORWARD" in out
        assert "roic" in out and "2026-08-31" in out and "finviz" in out

    def test_a_carried_field_is_not_also_reported_missing(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(
                       roic=0.3704,
                       field_as_of={"roic": {"as_of": date(2026, 8, 31),
                                             "source": "finviz"}})):
            out = build_fundamental_block("DELL")

        not_on_file = [ln for ln in out.splitlines() if "NOT ON FILE (report" in ln][0]
        assert "roic" not in not_on_file

    def test_no_carried_fields_means_no_extra_line(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            assert "CARRIED FORWARD" not in build_fundamental_block("TEST")


def _baseline(**over):
    b = {
        "ticker": "TEST", "as_of": "2026-07-28", "age_days": 0,
        "stale": False, "source": "yfinance",
        "pe_ratio": 27.99, "forward_pe": 4.83, "roe": 0.0569,
        "debt_to_equity": 0.98, "oper_margin": 0.0972,
    }
    b.update(over)
    return b


class TestTheBlockIsHonestAboutGaps:
    def test_no_row_is_stated_not_silent(self):
        """A silent empty block is indistinguishable from a healthy one
        downstream — the ASIC failure that put NO DATA into technical_baseline."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=None):
            out = build_fundamental_block("NOPE")

        assert "NO DATA ON FILE" in out
        assert out.strip() != ""

    def test_missing_fields_are_named_not_omitted(self):
        """An omitted line reads as 'not relevant'; a named gap reads as
        'unknown'. Only one of those stops the model substituting a memory."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        assert "NOT ON FILE" in out
        assert "roic" in out

    def test_earnings_absence_is_explicit(self):
        """'Binary earnings risk' is a recurring override reason on this desk
        while earnings_date was cited in 1.5% of decisions — asserted far more
        often than known."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        assert "Next earnings: NOT ON FILE" in out

    def test_stale_snapshot_says_so(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(stale=True, age_days=90)):
            out = build_fundamental_block("TEST")

        assert "STALE" in out and "90 days old" in out


class TestReconcileCatchesTheRealFailure:
    def test_the_cars_case_forward_pe_quoted_as_trailing(self):
        """CARS stated P/E 4.83 against a stored 27.99 — its forward P/E. The
        reconcile must correct it AND preserve the original, because a rate you
        cannot count is a rate you cannot fix."""
        art = {"metrics": {"pe_ratio": 4.83}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            rep = reconcile_fundamental_metrics(art, "CARS")

        assert art["metrics"]["pe_ratio"] == 27.99
        assert art["_model_reported_fundamentals"]["pe_ratio"] == 4.83
        assert rep["corrected"]["pe_ratio"]["model"] == 4.83
        assert rep["applied"] is True

    def test_an_agreeing_number_is_left_alone(self):
        art = {"metrics": {"pe_ratio": 27.99, "roe": 0.0569}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            rep = reconcile_fundamental_metrics(art, "TEST")

        assert rep["corrected"] == {}
        assert "_model_reported_fundamentals" not in art

    def test_a_stale_snapshot_does_not_overwrite_a_tool_call(self):
        """A live tool call legitimately beats a stale stored row. Record the
        disagreement, do not apply it — same rule as the valuation pass."""
        art = {"metrics": {"pe_ratio": 4.83}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(stale=True, age_days=90)):
            rep = reconcile_fundamental_metrics(art, "TEST", model_used_tools=True)

        assert art["metrics"]["pe_ratio"] == 4.83
        assert art["_unreconciled_fundamentals"]["pe_ratio"]["verified"] == 27.99
        assert rep["applied"] is False

    def test_judgment_fields_are_never_touched(self):
        """Interpretation is the analyst's actual job; this module has no
        opinion about it."""
        art = {
            "metrics": {"pe_ratio": 4.83},
            "summary": "cheap", "thesis_direction": "BULLISH",
            "confidence": 80, "pillars": {"moat": "wide"},
        }
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["summary"] == "cheap"
        assert art["thesis_direction"] == "BULLISH"
        assert art["confidence"] == 80
        assert art["pillars"] == {"moat": "wide"}

    def test_a_missing_metrics_block_is_not_an_error(self):
        """An artifact from before this field existed, or one the model omitted,
        must not raise inside the runner."""
        assert reconcile_fundamental_metrics({"summary": "x"}, "TEST") == {}
        assert reconcile_fundamental_metrics({}, "TEST") == {}

    def test_nan_is_treated_as_absent_not_as_agreement(self):
        """NaN survives NOT NULL and compares false against every threshold, so
        an unfiltered one lands in metrics looking like data."""
        art = {"metrics": {"pe_ratio": float("nan")}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["metrics"]["pe_ratio"] == 27.99


class TestTheContract:
    def test_verified_fields_are_all_rendered_or_named(self):
        """A field the reconcile enforces but the block never shows is a field
        the agent is corrected on without ever being told the right value."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        for field in VERIFIED_NUMERIC_FIELDS:
            present = _baseline().get(field) is not None
            assert (field in out) or present, f"{field} neither shown nor named"


class TestTheUnitsAreUnambiguous:
    """First live cycle (2026-07-28, SMCI): the block printed "ROE 17.88%"
    while `fundamentals.roe` stores 0.17877, and the model copied 17.88 exactly
    as instructed. The reconcile then "corrected" 8 of 8 fields at a ratio of
    precisely 100.0.

    Decisions were never wrong — every value was overwritten — but the
    fabrication RATE was destroyed, and that rate is the entire reason
    originals are preserved. Eight guaranteed false positives per ticker would
    bury any real invention.
    """

    def test_percentage_lines_state_the_value_to_copy(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            out = build_fundamental_block("TEST")

        assert "17.88%" in out          # readable
        assert "copy as 0.17877" in out  # unambiguous

    def test_the_copied_value_reconciles_clean(self):
        """The whole point: a model that follows the instruction must produce
        ZERO corrections, so a correction means something real."""
        art = {"metrics": {"roe": 0.17877}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            rep = reconcile_fundamental_metrics(art, "TEST")

        assert rep["corrected"] == {}

    def test_the_percentage_form_is_still_caught(self):
        """And a model that copies the display value is still corrected —
        the guard must not be loosened to paper over the ambiguity."""
        art = {"metrics": {"roe": 17.88}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["metrics"]["roe"] == 0.17877

    def test_the_prompt_names_the_bracket_convention(self):
        """A convention the block uses and the prompt never mentions is a
        convention the model cannot follow."""
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "copy as" in SYSTEM_PROMPT


class TestPositioningIsConsumedNotJustInjected:
    """The alt-data block was widened from 2 agents to 6 on 2026-07-28 and then
    MEASURED across a full cycle: zero of the newly-added agents cited it.
    Injection alone loses to a 7,962-char compressed desk view.

    What worked for fundamentals was three things together — block, REQUIRED
    schema field, reconcile pass — which took this desk from 0 numeric fields
    to 23 reconciled ones. This is the same shape for positioning evidence
    (30,483 congress rows, insider_trades, social_posts).
    """

    def test_invented_counts_are_corrected(self):
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {
            "insider_buy_filings_30d": 3, "congress_disclosures_90d": 99,
            "social_posts_7d": 10, "stance": "SUPPORTS_BEAR"}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 6,
                                 "social_posts_7d": 203}):
            rep = reconcile_positioning_read(art, "NVDA")

        assert art["positioning_read"]["congress_disclosures_90d"] == 6
        assert art["_model_reported_positioning"]["congress_disclosures_90d"] == 99
        assert rep["corrected"]["social_posts_7d"]["verified"] == 203

    def test_the_agents_judgment_is_never_touched(self):
        """A module that counts filings has no opinion on what they mean."""
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {
            "congress_disclosures_90d": 99, "stance": "SUPPORTS_BEAR",
            "note": "congress has been selling into strength"}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 6,
                                 "social_posts_7d": 0}):
            reconcile_positioning_read(art, "NVDA")

        assert art["positioning_read"]["stance"] == "SUPPORTS_BEAR"
        assert art["positioning_read"]["note"] == (
            "congress has been selling into strength")

    def test_counts_are_matched_exactly_not_by_tolerance(self):
        """These are integer counts of filings — 6 and 7 are different facts,
        not a rounding difference. A relative tolerance would let a wrong count
        through on any ticker with enough coverage."""
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {"congress_disclosures_90d": 7}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 6,
                                 "social_posts_7d": 0}):
            reconcile_positioning_read(art, "X")

        assert art["positioning_read"]["congress_disclosures_90d"] == 6

    def test_zero_coverage_is_an_answer_not_a_gap(self):
        """'Nobody is positioned here' is information. Treating absence as a
        gap would teach the agent to read silence as unknown."""
        from app.v3.alt_data_block import compute_positioning_facts

        # The module counts via `mongo_store.count_docs`, imported inside the
        # function, so it must be broken at its source module — the old
        # `alt_data_block.get_db` patch hit a symbol that no longer exists and
        # the "unreachable database" it claimed to simulate was the live one.
        with patch("app.db.mongo_store.count_docs", side_effect=RuntimeError):
            facts = compute_positioning_facts("NOPE")

        assert facts == {"insider_buy_filings_30d": 0,
                         "congress_disclosures_90d": 0, "social_posts_7d": 0}

    def test_a_missing_block_does_not_raise_in_the_runner(self):
        from app.v3.alt_data_block import reconcile_positioning_read

        assert reconcile_positioning_read({"summary": "x"}, "X") == {}
        assert reconcile_positioning_read({}, "X") == {}

    def test_the_prompt_requires_it_and_allows_zero(self):
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "positioning_read" in SYSTEM_PROMPT
        assert "REQUIRED" in SYSTEM_PROMPT
        assert "NO_COVERAGE" in SYSTEM_PROMPT

    def test_it_is_rendered_onto_the_desk(self):
        """Rendered or it reaches nobody — the point of the required field is
        that the evidence travels past the desk that read it."""
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk()
        desk.fundamental_report = {
            "summary": "s", "confidence": 60,
            "positioning_read": {"congress_disclosures_90d": 6,
                                 "social_posts_7d": 203,
                                 "stance": "SUPPORTS_BEAR",
                                 "note": "congress selling"},
        }

        ctx = desk.get_compressed_context()

        assert "Positioning (SUPPORTS_BEAR)" in ctx
        assert "congress_disclosures_90d=6" in ctx
        assert "congress selling" in ctx


class TestACorrectedCountLeavesAStaleStance:
    """Seen on the first live run of positioning_read: AAPL reported
    `congress_disclosures_90d: 0` against a true 8, and concluded
    "NO_COVERAGE". The reconcile fixed the count. The conclusion built on it
    survived, and would have travelled downstream as though founded.

    We do NOT rewrite the stance — judgment is the agent's job and this module
    counts filings. What it can do is stop the stale conclusion travelling
    unmarked.
    """

    def test_the_stance_is_flagged_when_its_inputs_changed(self):
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {
            "congress_disclosures_90d": 0, "stance": "NO_COVERAGE"}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 8,
                                 "social_posts_7d": 0}):
            reconcile_positioning_read(art, "AAPL")

        assert art["positioning_read"]["stance_is_stale"] is True
        assert "stated 0, actual 8" in art["positioning_read"]["stance_stale_reason"]

    def test_the_stance_itself_is_still_not_rewritten(self):
        """The boundary holds: we mark it, we do not replace it."""
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {
            "congress_disclosures_90d": 0, "stance": "NO_COVERAGE"}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 8,
                                 "social_posts_7d": 0}):
            reconcile_positioning_read(art, "AAPL")

        assert art["positioning_read"]["stance"] == "NO_COVERAGE"

    def test_an_accurate_read_is_not_flagged(self):
        """A false stale-flag on every artifact would make the real ones
        invisible."""
        from app.v3.alt_data_block import reconcile_positioning_read

        art = {"positioning_read": {
            "insider_buy_filings_30d": 0, "congress_disclosures_90d": 8,
            "social_posts_7d": 0, "stance": "SUPPORTS_BEAR"}}
        with patch("app.v3.alt_data_block.compute_positioning_facts",
                   return_value={"insider_buy_filings_30d": 0,
                                 "congress_disclosures_90d": 8,
                                 "social_posts_7d": 0}):
            reconcile_positioning_read(art, "AAPL")

        assert "stance_is_stale" not in art["positioning_read"]

    def test_the_desk_render_carries_the_warning(self):
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk()
        desk.fundamental_report = {
            "summary": "s", "confidence": 60,
            "positioning_read": {"congress_disclosures_90d": 8,
                                 "stance": "NO_COVERAGE",
                                 "stance_is_stale": True},
        }

        ctx = desk.get_compressed_context()

        assert "STANCE IS STALE" in ctx
        assert "weigh the counts, not the label" in ctx
