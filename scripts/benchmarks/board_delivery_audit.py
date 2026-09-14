"""Audit frozen Board trace exports; reads local files only."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median
from app.v3.board_evidence import regime_packet
from scripts.benchmarks.board_loop_replay import fixture


def audit(inputs, snapshots):
    rows=[]; tools=Counter(); sections=Counter()
    for item in inputs['items']:
        row=item['telemetry']; key=row['cycle_id']+'|'+row['ticker']
        first,_=fixture(item)  # Reject a missing initial payload, not a later substitute.
        user='\n'.join(m.get('content') or '' for m in first['messages'] if m['role']=='user')
        source=next(e['content'] for e in reversed(snapshots[key]['entries']) if e['section']=='regime_classification')
        if isinstance(source,str): source=json.loads(source)
        packet,receipt=regime_packet(source); directive=source.get('board_directive')
        calls={}
        for event in item['events']:
            if event['stage']=='agent.end': break
            blob=event.get('blob')
            if event['stage']!='provider.payload' or not blob or blob.get('truncated'): continue
            for message in json.loads(blob['content'])['messages']:
                for call in message.get('tool_calls',[]): calls[call['id']]=call['function']
        reads=[]; recovered=False
        for fn in calls.values():
            name=fn['name'].split('__')[-1]; args=json.loads(fn['arguments']);args.pop('_lazy_trading_context',None)
            tools[name]+=1
            if name=='whiteboard_read':
                reads.append(json.dumps(args,sort_keys=True));section=args.get('section','<overview>')
                sections[section]+=1;recovered|=section=='regime_classification'
        lines=Counter(l.strip() for l in user.splitlines() if len(l.strip())>=100)
        rows.append({'cycle_id':row['cycle_id'],'ticker':row['ticker'],'user_chars':len(user),
                     'research_truncated':'research context TRUNCATED' in user,
                     'directive_available':bool(directive),'directive_delivered':bool(directive and directive in user),
                     'fixed_directive_delivered':bool(directive and directive in packet),
                     'added_packet_chars':len(packet),'unique_tool_calls':len(calls),
                     'repeated_identical_reads':len(reads)-len(set(reads)),
                     'requested_regime_later':recovered,
                     'exact_repeated_long_line_chars':sum((n-1)*len(line) for line,n in lines.items()),
                     'regime_receipt':receipt})
    return {'scope':'First invocation only, source versions frozen before Board start. Captured call IDs deduplicated across cumulative payloads. Long-line repetition is not semantic noise or evidence irrelevance.',
            'cases':len(rows),'initial_directive_deliveries':sum(r['directive_delivered'] for r in rows),
            'fixed_directive_deliveries':sum(r['fixed_directive_delivered'] for r in rows),
            'truncated_research_summaries':sum(r['research_truncated'] for r in rows),
            'median_user_chars':median(r['user_chars'] for r in rows),
            'median_added_packet_chars':median(r['added_packet_chars'] for r in rows),
            'tool_calls':dict(tools),'whiteboard_sections':dict(sections),'rows':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('input','snapshots','output'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    result=audit(json.loads(Path(args.input).read_text()),json.loads(Path(args.snapshots).read_text()))
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
