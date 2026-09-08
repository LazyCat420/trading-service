"""Export compact, reviewable ticket replay metrics without raw model prompts."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(row):
    roles = row.get('roles', [])
    invocations = [call for role in roles for call in role.get('calls', [])]
    turns = [turn for call in invocations for turn in call.get('turns', [])]
    tools = [tool for turn in turns for tool in turn.get('tools', [])]
    answers = [dict(answer, role=role['agent']) for role in roles
               for answer in (role.get('artifact') or {}).get('research_answers', [])
               if isinstance(answer, dict)]
    counts = Counter(a.get('item_id') for a in answers if a.get('status') == 'answered')
    research_calls = Counter()
    for tool in tools:
        function = tool.get('call', {}).get('function', {})
        name = function.get('name', '').split('__')[-1]
        if name.startswith('whiteboard_'):
            continue
        arguments = function.get('arguments', '')
        try:
            arguments = json.dumps(json.loads(arguments), sort_keys=True)
        except (ValueError, TypeError):
            pass
        research_calls[(name, arguments)] += 1
    usage_complete = bool(turns) and all('prompt_tokens' in t.get('usage', {}) and
                                       'completion_tokens' in t.get('usage', {}) for t in turns)
    return {
        'case': row['case'], 'repeat': row['repeat'], 'arm': row['arm'],
        'pilot': row.get('pilot'), 'complete': row.get('complete', False),
        'fixture_sha256': row['fixture_sha256'], 'model': row.get('model'),
        'elapsed_s': row.get('elapsed_s'),
        'role_outcomes': {r['agent']: r.get('outcome', 'incomplete') for r in roles},
        'degraded_roles': [r['agent'] for r in roles if (r.get('artifact') or {}).get('_degraded')],
        'model_invocations': len(invocations), 'model_turns': len(turns),
        'exact_duplicate_research_calls': sum(max(0, n - 1) for n in research_calls.values()),
        'tool_calls': len(tools), 'tool_errors': sum(bool(t.get('result', {}).get('error')) for t in tools),
        'usage_complete': usage_complete,
        'prompt_tokens': sum(t['usage']['prompt_tokens'] for t in turns) if usage_complete else None,
        'completion_tokens': sum(t['usage']['completion_tokens'] for t in turns) if usage_complete else None,
        'verified_ids': sorted(row.get('verified_answers', {})),
        'repeated_answer_ids': {k: v for k, v in counts.items() if v > 1},
        'research_answers': answers,
        'board_artifact': (roles[-1].get('artifact') if roles and roles[-1]['agent'] == 'v3_board_of_directors' else None),
        'delivery': {r['agent']: r.get('delivery') for r in roles},
        'ticket_delivery': row.get('ticket_delivery'),
        'semantic_review': 'pending: verified quotes do not establish entailment',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(json.loads(p.read_text())) for p in sorted(args.directory.glob('*.json'))], indent=2))
