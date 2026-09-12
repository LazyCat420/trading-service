from types import SimpleNamespace
from unittest.mock import patch
import pytest
from scripts.test_live_financial_record import read_only_store
from scripts.validate_financial_cycle import staging_writes_only, valid_staging_database


def test_source_probe_blocks_store_and_direct_driver_writes_even_if_swallowed():
    from app.db import mongo_store
    from pymongo.collection import Collection
    with read_only_store() as attempts:
        with pytest.raises(RuntimeError, match='forbids database writes'):
            mongo_store.insert_docs('bots', [{'bot_id': 'test'}])
        with pytest.raises(RuntimeError, match='forbids database writes'):
            Collection.update_one(SimpleNamespace(), {}, {'$set': {'value': 1}})
    assert len(attempts) == 2


@pytest.mark.parametrize('name', ['trading_bot', 'prism', 'financial_validation_', 'financial_validation_test'])
def test_staging_refuses_nonunique_or_production_namespace(name):
    assert not valid_staging_database(name)
    with pytest.raises(ValueError, match='Invalid staging'):
        with staging_writes_only(name, []):
            pytest.fail('Invalid namespace entered')


def test_staging_driver_guard_refuses_writes_outside_owned_namespace():
    from pymongo.collection import Collection
    name = 'financial_validation_' + 'a' * 32
    violations = []
    def original(collection, *args, **kwargs):
        return 'isolated-write'
    with patch.object(Collection, 'update_one', original):
        with staging_writes_only(name, violations):
            with pytest.raises(RuntimeError, match='outside its temporary database'):
                Collection.update_one(SimpleNamespace(database=SimpleNamespace(name='trading_bot'), name='bots'), {}, {})
            result = Collection.update_one(SimpleNamespace(database=SimpleNamespace(name=name), name='bots'), {}, {})
    assert result == 'isolated-write'
    assert violations == [{'method': 'update_one', 'collection': 'bots'}]


def test_a_questions_run_cannot_use_a_cycle_id_the_queue_refuses_to_serve():
    """`bench-` is synthetic, and a synthetic cycle claims NO questions at all.

    `ResearchQueueService.claim_for_ticker` returns [] for any id matching
    SYNTHETIC_CYCLE_PREFIXES, so every staging cycle ever run reported
    questions_required=0 no matter what was in the queue -- the compound-question
    path was unreachable from the harness by construction, not by chance. A
    questions run therefore mints a non-synthetic id; isolation is the throwaway
    database and the driver guard, never the name. If this reverts, a questions
    run goes green while testing nothing.
    """
    import re
    from pathlib import Path
    from app.services.cycle_scope import is_synthetic_cycle
    source = Path(__file__).resolve().parents[2] / 'scripts/validate_financial_cycle.py'
    prefixes = re.findall(r"'((?:bench-|cycle-v3-staging-)[a-z0-9-]*)'", source.read_text())
    assert set(prefixes) == {'bench-financial-cycle-', 'cycle-v3-staging-'}, prefixes
    assert is_synthetic_cycle('bench-financial-cycle-deadbeef')
    assert not is_synthetic_cycle('cycle-v3-staging-deadbeef')


def test_the_questions_run_seeds_through_the_production_writer():
    """Seeding by hand-built row would not exercise the shape the claim reads."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'scripts/validate_financial_cycle.py').read_text()
    assert 'ResearchQueueService.enqueue_item(' in source
    assert 'QUESTION_BANK' in source


def test_every_symbol_the_harness_patches_still_exists():
    """A renamed target only shows up ~20 minutes into a staging run otherwise.

    `--tools` wrapped `tool_logging.log_tool_usage`, which has been
    `log_tool_call` all along; the run reached the first agent call and died on
    an AttributeError. `patch.object` DOES raise for a missing attribute -- the
    cost was that nothing checked before a full cycle had been started.
    """
    import importlib
    import inspect
    targets = [
        ('app.services.logging.tool_logging', 'log_tool_call', ('service_source',)),
        ('app.services.research_queue_service', 'ResearchQueueService', ()),
        ('app.trading.paper_trader', 'buy', ('strict_capacity',)),
        ('app.trading.paper_trader', 'sell', ()),
        ('app.v3.data_report', 'build_ticker_data_report', ()),
        ('app.services.llm_preflight', 'llm_can_answer', ()),
        ('app.services.llm_preflight', 'tool_calls_are_parsed', ()),
        ('app.agents.tool_whitelists', 'get_agent_tools', ()),
        ('app.agents.tool_whitelists', 'get_agent_budget_turns', ()),
        ('app.agents.base_agent', 'run_agent', ('enable_tools',)),
    ]
    for module_name, attribute, parameters in targets:
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), f'{module_name}.{attribute} is gone'
        if parameters:
            signature = inspect.signature(getattr(module, attribute))
            for parameter in parameters:
                assert parameter in signature.parameters, f'{module_name}.{attribute}({parameter})'
    import httpx
    for attribute in ('request', 'send'):
        assert hasattr(httpx.AsyncClient, attribute), attribute
    assert hasattr(httpx.Client, 'request')
