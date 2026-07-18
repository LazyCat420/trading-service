import asyncio
import logging
import re

from deepeval.models import DeepEvalBaseLLM
from app.services.prism_agent_caller import llm, Priority

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Best-effort extraction of the JSON object/array DeepEval expects.

    Local eval models wrap their JSON in prose/markdown fences; DeepEval does a
    bare json.loads and every metric call failed with "Evaluation LLM outputted
    an invalid JSON" (2 metrics × 2 retries × ~30s = minutes wasted per
    decision). Strip fences, then fall back to the first balanced {...} / [...]
    block. Returns the input unchanged when nothing better is found."""
    if not text:
        return text
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    # First balanced JSON object or array in the prose — earliest opener first
    # (so an array wrapping objects isn't truncated to its first element), and
    # if a candidate never balances (e.g. a '{' inside prose quotes) fall
    # through to the next opener occurrence.
    _PAIR = {"{": "}", "[": "]"}
    starts = [i for i, ch in enumerate(text) if ch in _PAIR][:10]
    for start in starts:
        opener = text[start]
        closer = _PAIR[opener]
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return text


class VLLMDeepEvalWrapper(DeepEvalBaseLLM):
    def __init__(self):
        # We pass no additional config since we use the singleton
        super().__init__()

    def load_model(self):
        # The model is loaded via vllm_client singleton, return it
        return llm

    def generate(self, prompt: str) -> str:
        """Synchronous generate for DeepEval."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are in a running loop, so we cannot do run_until_complete easily
                # unless we run it in an executor, but vllm_client is async.
                # In FastAPI/async contexts, deepeval calls a_generate anyway,
                # but if it falls back here we must handle it.
                future = asyncio.run_coroutine_threadsafe(self.a_generate(prompt), loop)
                return future.result()
            else:
                return loop.run_until_complete(self.a_generate(prompt))
        except RuntimeError:
            return asyncio.run(self.a_generate(prompt))

    async def a_generate(self, prompt: str) -> str:
        """Asynchronous generate for DeepEval."""
        response, _, _ = await llm.chat(
            system=(
                "You are an expert impartial evaluator determining factual alignment. "
                "Respond with the requested JSON ONLY — no prose, no markdown fences."
            ),
            user=prompt,
            temperature=0.0,
            # 2048 truncated long faithfulness claim lists mid-JSON → invalid JSON.
            max_tokens=4096,
            priority=Priority.HIGH,  # Evaluators need high priority so they don't block
            agent_name="deepeval_judge",
        )
        return _extract_json(response)

    def get_model_name(self) -> str:
        return llm.model
