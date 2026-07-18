import asyncio
import logging

from deepeval.models import DeepEvalBaseLLM
from app.services.prism_agent_caller import llm, Priority
from app.utils.text_utils import extract_json_str

logger = logging.getLogger(__name__)


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
        # Local eval models wrap their JSON in prose/markdown fences; DeepEval
        # does a bare json.loads, so hand it just the JSON block.
        return extract_json_str(response)

    def get_model_name(self) -> str:
        return llm.model
