import asyncio
import json
import logging
import os
from enum import Enum

from app.services.prism_agent_caller import llm, Priority
from app.utils.text_utils import (
    parse_json_response,
    extract_reasoning_text,
    normalize_for_rouge,
    compute_citation_overlap,
)
from app.db.mongo_store import handle_mongo_read_failure

from .oracle import DataCompletenessOracle
from app.db import mongo_query
from app.db import mongo_store

# Grounding score weights: ROUGE-L precision (textual overlap) vs citation
# overlap (numeric data point grounding).  Citation is weighted higher because
# it directly measures whether the bot references actual data values from the
# context.  Increase ROUGE_WEIGHT if the LLM style becomes more verbose.
ROUGE_WEIGHT = 0.4
CITATION_WEIGHT = 0.6

# ── Failure classification codes ──
# Stored in evidence_gathering["failure_reason"] so the UI/strategy auditor
# can group low scores by root cause instead of just by number.


class FailureReason(Enum):
    NONE = "none"
    PARSE = "parse_failure"
    MISSING_CONTEXT = "missing_context"
    UNSUPPORTED_ASSET = "unsupported_asset"
    FAITHFULNESS = "faithfulness_failure"
    RELEVANCY = "relevancy_failure"
    DEEPEVAL_ERROR = "deepeval_error"
    EMPTY_RESPONSE = "empty_response"


# ── DeepEval metric thresholds (override via env or Settings) ──
FAITHFULNESS_THRESHOLD = float(os.environ.get("FAITHFULNESS_THRESHOLD", "0.7"))
RELEVANCY_THRESHOLD = float(os.environ.get("RELEVANCY_THRESHOLD", "0.5"))
# How far below its threshold a grounding metric must fall before the red card
# costs the WHOLE score. A metric sitting just under the line is a doubt; a
# metric near zero is an ungrounded answer, and those are not the same finding.
GROUNDING_TOTAL_LOSS_AT = float(os.environ.get("GROUNDING_TOTAL_LOSS_AT", "0.0"))


def grounding_shortfall(score: float, threshold: float) -> float:
    """How badly a grounding metric missed, normalised to 0..1.

    0.0 sitting just under `threshold`, 1.0 at GROUNDING_TOTAL_LOSS_AT (and
    anywhere below it). Module level so the penalty is testable without an
    LLM: the whole point of the change is that this number is no longer a
    boolean.
    """
    span = threshold - GROUNDING_TOTAL_LOSS_AT
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (threshold - float(score)) / span))


def apply_red_card_penalty(base_score: float, shortfalls: list[float],
                           has_red_cards: bool) -> tuple[float, float]:
    """(final_score, penalty) for a judged decision.

    A red card deducts in proportion to how far below threshold the metric
    actually sat, rather than zeroing the score outright. A card with no
    score behind it keeps the old conservative full penalty.
    """
    if not has_red_cards:
        return base_score, 0.0
    penalty = max(shortfalls) if shortfalls else 1.0
    return round(base_score * (1.0 - penalty), 2), penalty


# Max seconds to wait for a single DeepEval metric call before treating as error
DEEPEVAL_TIMEOUT_SEC = float(os.environ.get("DEEPEVAL_TIMEOUT_SEC", "180"))
# Max retries per DeepEval metric call before recording a red card
DEEPEVAL_MAX_RETRIES = int(os.environ.get("DEEPEVAL_MAX_RETRIES", "2"))
# Max concurrent DeepEval evaluations to prevent vLLM saturation
_DEEPEVAL_CONCURRENCY = int(os.environ.get("MAX_CONCURRENT_DEEPEVAL", "3"))
_deepeval_semaphore = asyncio.Semaphore(_DEEPEVAL_CONCURRENCY)

# ── DeepEval circuit breaker ──
# When the eval model can't produce DeepEval-parseable output, EVERY metric
# call fails after retries (~2min per decision, observed exhausting the
# strategy auditor's whole backfill budget on 2/4 decisions). After
# _DEEPEVAL_BREAKER_LIMIT consecutive metric failures, skip DeepEval for
# _DEEPEVAL_BREAKER_COOLDOWN_SEC and score with the local judge + ROUGE only.
_DEEPEVAL_BREAKER_LIMIT = int(os.environ.get("DEEPEVAL_BREAKER_LIMIT", "4"))
_DEEPEVAL_BREAKER_COOLDOWN_SEC = float(os.environ.get("DEEPEVAL_BREAKER_COOLDOWN_SEC", str(6 * 3600)))
_deepeval_consecutive_failures = 0
_deepeval_disabled_until = 0.0


def _deepeval_breaker_open() -> bool:
    import time as _time
    return _time.monotonic() < _deepeval_disabled_until


def _deepeval_record_outcome(success: bool) -> None:
    """Track consecutive metric failures; open the breaker at the limit."""
    global _deepeval_consecutive_failures, _deepeval_disabled_until
    import time as _time
    if success:
        _deepeval_consecutive_failures = 0
        return
    _deepeval_consecutive_failures += 1
    if _deepeval_consecutive_failures >= _DEEPEVAL_BREAKER_LIMIT:
        _deepeval_disabled_until = _time.monotonic() + _DEEPEVAL_BREAKER_COOLDOWN_SEC
        _deepeval_consecutive_failures = 0
        logger.warning(
            "[JUDGE] DeepEval breaker OPEN — %d consecutive metric failures; "
            "skipping DeepEval metrics for %.0f min (local judge + ROUGE still run).",
            _DEEPEVAL_BREAKER_LIMIT, _DEEPEVAL_BREAKER_COOLDOWN_SEC / 60,
        )


GROUNDING_JUDGE_SYSTEM = """You are a strict, impartial grounding evaluator for a quantitative trading firm.
Given SOURCE CONTEXT (collected market data) and a DECISION OUTPUT (a trading bot's decision), score two things:

1. faithfulness_score (0.0-1.0): Is every factual claim in the decision supported by the source context?
   1.0 = every number, fact, and characterization traces to the context.
   0.0 = the decision invents facts or contradicts the context.
2. relevancy_score (0.0-1.0): Does the decision actually address the analyzed ticker and use the provided data,
   rather than generic boilerplate that could apply to any stock?

Return EXACTLY this JSON, no prose, no markdown fences:
{
    "faithfulness_score": <float 0.0-1.0>,
    "faithfulness_reason": "<one sentence: the worst unsupported claim, or why it is fully supported>",
    "relevancy_score": <float 0.0-1.0>,
    "relevancy_reason": "<one sentence>"
}"""

GROUNDING_JUDGE_TEMPLATE = """### SOURCE CONTEXT
{context}

### DECISION OUTPUT
{output}"""


async def _run_grounding_judge(context_blob: str, raw_response: str, decision_id: str, ticker: str) -> tuple[dict | None, str | None]:
    """In-house replacement for DeepEval Faithfulness/AnswerRelevancy.

    DeepEval's bare json.loads rejected local-model output on most rows
    ("Evaluation LLM outputted an invalid JSON"), so grounding was silently
    dead. Same judge LLM, our own schema, parsed with parse_json_response
    (fence-stripping + repair). Reuses the DeepEval breaker/semaphore so a
    broken judge model still can't stall the backfill budget.

    Returns (scores_dict, infra_error) — exactly one is non-None.
    """
    if _deepeval_breaker_open():
        logger.info("[JUDGE] Grounding breaker open — skipping for %s", decision_id)
        return None, "Grounding judge skipped: circuit breaker open"

    last_err: Exception | None = None
    for attempt in range(DEEPEVAL_MAX_RETRIES):
        try:
            async with _deepeval_semaphore:
                response, _, _ = await asyncio.wait_for(
                    llm.chat(
                        system=GROUNDING_JUDGE_SYSTEM,
                        user=GROUNDING_JUDGE_TEMPLATE.format(
                            context=context_blob, output=(raw_response or "Empty Response")[:8000],
                        ),
                        temperature=0.0,
                        max_tokens=1024,
                        priority=Priority.HIGH,
                        agent_name="grounding_judge",
                        ticker=ticker,
                    ),
                    timeout=DEEPEVAL_TIMEOUT_SEC,
                )
            payload = parse_json_response(response)
            scores = {
                "faithfulness_score": max(0.0, min(1.0, float(payload["faithfulness_score"]))),
                "relevancy_score": max(0.0, min(1.0, float(payload["relevancy_score"]))),
                "faithfulness_reason": str(payload.get("faithfulness_reason") or "")[:500],
                "relevancy_reason": str(payload.get("relevancy_reason") or "")[:500],
            }
            _deepeval_record_outcome(True)
            return scores, None
        except Exception as eval_err:
            last_err = eval_err
            if attempt < DEEPEVAL_MAX_RETRIES - 1:
                logger.warning(
                    "Grounding judge attempt %d failed for %s: %s — retrying",
                    attempt + 1, decision_id, eval_err,
                )
                await asyncio.sleep(2)
    logger.error(
        "Grounding judge failed for %s after %d attempts: %s",
        decision_id, DEEPEVAL_MAX_RETRIES, last_err,
    )
    _deepeval_record_outcome(False)
    return None, f"Grounding Judge Error: {type(last_err).__name__}: {last_err}"

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an independent, institutional Auditor Agent (LLM-as-a-Judge) for a quantitative trading firm.
Your job is to strictly evaluate the trading bot's proposed causal thesis using the First-Principles framework.
You DO NOT need to check data completeness or hallucinations, as deterministic systems handle those.
You care ONLY about grading the depth of the Causal Thesis.

### SCORING ANCHORS (1-5 Scale for Causal Thesis)
1 - Poor: Hallucinated connection, forced reasoning, or random associations.
2 - Weak: Contradicts context or anchors to bias rather than data.
3 - Adequate: Basic pattern matching without causal depth.
4 - Strong: Sound logic but misses minor elements like explicit invalidation.
5 - Excellent: Deep causal thesis supported by context, includes invalidation levels.

Return EXACTLY the following JSON format:
{
    "judge_score": <int 1-5>,
    "first_principles": "<A brief string extracting the bot's causal thesis, or explaining why it's missing>"
}
"""

USER_TEMPLATE = """### Decision ID: {decision_id}
### Asset: {ticker}

### Raw Context from Bot (What it saw):
{context}

### Bot's Raw Reasoning (What it decided):
{raw_response}

Act as the Auditor and score this decision. Output JSON only.
"""


async def evaluate_decision(decision_id: str) -> bool:
    """Run the LLM-as-a-Judge protocol on a single decision record in pure MongoDB."""
    failure_reason = FailureReason.NONE

    try:
        # 1. Fetch raw logs
        docs = mongo_store.find_docs(
            "llm_audit_logs", {"id": decision_id}, limit=1,
            projection={"_id": 0, "cycle_id": 1, "ticker": 1,
                        "context_hash": 1, "raw_response": 1, "created_at": 1},
        )
        if not docs:
            logger.error(f"Cannot evaluate {decision_id}. Log not found.")
            return False

        d = docs[0]
        cycle_id = d.get("cycle_id")
        ticker = d.get("ticker")
        context_hash = d.get("context_hash")
        raw_response = d.get("raw_response")
        created_at = d.get("created_at")

        # ── Classify empty/missing response early ──
        if not raw_response or raw_response.strip() == "":
            failure_reason = FailureReason.EMPTY_RESPONSE
            logger.warning(f"Empty response for {decision_id}.")

        # ── Classify parse failures: check if FINAL() can be extracted ──
        if failure_reason == FailureReason.NONE and raw_response:
            from app.utils.text_utils import parse_trading_decision

            parsed_decision = parse_trading_decision(raw_response)
            if not parsed_decision or "action" not in parsed_decision:
                failure_reason = FailureReason.PARSE
                logger.warning(
                    f"Parse failure for {decision_id}: no valid FINAL() found."
                )

        # ── Classify unsupported asset: check for tool error markers ──
        if failure_reason == FailureReason.NONE and raw_response:
            error_markers = [
                '"error":',
                "'error':",
                "No technicals for",
                "No fundamentals for",
                "No price data for",
                "No data for",
            ]
            error_count = sum(1 for m in error_markers if m in raw_response)
            if error_count >= 2:
                failure_reason = FailureReason.UNSUPPORTED_ASSET
                logger.warning(
                    f"Unsupported asset pattern for {decision_id} ({ticker}): "
                    f"{error_count} tool errors detected in response."
                )

        # 2. Extract Context (if available in context_blobs)
        context_blob = "Context Blob Missing"
        full_context_blob = ""
        if context_hash:
            blob_docs = mongo_store.find_docs("context_blobs", {"context_hash": context_hash}, limit=1)
            if blob_docs:
                raw = blob_docs[0].get("content", "")
                full_context_blob = raw
                parts = [raw[:800]]
                matched = 0
                for section_marker in (
                    "## Fundamental Analysis",
                    "## Quantitative / Risk Analysis",
                    "## Market Regime",
                    "## Tournament Debate Verdict",
                    "## Debate Judge Verdict",
                    "## Board of Directors Verdict",
                    "## Junior Analyst Notes",
                ):
                    idx = raw.find(section_marker)
                    if idx >= 0:
                        parts.append(raw[idx : idx + 600])
                        matched += 1
                if matched == 0:
                    context_blob = raw[:3000]
                else:
                    context_blob = "\n...\n".join(parts)[:3000]
            else:
                if failure_reason == FailureReason.NONE:
                    failure_reason = FailureReason.MISSING_CONTEXT
                logger.warning(
                    f"Context blob missing for {decision_id} (hash={context_hash})"
                )
        else:
            if failure_reason == FailureReason.NONE:
                failure_reason = FailureReason.MISSING_CONTEXT
            logger.warning(f"No context_hash for {decision_id}")

        # 3. Deterministic Ground Truth Checks
        oracle_results = DataCompletenessOracle.verify_ground_truth(ticker)

        _SKIP_LLM_FAILURES = {
            FailureReason.EMPTY_RESPONSE,
            FailureReason.MISSING_CONTEXT,
            FailureReason.UNSUPPORTED_ASSET,
            FailureReason.PARSE,
        }
        if failure_reason in _SKIP_LLM_FAILURES:
            logger.info(
                "Skipping LLM evaluation for %s — pre-check failure: %s",
                decision_id,
                failure_reason,
            )
            evidence = oracle_results["checklist"].copy()
            evidence["failure_reason"] = failure_reason.value
            evidence["skipped_llm"] = True
            evidence_json = json.dumps(evidence)

            existing = mongo_query.find_row('decision_evaluations', {'decision_id': decision_id}, ['decision_id'])

            if existing:
                mongo_store.update_docs('decision_evaluations', {'decision_id': decision_id}, {'$set': {
                    'judge_a_score': 0.0,
                    'final_quality_score': 0.0,
                    'red_cards': json.dumps([]),
                    'first_principles_reasoning': f"Skipped: {failure_reason.value}",
                    'policy_understanding': True,
                    'evidence_gathering': evidence_json,
                }})
            else:
                mongo_store.insert_docs('decision_evaluations', [{
                    'decision_id': decision_id,
                    'cycle_id': cycle_id,
                    'ticker': ticker,
                    'timestamp': created_at,
                    'judge_a_score': 0.0,
                    'final_quality_score': 0.0,
                    'red_cards': json.dumps([]),
                    'first_principles_reasoning': f"Skipped: {failure_reason.value}",
                    'policy_understanding': True,
                    'discrepancy_trigger': False,
                    'evidence_gathering': evidence_json,
                }])
            return True

        # 4. Construct Prompt
        user_prompt = USER_TEMPLATE.format(
            decision_id=decision_id,
            ticker=ticker,
            context=context_blob,
            raw_response=raw_response or "Empty Response",
        )

        # 5. Grounding checks — in-house judge
        red_cards = []
        # How far below threshold each red card sat, normalised to 0..1 where
        # 1.0 means the metric bottomed out. Kept parallel to red_cards so the
        # penalty can be graded instead of binary — see the note at the
        # final_quality_score computation below.
        red_card_shortfalls: list[float] = []
        infra_errors = []

        grounding, grounding_infra_err = await _run_grounding_judge(
            context_blob, raw_response, decision_id, ticker
        )
        if grounding is not None:
            if grounding["faithfulness_score"] < FAITHFULNESS_THRESHOLD:
                red_cards.append(
                    f"Faithfulness Failure (GroundingJudge): {grounding['faithfulness_reason'] or grounding['faithfulness_score']}"
                )
                red_card_shortfalls.append(
                    grounding_shortfall(grounding["faithfulness_score"], FAITHFULNESS_THRESHOLD)
                )
                if failure_reason == FailureReason.NONE:
                    failure_reason = FailureReason.FAITHFULNESS
            if grounding["relevancy_score"] < RELEVANCY_THRESHOLD:
                red_cards.append(
                    f"Answer Relevancy Failure (GroundingJudge): {grounding['relevancy_reason'] or grounding['relevancy_score']}"
                )
                red_card_shortfalls.append(
                    grounding_shortfall(grounding["relevancy_score"], RELEVANCY_THRESHOLD)
                )
                if failure_reason == FailureReason.NONE:
                    failure_reason = FailureReason.RELEVANCY
        else:
            if grounding_infra_err:
                infra_errors.append(grounding_infra_err)
            if failure_reason == FailureReason.NONE:
                failure_reason = FailureReason.DEEPEVAL_ERROR

        # 6. ROUGE-L Grounding Check
        try:
            from rouge_score import rouge_scorer

            rouge_scorer_instance = rouge_scorer.RougeScorer(
                ["rougeL"], use_stemmer=True
            )

            reasoning_text = extract_reasoning_text(raw_response or "")
            rouge_reference = full_context_blob or context_blob
            norm_prediction = normalize_for_rouge(reasoning_text)
            norm_reference = normalize_for_rouge(rouge_reference)

            if norm_prediction and norm_reference:
                rouge_scores = rouge_scorer_instance.score(
                    norm_reference, norm_prediction
                )
                rouge_l = round(rouge_scores["rougeL"].precision, 3)
            else:
                rouge_l = 0.0

            citation_score = compute_citation_overlap(
                reasoning_text, rouge_reference
            )

            grounding_score = round(
                ROUGE_WEIGHT * rouge_l + CITATION_WEIGHT * citation_score, 3
            )

            oracle_results["checklist"]["hf_rougeL"] = grounding_score
            oracle_results["checklist"]["raw_rougeL"] = rouge_l
            oracle_results["checklist"]["citation_overlap"] = citation_score
            oracle_results["checklist"]["grounding_score"] = grounding_score
        except Exception as hf_err:
            logger.error(
                f"ROUGE-L grounding check failed for {decision_id}: {hf_err}"
            )

        policy_understanding = True

        # 7. Prompt VLLM Model
        eval_response = None
        for attempt in range(DEEPEVAL_MAX_RETRIES):
            try:
                eval_response, tokens, ms = await asyncio.wait_for(
                    llm.chat(
                        system=SYSTEM_PROMPT,
                        user=user_prompt,
                        temperature=0.1,
                        max_tokens=256,
                        priority=Priority.HIGH,
                        agent_name="judge_evaluator",
                        ticker=ticker,
                    ),
                    timeout=DEEPEVAL_TIMEOUT_SEC,
                )
                break
            except Exception as api_err:
                if attempt < DEEPEVAL_MAX_RETRIES - 1:
                    logger.warning(
                        "llm.chat attempt %d failed for %s: %s — retrying",
                        attempt + 1,
                        decision_id,
                        api_err,
                    )
                    await asyncio.sleep(2)
                else:
                    logger.error(
                        "llm.chat failed for %s after %d attempts: %s",
                        decision_id,
                        DEEPEVAL_MAX_RETRIES,
                        api_err,
                    )
                    raise api_err

        payload = parse_json_response(eval_response)

        # 8. Calculate Final Hybrid Auto-Score
        llm_score = float(payload.get("judge_score", 0))
        oracle_score = float(oracle_results["completeness_score"])

        base_score = round((llm_score + oracle_score) / 2.0, 2)
        # A red card used to zero the score outright. Measured over the seven
        # days to 2026-09-04: 7 of 22 judged decisions were zeroed while their
        # judge_a_score was 3.5-4.5, dragging the 7d judge mean from 4.19
        # (83.8%) to 2.92 (58.3%). That single binary rule was simultaneously
        # firing the "LLM-judge decision quality low" finding, costing LLM
        # Performance ~7.6 points, and tripping the Goodhart tripwire (7/22 =
        # 32% against a 10% rate) -- three dashboard indicators moved by one
        # if-statement. Reading the seven card texts, at least three said the
        # claims WERE supported and red-carded anyway.
        #
        # The card still deducts, proportionally to how far below threshold the
        # metric actually sat: a hair under the line barely moves the score, a
        # metric at zero still takes the whole thing. A red card with no score
        # behind it (none exist today, but the list is append-only) keeps the
        # old conservative full penalty.
        final_quality_score, penalty = apply_red_card_penalty(
            base_score, red_card_shortfalls, bool(red_cards)
        )

        evidence = oracle_results["checklist"].copy()
        evidence["red_card_penalty"] = round(penalty, 3)
        if failure_reason != FailureReason.NONE:
            evidence["failure_reason"] = failure_reason.value
        if infra_errors:
            evidence["deepeval_infra_errors"] = infra_errors

        evidence["deepeval_scorecard"] = {
            "faithfulness": {
                "score": grounding["faithfulness_score"] if grounding else None,
                "reason": grounding["faithfulness_reason"] if grounding else None,
                "passed": (grounding["faithfulness_score"] >= FAITHFULNESS_THRESHOLD) if grounding else False,
            },
            "relevancy": {
                "score": grounding["relevancy_score"] if grounding else None,
                "reason": grounding["relevancy_reason"] if grounding else None,
                "passed": (grounding["relevancy_score"] >= RELEVANCY_THRESHOLD) if grounding else False,
            },
        }

        evidence_json = json.dumps(evidence)
        red_cards_json = json.dumps(red_cards)

        # Upsert Evaluator Score
        existing = mongo_query.find_row('decision_evaluations', {'decision_id': decision_id}, ['decision_id'])

        if existing:
            mongo_store.update_docs('decision_evaluations', {'decision_id': decision_id}, {'$set': {'judge_a_score': base_score, 'final_quality_score': final_quality_score, 'red_cards': red_cards_json, 'first_principles_reasoning': payload.get("first_principles", ""), 'policy_understanding': policy_understanding, 'evidence_gathering': evidence_json}})
        else:
            mongo_store.insert_docs('decision_evaluations', [{'decision_id': decision_id, 'cycle_id': cycle_id, 'ticker': ticker, 'timestamp': created_at, 'judge_a_score': base_score, 'final_quality_score': final_quality_score, 'red_cards': red_cards_json, 'first_principles_reasoning': payload.get("first_principles", ""), 'policy_understanding': policy_understanding, 'discrepancy_trigger': False, 'evidence_gathering': evidence_json}])

        logger.info(
            f"Decision {decision_id} Auto-Evaluated: Score {final_quality_score}"
            f" | failure_reason={failure_reason.value}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed LLM-as-a-Judge for {decision_id}: {e}", exc_info=True)
        return False
