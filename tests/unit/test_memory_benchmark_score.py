"""Offline benchmark scoring through real parser/contract functions; no inference."""
import json,os
from pathlib import Path
import pytest
pytestmark=pytest.mark.skipif(os.environ.get('MEMORY_BENCHMARK_SCORE')!='1',reason='explicit frozen replay scorer')
BASE=Path(os.environ.get('MEMORY_BENCHMARK_DIR',str(Path(__file__).resolve().parents[3]/'.scratch/memory-isolation-20260907')))
EXPECTED={
 'v3_fundamental_analyst':{'metrics.pe_ratio':8.25,'metrics.forward_pe':11.25,'metrics.peg_ratio':0.93,'metrics.price_to_book':2.35,'metrics.profit_margin':0.1278,'metrics.oper_margin':0.1803,'metrics.gross_margin':0.5604,'metrics.roe':0.309,'metrics.roa':0.1772,'metrics.debt_to_equity':0.45,'metrics.current_ratio':2.19,'metrics.revenue_growth':0.0174,'metrics.eps_growth_qoq':-0.0592,'metrics.short_float_pct':0.1071,'metrics.inst_own_pct':0.7521,'metrics.recom_score':3.03,'metrics.target_price':102.53},
 'v3_quant_analyst':{'risk_metrics.rsi':50.64,'risk_metrics.atr':55.68,'risk_metrics.vol_signal':'NEUTRAL','risk_metrics.vol_prediction_premium':-0.04,'risk_metrics.predicted_vol_annualized_pct':68.87,'risk_metrics.sma_200_status':'ABOVE','risk_metrics.bollinger_position':'MIDDLE','risk_metrics.volume_trend':'DECREASING','risk_metrics.max_drawdown_est':35.07,'risk_metrics.diversification_ratio':3.40},
 'v3_regime_engine':{'vix_level':14.53,'yield_trend':'stable','dxy_trend':'strengthening'},
}
def lookup(obj,path):
 for key in path.split('.'):
  if not isinstance(obj,dict) or key not in obj:return None
  obj=obj[key]
 return obj

def test_score_frozen_outputs():
 from app.v3.agent_runner import _parse_artifact,_is_wrong_shape
 from app.v3.artifacts import validate_artifact
 from app.v3.decision_contract import entry_errors
 cases={c['id']:c for c in json.loads((BASE/'role-inputs.json').read_text())}
 rows=json.loads((BASE/'role-results.json').read_text());scores=[]
 for row in rows:
  case=cases[row['case']];role=case['role'];kind=case['artifact_type']
  artifact=_parse_artifact(row['response'],kind,role)
  final_turn=bool(row['turns']) and not (row['turns'][-1]['message'].get('tool_calls') or [])
  parsed=bool(artifact) and not _is_wrong_shape(kind,artifact)
  schema_errors=validate_artifact(kind,artifact) if parsed else ['No artifact of expected shape']
  contract_errors=entry_errors(artifact) if parsed and role=='v3_board_of_directors' else []
  frozen_constraints={}
  frozen_constraint_applicability={}
  if role=='v3_board_of_directors':
   size=artifact.get('position_size_pct') if isinstance(artifact,dict) else None
   frozen_constraint_applicability['size_within_frozen_ceiling']=isinstance(artifact,dict) and str(artifact.get('action','')).upper()=='BUY'
   frozen_constraints['size_within_frozen_ceiling']=isinstance(size,(int,float)) and not isinstance(size,bool) and 0<=size<=0.6
  facts=[]
  for path,expected in EXPECTED.get(role,{}).items():
   actual=lookup(artifact,path);status='missing'
   if actual is not None:
    if isinstance(expected,(int,float)):
     # max_drawdown_est historically accepts percentage or fraction; compare
     # units equivalently, never flag a normalized representation as wrong.
     normalized=actual*100 if path.endswith('max_drawdown_est') and isinstance(actual,(int,float)) and 0<=actual<=1 else actual
     correct=isinstance(normalized,(int,float)) and not isinstance(normalized,bool) and abs(normalized-expected)<=max(1e-6,abs(expected)*0.001)
    else:correct=isinstance(actual,str) and actual.lower()==expected.lower()
    status='correct' if correct else 'wrong'
   facts.append({'field':path,'expected':expected,'actual':actual,'status':status})
  tool_errors={}
  for turn in row['turns']:
   for result in turn['tool_results']:
    if result.get('error'):tool_errors[result['error']]=tool_errors.get(result['error'],0)+1
  usable=bool(final_turn and parsed and not schema_errors and not contract_errors and not row.get('error'))
  required_step=role!='v3_junior_analyst' or row['whiteboard_writes']>0
  scores.append({'case':row['case'],'arm':row['arm'],'repeat':row.get('repeat',0),'role':role,
    'raw_json':row['valid_json'],'parser_accepted':parsed,'final_turn':final_turn,'schema_errors':schema_errors,
    'decision_contract_errors':contract_errors,'first_pass_usable':usable,'required_step_complete':required_step,
    'complete_with_required_step':usable and required_step,'frozen_constraints':frozen_constraints,'frozen_constraint_applicability':frozen_constraint_applicability,'facts':facts,'tool_errors_by_cause':tool_errors,
    'http_or_runtime_error':row.get('error'),'stop':row['stop'],'artifact':artifact})
 (BASE/'role-quality.json').write_text(json.dumps(scores,indent=2))
 assert len(scores)==len(rows)
