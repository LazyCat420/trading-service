"""Wiring invariants for the non-blocking valuation analyst.

The agent was deliberately kept OUT of the AND-gate that releases the debate,
because adding a third term to that conjunction requires a matching synthetic
artifact in every skip path and the two that already exist for `fa_skipped` are
the evidence of how easily one gets missed. What buys the ordering instead is
that the scheduler is FIFO. These tests pin both halves of that argument — the
gate must stay untouched, AND the FIFO property it relies on must hold.
"""

import inspect
import re

from app.v3 import orchestrator
from app.v3.shared_desk import SharedDesk, _VALID_ARTIFACT_TYPES


class TestTheDebateGateWasNotTouched:
    def test_valuation_is_absent_from_the_debate_release_condition(self):
        """A third term here without a stub in every skip path hangs the cycle
        until MAX_LOOP_ITERATIONS."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        gate = re.search(
            r'elif sec in \("fundamental_report", "quant_report"\).*?'
            r'_queue_debate_phase\(\)\n\s*\n',
            src, re.S,
        )
        assert gate, "the debate gate moved — re-verify the non-blocking design"
        assert "valuation_report" not in gate.group(0)

    def test_the_valuation_branch_does_not_abort_the_desk(self):
        """Every other research branch calls _check_abort, because the debate
        cannot proceed without it. This one must not: a failed valuation leaves
        the cycle running with one fewer opinion rather than killing it."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        branch = re.search(
            r'elif name == "valuation_analyst":(.*?)elif name == "bull_argument":',
            src, re.S,
        )
        assert branch, "the valuation scheduler branch is missing"
        # Comments stripped: the branch explains WHY it omits _check_abort, and
        # a naive substring test matches its own rationale.
        code = "\n".join(
            line for line in branch.group(1).splitlines()
            if not line.strip().startswith("#")
        )
        assert "_check_abort" not in code
        assert "whiteboard.write_section" in code


class TestFifoOrderingIsWhatMakesThisSafe:
    def test_queue_appends_and_the_loop_pops_the_front(self):
        """The whole non-blocking design rests on this. If _queue_agent ever
        prepends, or the loop pops from the back, the valuation agent would run
        AFTER the debate it is meant to inform — silently, with no test failing
        anywhere else."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)

        assert "tasks_to_run.append(" in src
        assert "tasks_to_run.pop(0)" in src
        assert "tasks_to_run.insert(" not in src

    def test_valuation_is_queued_with_the_other_research_agents(self):
        """Queued off desk_note alongside FA and QA — therefore ahead of
        bull/bear, which _queue_debate_phase can only append later."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        block = re.search(
            r'elif not fa_skipped:(.*?)elif sec in \("fundamental_report"',
            src, re.S,
        )
        assert block
        body = block.group(1)
        assert '_queue_agent("fundamental_analyst"' in body
        assert '_queue_agent("valuation_analyst"' in body

    def test_run_counts_has_the_key(self):
        """run_counts[name] += 1 is a BARE SUBSCRIPT in the scheduler loop, so
        a missing key is a KeyError that kills the desk. _queue_agent uses
        .get(), which would mask it right up until dispatch."""
        src = inspect.getsource(orchestrator.run_v3_pipeline)
        counts = re.search(r"run_counts = \{(.*?)\}", src, re.S)
        assert counts
        assert '"valuation_analyst": 0' in counts.group(1)
        assert "run_counts[name] += 1" in src


class TestTheArtifactReachesDownstream:
    def test_the_desk_accepts_the_artifact_type(self):
        assert "valuation_report" in _VALID_ARTIFACT_TYPES

    def test_it_round_trips_through_serialization(self):
        """A desk is persisted and rehydrated between phases; a field missing
        from to_dict/from_dict vanishes silently at that boundary."""
        desk = SharedDesk()
        desk.valuation_report = {"verdict": "FAIR", "confidence": 60}

        revived = SharedDesk.from_dict(desk.to_dict())

        assert revived.valuation_report == {"verdict": "FAIR", "confidence": 60}

    def test_it_is_rendered_into_the_compressed_context(self):
        """Rendered here or it reaches nobody: the Board and the debate read the
        desk through this view, not the raw artifact. A valuation_report that is
        computed, reconciled and then never rendered is invisible work."""
        desk = SharedDesk()
        desk.ticker = "COST"
        desk.valuation_report = {
            "verdict": "OVERVALUED", "confidence": 71,
            "summary": "The price requires growth the business has not shown.",
            "price_implied_assumption": "17.1%/yr NOPAT growth for a decade",
            "valuation_metrics": {"ev_to_ebit": 37.1, "implied_growth_pct": 17.1},
            "fair_value_estimate": 720.0,
            "fair_value_basis": "22x EV/EBIT on TTM operating income",
            "what_would_change_my_mind": "two quarters of EBIT growth above 15%",
        }

        ctx = desk.get_compressed_context()

        assert "Valuation" in ctx
        assert "OVERVALUED" in ctx
        assert "17.1" in ctx
        assert "22x EV/EBIT" in ctx

    def test_the_multiples_line_says_it_is_ebit_based(self):
        """The desk also carries a vendor EV/EBITDA elsewhere. An unlabelled
        multiple next to it invites the Board to compare two different
        quantities — ours is systematically higher."""
        desk = SharedDesk()
        desk.valuation_report = {
            "verdict": "FAIR", "confidence": 50, "summary": "s",
            "valuation_metrics": {"ev_to_ebit": 18.4},
        }

        assert "no D&A on file" in desk.get_compressed_context()

    def test_the_corrected_figure_reaches_the_board(self):
        """The reconcile fixes `valuation_metrics` but must not rewrite prose.
        In the 07-28 cycle every figure that reached a final rationale was the
        model's ORIGINAL — PYPL quoted 1.1% against a computed 0.77% and became
        a live BUY. The correction has to be rendered or the guard only cleans
        the field nobody downstream reads."""
        desk = SharedDesk()
        desk.valuation_report = {
            "verdict": "UNDERVALUED", "confidence": 65,
            "summary": "The market prices in just 1.1% NOPAT growth.",
            "price_implied_assumption": "1.1%/yr",
            "valuation_metrics": {"implied_growth_pct": 0.77},
            "_model_reported_valuation": {"implied_growth_pct": 1.1},
        }

        ctx = desk.get_compressed_context()

        assert "0.77" in ctx
        assert "implied_growth_pct" in ctx
        assert "Corrected figures" in ctx

    def test_an_unapplied_correction_says_it_was_not_applied(self):
        """On a stale snapshot the correction is withheld from the metrics and
        parked under _unreconciled_valuation. Rendering that as though it had
        been applied would misreport which number is authoritative."""
        desk = SharedDesk()
        desk.valuation_report = {
            "verdict": "FAIR", "confidence": 50, "summary": "s",
            "valuation_metrics": {"ev_to_ebit": 12.0},
            "_unreconciled_valuation": {
                "ev_to_ebit": {"model": 12.0, "verified": 18.4}
            },
        }

        ctx = desk.get_compressed_context()

        assert "18.4" in ctx
        assert "NOT applied" in ctx

    def test_a_clean_report_renders_no_corrections_block(self):
        """A model that reported nothing wrong must not get a corrections
        heading — an empty guard block reads as a finding."""
        desk = SharedDesk()
        desk.valuation_report = {
            "verdict": "FAIR", "confidence": 50, "summary": "s",
            "valuation_metrics": {"ev_to_ebit": 18.4},
        }

        assert "Corrected figures" not in desk.get_compressed_context()

    def test_it_is_counted_as_a_research_artifact(self):
        desk = SharedDesk()
        desk.valuation_report = {"verdict": "FAIR"}

        assert "valuation_report" in desk.get_research_artifacts()


class TestReconcileIsWiredIntoTheRunner:
    def test_the_runner_reconciles_this_agent(self):
        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        assert 'agent_name == "v3_valuation_analyst"' in src
        assert "reconcile_valuation_metrics" in src

    def test_the_block_is_injected_for_the_agent_and_the_board(self):
        """The Board sizes the trade and should see the multiples too.

        Matched by MEMBERSHIP, not against the exact tuple literal: the first
        version of this test pinned `("v3_valuation_analyst",
        "v3_board_of_directors")` verbatim and failed the moment the
        synthesizer was added to the same guard on 2026-07-28 — a passing test
        that breaks on a correct widening tests the spelling, not the wiring.
        """
        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        inject = re.search(
            r"if agent_name in \(([^)]*?)\):\s*\n\s*valuation = ", src, re.S,
        )
        assert inject, "the valuation_context injection guard moved or was removed"
        recipients = inject.group(1)
        assert '"v3_valuation_analyst"' in recipients
        assert '"v3_board_of_directors"' in recipients

    def test_the_synthesizer_sees_the_verified_blocks(self):
        """It issues the FINAL action — it downgraded 21 of 41 Board BUYs to
        HOLD — and until 2026-07-28 it received none of the blocks the
        reconcile passes enforce, deciding from summarised prose while every
        verified number went to other agents. Measured consequence: its
        overrides leaned on oscillators (stochastic +27.1pp) and away from
        fundamentals (eps -21.2pp)."""
        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        for block in ("valuation_context", "quant_math_context",
                      "fundamental_context"):
            guard = re.search(
                r"if agent_name in \(([^)]*?)\):\s*\n\s*\w+ = "
                r"desk\.cycle_metadata\.get\(\"" + block + r"\"", src, re.S,
            )
            assert guard, f"{block} injection guard not found"
            assert '"v3_decision_synthesizer"' in guard.group(1), (
                f"the synthesizer does not receive {block}"
            )


class TestTheOptimizerCannotReachTheDoctrine:
    def test_the_agent_is_not_a_skill_optimizer_target(self):
        """A mined doctrine is a SOURCE DOCUMENT. The optimizer issues REPLACE
        actions and rolls back on outcome scores — after a month of that nobody
        could say which sentences came from the corpus."""
        from app.autoresearch.skill_optimizer import TARGET_AGENTS

        assert "v3_valuation_analyst" not in TARGET_AGENTS

    def test_the_omission_is_documented_where_someone_would_undo_it(self):
        from app.autoresearch import skill_optimizer

        src = inspect.getsource(skill_optimizer)
        assert "DELIBERATELY ABSENT" in src


class TestAlternativeDataReachesTheDecidingDesks:
    """2026-07-28: `get_congress_trades` was never called in 30 days by any
    agent, and the alt-data BLOCK carrying that same evidence went to only two
    of ten agents. The data is not missing — 30,483 congress rows across 508
    tickers active in the last 90 days — it simply never reached the desks that
    size and authorise the trade.

    Fixed as a block rather than by nudging the tool, because six of ten agents
    average loops_used = 1.00: they emit their JSON on the first pass and never
    take a tool turn, so a whitelist entry cannot reach them. A block always
    can.
    """

    def test_the_deciding_agents_receive_alt_data(self):
        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        guard = re.search(
            r"if agent_name in \(([^)]*?)\):\s*\n\s*alt_data = ", src, re.S)
        assert guard, "the alt_data injection guard moved or was removed"
        recipients = guard.group(1)
        for agent in ("v3_board_of_directors", "v3_decision_synthesizer",
                      "v3_quant_analyst", "v3_valuation_analyst"):
            assert f'"{agent}"' in recipients, f"{agent} cannot see alt data"

    def test_the_research_analysts_keep_it(self):
        """Widening must not drop the two agents that already had it."""
        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        guard = re.search(
            r"if agent_name in \(([^)]*?)\):\s*\n\s*alt_data = ", src, re.S)
        recipients = guard.group(1)
        assert '"v3_junior_analyst"' in recipients
        assert '"v3_fundamental_analyst"' in recipients


class TestToolInstructionsAreNotHedgedIntoSilence:
    """Measured over 30 days on the ONE agent that actually loops (5.94 turns):

        get_institutional_holdings  "(are top funds adding or cutting?)"  310 calls
        get_finnhub_news            "(7-day catalysts)"                   258 calls
        get_reddit_trending_stocks  "only if retail buzz is plausibly..."   0 calls

    Same prompt, same step, same whitelist. Under a stated 7-turn budget plus
    "TRACE one lead depth-first", every hedge resolves to no.
    """

    def test_reddit_is_not_gated_behind_a_plausibility_hedge(self):
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "only if retail buzz is plausibly a factor" not in SYSTEM_PROMPT
        assert "get_reddit_trending_stocks" in SYSTEM_PROMPT

    def test_absence_of_chatter_is_reportable_evidence(self):
        """'No meaningful retail chatter' on a moving tape is information. If
        the only reportable outcome is a positive finding, the cheapest
        compliant action is to skip the call."""
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "no meaningful retail chatter" in SYSTEM_PROMPT.lower()

    def test_market_data_stays_hedged_on_purpose(self):
        """NOT every hedge is a bug. The Pre-Collected Data Report already
        carries price/volume, so calling get_market_data is usually waste —
        this one is correctly conditional and must stay that way."""
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "`get_market_data` ONLY if" in SYSTEM_PROMPT


class TestPromptsDoNotHardcodeAFixedToolOpening:
    """2026-07-28, measured across 7 tickers in two cycles: the junior analyst
    opened with `get_finnhub_news` + `get_institutional_holdings` +
    `whiteboard_write` on 7 of 7, and the fundamental analyst used an IDENTICAL
    six-tool set on every ticker. Only the 4th/5th slot ever varied.

    Cause: the prompts named specific tools inside a numbered step
    ("FETCH core metrics (both, always)"), so the opening was deterministic and
    50-75% of the turn budget was spent before any ticker-specific choice. A
    newly-added tool has no slot to occupy — it is never reached, not rejected.
    That is why removing the hedge on get_reddit_trending_stocks alone changed
    nothing.
    """

    def test_fundamental_no_longer_mandates_a_fixed_pair(self):
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "(both, always)" not in SYSTEM_PROMPT
        assert "largest gap" in SYSTEM_PROMPT

    def test_fundamental_points_at_the_block_not_the_tool(self):
        """`get_finviz_fundamentals` returns what `fundamental_context` already
        carries, reconciled. A tool named in a prompt outlives the block that
        replaced it and keeps consuming turns."""
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "PRECOMPUTED FUNDAMENTAL SNAPSHOT" in SYSTEM_PROMPT
        assert "Do NOT spend a turn on `get_finviz_fundamentals`" in SYSTEM_PROMPT

    def test_junior_selects_by_evidence_gap(self):
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "RECON the LARGEST GAP first" in SYSTEM_PROMPT
        assert "RECON the gaps only:" not in SYSTEM_PROMPT

    def test_junior_names_the_measured_failure_mode(self):
        """The instruction carries its own evidence, so a future edit that
        reintroduces a fixed opening has to argue with the measurement."""
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "identical pair every single time" in SYSTEM_PROMPT

    def test_the_quant_prompt_is_deliberately_untouched(self):
        """The quant already says FETCH ONLY WHAT IS MISSING, runs at 1.08
        loops, and scores the HIGHEST quality of the four research desks
        (82.2). Rewriting it to encourage more calls would undo the
        invented-RSI fix that put its numbers in the prompt."""
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT

        assert "FETCH ONLY WHAT IS MISSING" in SYSTEM_PROMPT
        assert "you do NOT need `get_technical_indicators`" in SYSTEM_PROMPT
