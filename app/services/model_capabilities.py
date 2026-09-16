"""Model-independent capability receipts for the actual serving endpoint."""
import asyncio
import json
import time
from datetime import datetime, timezone
import httpx

_receipts = {}
_locks = {}


async def validate_endpoint(key, model, force=False):
    from app.services.prism_agent_caller import llm
    from app.services.llm_preflight import _PROBE_TOOL
    ep = llm._endpoints[key]
    identity = (ep.url, model, ep.max_model_len)
    lock = _locks.setdefault((id(asyncio.get_running_loop()), identity), asyncio.Lock())
    async with lock:
        if not force and identity in _receipts and time.monotonic() - _receipts[identity]["_cached_at"] < 300:
            return _receipts[identity]
        receipt = {"box": key, "model": model, "observed_at": datetime.now(timezone.utc).isoformat(),
                   "context_tokens": ep.max_model_len, "tools": False, "structured_output": False,
                   "eligible": False}
        base = {"model": model, "max_tokens": 512, "temperature": 0,
                "min_p": 0, "chat_template_kwargs": {"enable_thinking": False}}
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.post(ep.url.rstrip('/') + '/v1/chat/completions', json={**base,
                    "messages": [{"role": "user", "content": "Call preflight_echo with word OK. Do not explain."}],
                    "tools": [_PROBE_TOOL], "tool_choice": "auto"})
                response.raise_for_status()
                msg = response.json()['choices'][0]['message']
                calls = msg.get('tool_calls') or []
                receipt['tools'] = any(c.get('function', {}).get('name') == 'preflight_echo'
                    and json.loads(c['function'].get('arguments') or '{}') == {'word': 'OK'} for c in calls)
                response = await client.post(ep.url.rstrip('/') + '/v1/chat/completions', json={**base,
                    "messages": [{"role": "user", "content": 'Return exactly a JSON object with key ok and boolean true. No other text.'}],
                    "response_format": {"type": "json_object"}})
                response.raise_for_status()
                content = response.json()['choices'][0]['message'].get('content') or ''
                receipt['structured_output'] = json.loads(content).get('ok') is True
            receipt['eligible'] = bool(receipt['tools'] and receipt['structured_output'] and ep.max_model_len)
            receipt['reason'] = 'verified' if receipt['eligible'] else 'tool, JSON, or context capability unverified'
        except Exception as exc:
            receipt['reason'] = f'capability probe failed: {type(exc).__name__}'
        # Failed probes may recover on the next call; don't poison the model forever.
        if receipt['eligible']:
            receipt["_cached_at"] = time.monotonic()
            _receipts[identity] = receipt
        return receipt


async def discover_cycle_models():
    from app.services.prism_agent_caller import llm, get_live_model_from_vllm, ENDPOINT_PROVIDERS
    async def discover(key, ep):
        ep.validation_required = True
        try:
            model = await get_live_model_from_vllm(ep.url, force_refresh=True)
            receipt = await validate_endpoint(key, model, force=True)
            return {**{k: v for k, v in receipt.items() if not k.startswith('_')}, 'provider': ENDPOINT_PROVIDERS.get(key)}
        except Exception as exc:
            return {'box': key, 'provider': ENDPOINT_PROVIDERS.get(key), 'eligible': False,
                    'reason': f'discovery failed: {type(exc).__name__}: {str(exc)[:300]}'}
    rows = await asyncio.gather(*(discover(k, ep) for k, ep in llm._endpoints.items() if ep.enabled and ep.url))
    return {'version': 1, 'endpoints': rows, 'eligible': any(r['eligible'] for r in rows)}
