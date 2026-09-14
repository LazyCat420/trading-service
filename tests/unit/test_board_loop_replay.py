import importlib.util
import json
from pathlib import Path
from copy import deepcopy
import pytest

spec=importlib.util.spec_from_file_location('board_replay',Path(__file__).resolve().parents[2]/'scripts/benchmarks/board_loop_replay.py')
replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)


def payload():
    return {'messages':[{'role':'system','content':'Original rules.\n## TOOLS (conditional — never reflexive)\nOriginal tool rules.'},
                        {'role':'user','content':'## Ticker: TEST\n## Cycle: frozen\n\nEvidence A. Dissent B. Unknown C.'}],
            'tools':[{'type':'function','function':{'name':'whiteboard_read','parameters':{'type':'object'}}}]}


@pytest.mark.parametrize('arm',['baseline','prompt','delivery','combined'])
def test_ablation_preserves_original_evidence_and_tools(arm):
    base=payload();original=deepcopy(base)
    result=replay.arm_payload(base,arm,'GUIDANCE\n',{'board_directive':'preserve dissent'})
    assert base==original and result['tools']==base['tools']
    assert 'Evidence A. Dissent B. Unknown C.' in result['messages'][1]['content']
    assert ('GUIDANCE' in result['messages'][0]['content'])==(arm in ('prompt','combined'))
    assert ('preserve dissent' in result['messages'][1]['content'])==(arm in ('delivery','combined'))


def test_truncated_initial_payload_cannot_be_replaced_by_later_attempt():
    item={'events':[{'stage':'provider.payload','blob':{'truncated':True,'content':'partial'}},
                    {'stage':'provider.payload','blob':{'truncated':False,'content':json.dumps(payload())}}]}
    with pytest.raises(ValueError,match='Initial provider payload'):replay.fixture(item)


def test_unknown_reads_and_writes_are_not_executed_or_invented():
    snapshot={'entries':[{'section':'known','content':{'fact':42}}]}
    assert replay.snapshot_reply(snapshot,'TEST','whiteboard_annotate',{'ticker':'TEST'}) is None
    assert replay.snapshot_reply(snapshot,'TEST','get_portfolio_state',{}) is None
    assert replay.snapshot_reply(snapshot,'TEST','whiteboard_read',{'ticker':'OTHER','section':'known'}) is None
