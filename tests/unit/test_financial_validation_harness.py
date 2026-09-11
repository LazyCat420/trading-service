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
