"""Collect OpenAI SSE output without executing tools or repairing model content."""
import json,time

async def complete_stream(client,url,payload,progress=None):
    started=time.monotonic();message={'role':'assistant','content':''};calls={};usage=None;finish=None;first=None;reasoning=''
    async with client.stream('POST',url,json={**payload,'stream':True,'stream_options':{'include_usage':True}}) as response:
        headers_s=time.monotonic()-started
        if response.is_error:
            body=(await response.aread()).decode(errors='replace')
            raise RuntimeError(f'HTTP {response.status_code}: {body[:2000]}')
        async for line in response.aiter_lines():
            if not line.startswith('data:'):continue
            data=line[5:].strip()
            if data=='[DONE]':break
            event=json.loads(data)
            if event.get('usage'):usage=event['usage']
            for choice in event.get('choices',[]):
                delta=choice.get('delta') or {};text=delta.get('content') or ''
                fragments=delta.get('tool_calls') or [];thought=delta.get('reasoning_content') or ''
                if first is None and (text or thought or any((x.get('function') or {}).get('name') or (x.get('function') or {}).get('arguments') for x in fragments)):
                    first=time.monotonic()-started
                    if progress:progress(first,headers_s)
                message['content']+=text;reasoning+=thought
                for fragment in fragments:
                    index=fragment['index'];call=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if fragment.get('id'):call['id']=fragment['id']
                    if fragment.get('type'):call['type']=fragment['type']
                    function=fragment.get('function') or {}
                    call['function']['name']+=function.get('name') or ''
                    call['function']['arguments']+=function.get('arguments') or ''
                if choice.get('finish_reason') is not None:finish=choice['finish_reason']
    if finish is None:raise RuntimeError('SSE ended without a completion finish reason')
    if calls:message['tool_calls']=[calls[i] for i in sorted(calls)]
    if reasoning:message['reasoning_content']=reasoning
    return {'message':message,'usage':usage,'finish_reason':finish,'first_delta_s':first,'headers_s':headers_s}
