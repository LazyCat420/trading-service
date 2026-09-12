"""A BUY must not be refused for having been sized the way the code sizes it.

`0bfdb438` added a pre-buy check that refused any BUY whose resolved size was
not EXACTLY the agent's requested size, gated on `financial_evidence_version==1`
-- which the deployed revision sets for every ticker on every cycle. But
`resolve_buy_size_pct` applies a consensus / data-quality haircut on purpose
("disagreement ... now costs size mechanically"), so equality is false whenever
a consensus score exists. Replayed over the 55 real BUYs in the fixture, the
gate refused 54; the survivor carried no consensus score. Nothing consumed the
`sizing_reconsideration_required` flag it set, so the BUY was simply dropped.

These tests assert the PREMISE (the haircut is deliberately non-identity), the
CONSEQUENCE on real production inputs, and that the BUY branch does not compare
the two sizes again. Capacity is revalidated inside `buy(strict_capacity=True)`
-- against cash, concentration and pending reservations -- which is where a
refusal can be based on something the caller cannot already compute.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.services.pipeline_service import resolve_buy_size_pct, apply_health_sizing

FIXTURE = Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/production_buy_sizing_v1.json'
SOURCE = Path(__file__).resolve().parents[2] / 'app/services/pipeline_service.py'
ROWS = json.loads(FIXTURE.read_text())['rows']
#: The runtime value at capture time. The tests below never assert an outcome
#: that depends on it -- it only stands in for `get_param('MAX_POSITION_SIZE_PCT')`
#: so the replay does not need a database.
MAX_POSITION_SIZE_PCT = 0.05


def _resolved_pct(row, max_pct=MAX_POSITION_SIZE_PCT):
    size = resolve_buy_size_pct(row['agent_position_size_pct'], row['confidence'] or 0, max_pct,
                                consensus_score=row['internal_consensus_score'],
                                data_quality=row['data_quality'])
    return None if size is None else size * 100


class TestTheHaircutIsDeliberatelyNotIdentity:
    """Derived from the function, not pinned to a number it happens to return."""

    @pytest.mark.parametrize('consensus', [1, 25, 50, 51, 75, 99])
    def test_any_consensus_below_full_shrinks_an_uncapped_size(self, consensus):
        uncapped = 1.0  # 1% of equity, far below the 5% cap: nothing else can move it
        sized = resolve_buy_size_pct(uncapped, 72, MAX_POSITION_SIZE_PCT, consensus_score=consensus)
        assert sized * 100 < uncapped, 'the consensus haircut must cost size'

    def test_low_data_quality_shrinks_it_too(self):
        full = resolve_buy_size_pct(1.0, 72, MAX_POSITION_SIZE_PCT, consensus_score=100, data_quality=60)
        poor = resolve_buy_size_pct(1.0, 72, MAX_POSITION_SIZE_PCT, consensus_score=100, data_quality=59)
        assert poor < full

    def test_health_reduce_shrinks_it_after_the_fact(self):
        sized = resolve_buy_size_pct(1.0, 72, MAX_POSITION_SIZE_PCT, consensus_score=100)
        assert apply_health_sizing(sized, 'REDUCE') < sized

    def test_so_an_equality_gate_would_be_unpassable_on_real_inputs(self):
        """The premise, measured: this is why the removed check refused 54 of 55."""
        scored = [row for row in ROWS if isinstance(row['internal_consensus_score'], (int, float))]
        assert scored, 'fixture must contain real consensus-scored BUYs'
        assert all(abs(_resolved_pct(row) - row['agent_position_size_pct']) > 1e-6 for row in scored)


class TestEveryRealProductionBuyStaysExecutable:

    def test_the_fixture_is_real_buys_not_an_invented_shape(self):
        assert len(ROWS) >= 20
        assert all(row['agent_position_size_pct'] is None
                   or isinstance(row['agent_position_size_pct'], (int, float)) for row in ROWS)
        assert len({row['ticker'] for row in ROWS}) > 5

    @pytest.mark.parametrize('row', ROWS, ids=lambda r: f"{r['ticker']}@{r['observed_at'][:10]}")
    def test_a_positive_agent_size_resolves_to_a_positive_tradeable_size(self, row):
        """None means an explicit watch-only directive -- a decision, not a refusal."""
        size = _resolved_pct(row)
        agent = row['agent_position_size_pct']
        if isinstance(agent, (int, float)) and agent > 0:
            assert size is not None and size > 0
            assert size <= MAX_POSITION_SIZE_PCT * 100 + 1e-9
        else:
            assert size is None or size > 0


class TestTheBuyBranchDoesNotCompareTheTwoSizesAgain:
    """An allowlist of what may be done with agent_size_pct, parsed, not grepped."""

    def test_agent_size_pct_is_only_read_where_it_cannot_refuse_a_trade(self):
        tree = ast.parse(SOURCE.read_text())
        offenders = []
        for node in ast.walk(tree):
            # Permitted: the call that resolves the size, and an isinstance
            # check that only chooses the wording of a log line.
            if isinstance(node, ast.Compare):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if 'agent_size_pct' in names and 'size_pct' in names:
                    offenders.append(('comparison', getattr(node, 'lineno', '?')))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if {'agent_size_pct', 'size_pct'} <= names:
                    offenders.append(('difference', getattr(node, 'lineno', '?')))
        assert not offenders, (
            'the resolved size must not be checked back against the requested one: ' + repr(offenders))

    def test_the_real_capacity_revalidation_is_still_wired(self):
        source = SOURCE.read_text()
        assert 'strict_capacity=result.get("financial_evidence_version") == 1' in source
        assert "trade_res.get('reason') == 'CAPACITY_REVALIDATION_FAILED'" in source
