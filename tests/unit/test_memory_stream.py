"""Protect benchmark accounting from split tool-call chunks and truncated streams."""
import json
import pytest
from scripts.benchmarks.memory_stream import complete_stream

class Response:
    is_error=False
    def __init__(self,events):self.events=events
    async def aiter_lines(self):
        for event in self.events:
            yield 'data: '+(event if isinstance(event,str) else json.dumps(event))
class Stream:
    def __init__(self,events):self.response=Response(events)
    async def __aenter__(self):return self.response
    async def __aexit__(self,*args):pass
class Client:
    def __init__(self,events):self.events=events;self.payload=None
    def stream(self,method,url,*,json):self.payload=json;return Stream(self.events)

@pytest.mark.asyncio
async def test_split_parallel_calls_and_final_usage():
    client=Client([
      {'choices':[{'delta':{'role':'assistant'}}]},
      {'choices':[{'delta':{'tool_calls':[
        {'index':0,'id':'call-a','function':{'name':'whiteboard_','arguments':'{"ticker":"LU'}},
        {'index':1,'id':'call-b','function':{'name':'get_market_data','arguments':'{"ticker":"STX"}'}}]}}]},
      {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'name':'write','arguments':'LU","content":"A \\"quote\\""}'}}]},'finish_reason':'tool_calls'}]},
      {'choices':[],'usage':{'prompt_tokens':8000,'completion_tokens':45,'total_tokens':8045}},'[DONE]'])
    observed=[]
    result=await complete_stream(client,'http://unused.invalid',{'messages':[]},lambda *times:observed.append(times))
    assert client.payload['stream'] is True and client.payload['stream_options']['include_usage'] is True
    assert result['usage']['prompt_tokens']==8000 and result['usage']['completion_tokens']==45
    assert result['finish_reason']=='tool_calls' and len(observed)==1
    calls=result['message']['tool_calls']
    assert calls[0]['id']=='call-a' and calls[0]['function']['name']=='whiteboard_write'
    assert json.loads(calls[0]['function']['arguments'])=={'ticker':'LULU','content':'A "quote"'}
    assert json.loads(calls[1]['function']['arguments'])=={'ticker':'STX'}

@pytest.mark.asyncio
async def test_truncated_stream_is_not_a_completed_artifact():
    client=Client([{'choices':[{'delta':{'content':'{"summary":"partial"}'}}]}])
    with pytest.raises(RuntimeError,match='without a completion finish reason'):
        await complete_stream(client,'http://unused.invalid',{})
