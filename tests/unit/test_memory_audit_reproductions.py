"""Read-only characterization of conflicts, not assertions of desired behavior."""
import ast,copy,inspect,json,os
from pathlib import Path
from unittest.mock import patch
import pytest
pytestmark=pytest.mark.skipif(os.environ.get('MEMORY_AUDIT_CAPTURE')!='1',reason='explicit audit characterization')

def test_missing_price_gate_reaches_buy_selector():
    from app.v3.shared_desk import SharedDesk
    from app.v3.orchestrator import _apply_policy_gates,_build_v1_compatible_result
    from app.services import pipeline_service as ps
    desk=SharedDesk(ticker='AUDIT',cycle_id='offline-rule-audit')
    desk.regime_classification={'summary':'ok'}
    desk.final_decision={'action':'BUY','confidence':80,'stop_loss':10.0,'dynamic_trigger':{'type':'trailing_drop','value':0.1},'position_size_pct':2.0}
    with patch('app.quant.technical_baseline.has_price_history',return_value=False),patch('app.v3.orchestrator._record_gate',side_effect=lambda d,label,**k:label):
        policy_action=_apply_policy_gates(desk)
    result=_build_v1_compatible_result(desk);result['policy_action']=policy_action
    assert policy_action=='HOLD_NO_PRICE_DATA' and result['action']=='BUY'
    tree=ast.parse(inspect.getsource(ps))
    selector=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.unparse(n.test)=="action == 'SELL' and policy_action == 'HOLD_NO_POSITION'")
    # Execute the actual branch CONDITIONS, replacing every branch BODY with
    # its source condition string. No order, emitter or sizing code can run.
    def safe(node):
        return ast.If(test=copy.deepcopy(node.test),body=[ast.Return(ast.Constant(ast.unparse(node.test)))],orelse=[safe(node.orelse[0])] if node.orelse and isinstance(node.orelse[0],ast.If) else [ast.Return(ast.Constant('else'))])
    fn=ast.FunctionDef(name='select',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=[safe(selector)],decorator_list=[])
    program=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
    env={'action':'BUY','policy_action':policy_action,'confidence':80,'contract':{},'result':result,'get_param':lambda key:70}
    exec(compile(program,'<actual-executor-conditions-only>','exec'),env)
    selected=env['select']();assert selected=="action == 'BUY'"
    triggers=ps.resolve_trigger_registration(policy_action,'BUY',False,False,False)
    assert triggers=={'sell_side':False,'dynamic':True}
    output={'gate':policy_action,'result_action':result['action'],'executor_selected_condition':selected,'trigger_registration':triggers,'scope':'Conditions-only executor reproduction; no actual buy or trigger executed.'}
    base=Path(__file__).resolve().parents[3]/'.scratch/memory-isolation-20260907'
    (base/'rule-reproductions.json').write_text(json.dumps(output,indent=2))
