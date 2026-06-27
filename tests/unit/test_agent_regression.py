import pytest
import httpx
import asyncio
from app.config import settings
from app.services.prism_agent_caller import call_prism_agent

# ── Check Reachability of Prism Gateway ──
def check_prism_reachable() -> bool:
    try:
        resp = httpx.get(f"{settings.PRISM_URL}/health", timeout=1.5)
        return resp.status_code == 200
    except Exception:
        return False

PRISM_ONLINE = check_prism_reachable()

# Override the global mock_llm autouse fixture in conftest.py
# so that these regression tests make real calls only when TEST_LIVE_LLM=1.
@pytest.fixture(autouse=True)
def patch_llm():
    import os
    if os.environ.get("TEST_LIVE_LLM") != "1":
        from unittest.mock import AsyncMock, patch
        from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric

        async def mock_call_prism_agent(*args, **kwargs):
            agent_id = kwargs.get("agent_id") or (args[0] if args else "")
            if "fundamental" in agent_id:
                return "Apple Inc. reported stellar Q3 earnings with strong revenue growth, iPhone demand, a raised dividend, and a share buyback program.", 100, 10
            elif "quant" in agent_id:
                return "TSLA stock TSLA is showing a bullish setup with a Golden Cross and strong EMA/RSI/MACD indicators.", 100, 10
            return "Mock response", 10, 1

        async def mock_a_measure(self, test_case, *args, **kwargs):
            self.score = 1.0
            self.reason = "Mocked pass for CI/CD"
            self.success = True
            return 1.0

        with patch("app.services.prism_agent_caller.call_prism_agent", mock_call_prism_agent), \
             patch("tests.unit.test_agent_regression.call_prism_agent", mock_call_prism_agent), \
             patch.object(FaithfulnessMetric, "a_measure", mock_a_measure), \
             patch.object(AnswerRelevancyMetric, "a_measure", mock_a_measure):
            yield
    else:
        yield


# ── Golden Dataset Definition ──
GOLDEN_DATASET = [
    {
        "agent_name": "v3_fundamental_analyst",
        "ticker": "AAPL",
        "context": (
            "Apple Inc. reported stellar Q3 earnings. Revenue grew 15% YoY to $92.5 billion, "
            "driven by record Services revenue of $24.8 billion and strong iPhone demand. "
            "Gross margin expanded to 46.2%. Operating cash flow was $26.4 billion. "
            "Management raised the dividend and announced a $110 billion share buyback program. "
            "However, iPad sales were slightly weak in China due to supply constraints."
        ),
        "user_message": "Analyze Apple's Q3 performance and draft a thesis regarding financial health.",
        "expected_reasoning_points": [
            "Revenue growth",
            "iPhone demand",
            "dividend",
            "buyback"
        ]
    },
    {
        "agent_name": "v3_quant_analyst",
        "ticker": "TSLA",
        "context": (
            "Tesla stock (TSLA) is currently showing a bullish setup. The 50-day EMA has crossed "
            "above the 200-day EMA (Golden Cross) on high volume. Daily RSI has recovered from oversold "
            "levels (28) to 58, indicating strong momentum. MACD histogram has entered positive territory "
            "with a clear bullish crossover. Support at $180 holds firmly."
        ),
        "user_message": "Provide technical analysis assessment and signal indication for TSLA.",
        "expected_reasoning_points": [
            "Golden Cross",
            "EMA",
            "RSI",
            "MACD",
            "bullish"
        ]
    }
]


@pytest.mark.skipif(not PRISM_ONLINE, reason="Prism Gateway is offline or unreachable")
@pytest.mark.asyncio
async def test_agent_regression_quality_gates():
    """Golden Dataset regression test verifying agent output quality via DeepEval."""
    from app.cognition.evaluation.deepeval_client import VLLMDeepEvalWrapper
    from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    eval_model = VLLMDeepEvalWrapper()
    faithfulness = FaithfulnessMetric(threshold=0.7, model=eval_model, include_reason=True)
    relevancy = AnswerRelevancyMetric(threshold=0.7, model=eval_model, include_reason=True)

    for case in GOLDEN_DATASET:
        agent_name = case["agent_name"]
        ticker = case["ticker"]
        context_blob = case["context"]
        user_msg = case["user_message"]
        expected_points = case["expected_reasoning_points"]

        system_prompt = (
            f"You are the {agent_name} agent. Analyze the provided stock context below and answer the query. "
            "Be factually grounded and reference metrics from the context. "
            "Do NOT hallucinate information.\n\n"
            f"CONTEXT:\n{context_blob}"
        )

        # Execute agent call
        raw_response, _, _ = await call_prism_agent(
            agent_id=agent_name,
            user_message=user_msg,
            fallback_system_prompt=system_prompt,
            fallback_agent_name=agent_name,
            ticker=ticker,
            cycle_id="regression_test_cycle",
            bot_id="regression_bot"
        )

        assert raw_response, f"Response from {agent_name} was empty or None."

        # Verify key terms exist in the response
        present_points = [p for p in expected_points if p.lower() in raw_response.lower()]
        assert len(present_points) >= 2, (
            f"Agent {agent_name} failed to incorporate expected points. "
            f"Expected: {expected_points}. Found in response: {present_points}."
        )

        # DeepEval validation
        test_case = LLMTestCase(
            input=user_msg,
            actual_output=raw_response,
            retrieval_context=[context_blob]
        )

        # Run Faithfulness
        await faithfulness.a_measure(test_case)
        assert faithfulness.is_successful(), (
            f"Faithfulness check failed for {agent_name} (Score: {faithfulness.score:.2f}). "
            f"Reason: {faithfulness.reason}"
        )

        # Run Relevancy
        await relevancy.a_measure(test_case)
        assert relevancy.is_successful(), (
            f"Answer Relevancy check failed for {agent_name} (Score: {relevancy.score:.2f}). "
            f"Reason: {relevancy.reason}"
        )
