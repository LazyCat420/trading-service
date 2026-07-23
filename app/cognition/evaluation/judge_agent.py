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
from app.db.connection import get_db

from .oracle import DataCompletenessOracle

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
    """Run the LLM-as-a-Judge protocol on a single decision record."""
    with get_db() as db:
        failure_reason = FailureReason.NONE

        try:
            # 1. Fetch raw logs
            log = None
            _mongo_hit = False
            try:
                from app.db import mongo_store
                if mongo_store.reads_mongo("llm_audit_logs"):
                    docs = mongo_store.find_docs(
                        "llm_audit_logs", {"id": decision_id}, limit=1,
                        projection={"_id": 0, "cycle_id": 1, "ticker": 1,
                                    "context_hash": 1, "raw_response": 1, "created_at": 1},
                    )
                    if docs:
                        d = docs[0]
                        log = (d.get("cycle_id"), d.get("ticker"), d.get("context_hash"),
                               d.get("raw_response"), d.get("created_at"))
                    _mongo_hit = True
            except Exception as me:
                logger.warning("[Judge] mongo log read failed, PG fallback: %s", me)
                _mongo_hit = False
            if not _mongo_hit:
                log = db.execute(
                    "SELECT cycle_id, ticker, context_hash, raw_response, created_at "
                    "FROM llm_audit_logs WHERE id = %s",
                    [decision_id],
                ).fetchone()

            if not log:
                logger.error(f"Cannot evaluate {decision_id}. Log not found.")
                return False

            cycle_id, ticker, context_hash, raw_response, created_at = log

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
                # Count how many distinct error markers appear
                error_count = sum(1 for m in error_markers if m in raw_response)
                if error_count >= 2:
                    failure_reason = FailureReason.UNSUPPORTED_ASSET
                    logger.warning(
                        f"Unsupported asset pattern for {decision_id} ({ticker}): "
                        f"{error_count} tool errors detected in response."
                    )

            # 2. Extract Context (if available in context_blobs)
            # Fallback to minimal context if blob expired
            context_blob = "Context Blob Missing"
            full_context_blob = ""
            if context_hash:
                blob = db.execute(
                    "SELECT content FROM context_blobs WHERE context_hash = %s",
                    [context_hash],
                ).fetchone()
                if blob:
                    full_context_blob = blob[0]  # full context for ROUGE grounding
                    # Build a representative truncation: take the first 800 chars
                    # (usually macro/header), then pull the ticker-specific desk
                    # sections so the DeepEval faithfulness check can see the data
                    # the bot's reasoning references.
                    #
                    # These markers MUST match the headers the V3 desk narrative
                    # actually emits (shared_desk.get_compressed_context). The old
                    # markers ("Technicals"/"Fundamentals"/"Balance Sheet"/"Price
                    # History") appear NOWHERE in the V3 blob, so faithfulness was
                    # graded against only the first 800 chars — a header — which
                    # depressed every V3 quality score.
                    raw = blob[0]
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
                    # Robustness: if NO known section matched (format drift), fall
                    # back to a large head slice of the real content rather than
                    # grounding faithfulness on just the 800-char header.
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

            # ── EARLY EXIT: skip all LLM/DeepEval calls when input data is
            #    missing or broken.  Without real context + response the judge
            #    and DeepEval metrics just evaluate placeholder strings
            #    ("Context Blob Missing", "Empty Response"), wasting tokens and
            #    producing meaningless scores.
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

                existing = db.execute(
                    "SELECT decision_id FROM decision_evaluations WHERE decision_id = %s",
                    [decision_id],
                ).fetchone()

                eval_row = [
                    0.0,  # judge_a_score
                    0.0,  # final_quality_score
                    json.dumps(
                        []
                    ),  # red_cards (none — failure is upstream, not hallucination)
                    f"Skipped: {failure_reason.value}",  # first_principles_reasoning
                    True,  # policy_understanding
                    evidence_json,  # evidence_gathering
                ]

                if existing:
                    db.execute(
                        """UPDATE decision_evaluations SET
                            judge_a_score = %s, final_quality_score = %s,
                            red_cards = %s, first_principles_reasoning = %s,
                            policy_understanding = %s, evidence_gathering = %s
                        WHERE decision_id = %s""",
                        eval_row + [decision_id],
                    )
                else:
                    db.execute(
                        """INSERT INTO decision_evaluations (
                            decision_id, cycle_id, ticker, timestamp,
                            judge_a_score, final_quality_score, red_cards,
                            first_principles_reasoning, policy_understanding,
                            discrepancy_trigger, evidence_gathering
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [decision_id, cycle_id, ticker, created_at]
                        + eval_row[:5]
                        + [False, evidence_json],
                    )
                return True

            # 4. Construct Prompt (For subjective First-Principles Only)
            user_prompt = USER_TEMPLATE.format(
                decision_id=decision_id,
                ticker=ticker,
                context=context_blob,
                raw_response=raw_response or "Empty Response",
            )

            # 5. Grounding checks — in-house judge (replaced DeepEval 2026-07-19)
            red_cards = []
            # Evaluator crashes are infra problems, not hallucinations — they
            # must not zero the decision's quality score like a red card does.
            infra_errors = []

            grounding, grounding_infra_err = await _run_grounding_judge(
                context_blob, raw_response, decision_id, ticker
            )
            if grounding is not None:
                if grounding["faithfulness_score"] < FAITHFULNESS_THRESHOLD:
                    red_cards.append(
                        f"Faithfulness Failure (GroundingJudge): {grounding['faithfulness_reason'] or grounding['faithfulness_score']}"
                    )
                    if failure_reason == FailureReason.NONE:
                        failure_reason = FailureReason.FAITHFULNESS
                if grounding["relevancy_score"] < RELEVANCY_THRESHOLD:
                    red_cards.append(
                        f"Answer Relevancy Failure (GroundingJudge): {grounding['relevancy_reason'] or grounding['relevancy_score']}"
                    )
                    if failure_reason == FailureReason.NONE:
                        failure_reason = FailureReason.RELEVANCY
            else:
                if grounding_infra_err:
                    infra_errors.append(grounding_infra_err)
                if failure_reason == FailureReason.NONE:
                    failure_reason = FailureReason.DEEPEVAL_ERROR

            # 6. ROUGE-L Grounding Check (Semantic/Text Overlap)
            # Extract meaningful reasoning text and use full context for fair comparison
            try:
                from rouge_score import rouge_scorer

                rouge_scorer_instance = rouge_scorer.RougeScorer(
                    ["rougeL"], use_stemmer=True
                )

                # Extract only the reasoning/rationale from the raw response
                # (strips code blocks, tool calls, JSON scaffolding)
                reasoning_text = extract_reasoning_text(raw_response or "")

                # Use the full context for ROUGE reference (not the 2000-char truncation)
                # Normalize both texts for fair comparison
                rouge_reference = full_context_blob or context_blob
                norm_prediction = normalize_for_rouge(reasoning_text)
                norm_reference = normalize_for_rouge(rouge_reference)

                if norm_prediction and norm_reference:
                    rouge_scores = rouge_scorer_instance.score(
                        norm_reference, norm_prediction
                    )
                    # Use PRECISION, not F-measure.  The bot's reasoning (prediction)
                    # is always much shorter than the full context blob (reference).
                    # F-measure's recall component measures "how much of the context
                    # is covered by reasoning" — this is always near zero because the
                    # context is 10-25 KB while reasoning is ~500-1000 chars.
                    # Precision answers the right question: "How much of what the bot
                    # said actually came from the context?" — i.e., is it grounded?
                    rouge_l = round(rouge_scores["rougeL"].precision, 3)
                else:
                    rouge_l = 0.0

                # Citation overlap: fraction of numbers in reasoning found in context
                citation_score = compute_citation_overlap(
                    reasoning_text, rouge_reference
                )

                # Composite grounding score: weighted blend of ROUGE-L and citation
                grounding_score = round(
                    ROUGE_WEIGHT * rouge_l + CITATION_WEIGHT * citation_score, 3
                )

                # Store all three metrics as top-level keys so the strategy
                # auditor (and any downstream consumer) can read each
                # independently.  Legacy key "hf_rougeL" kept for backward
                # compatibility with existing DB rows.
                oracle_results["checklist"]["hf_rougeL"] = grounding_score
                oracle_results["checklist"]["raw_rougeL"] = rouge_l
                oracle_results["checklist"]["citation_overlap"] = citation_score
                oracle_results["checklist"]["grounding_score"] = grounding_score
            except Exception as hf_err:
                logger.error(
                    f"ROUGE-L grounding check failed for {decision_id}: {hf_err}"
                )

            # Optional: You can enforce Policy rules here strictly.
            policy_understanding = True

            # 7. Prompt VLLM Model (with retry consistent with DeepEval pattern)
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
            # Hybrid formula: 50% deterministic data completeness, 50% subjective causation thesis
            llm_score = float(payload.get("judge_score", 0))
            oracle_score = float(oracle_results["completeness_score"])

            base_score = round((llm_score + oracle_score) / 2.0, 2)
            # Only zero on GENUINE red cards (hallucination/relevancy failures).
            # Evaluator crashes land in infra_errors and preserve the score —
            # previously they also zeroed it, making DeepEval flakiness
            # indistinguishable from a hallucinated decision.
            final_quality_score = 0 if red_cards else base_score

            # ── Build evidence_gathering with failure metadata ──
            evidence = oracle_results["checklist"].copy()
            if failure_reason != FailureReason.NONE:
                evidence["failure_reason"] = failure_reason.value
            if infra_errors:
                evidence["deepeval_infra_errors"] = infra_errors

            # Key name kept as "deepeval_scorecard": the HeLM panel and the
            # strategy auditor read this shape from stored rows.
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
            existing = db.execute(
                "SELECT decision_id FROM decision_evaluations WHERE decision_id = %s",
                [decision_id],
            ).fetchone()

            if existing:
                db.execute(
                    """
                    UPDATE decision_evaluations SET
                        judge_a_score = %s,
                        final_quality_score = %s,
                        red_cards = %s,
                        first_principles_reasoning = %s,
                        policy_understanding = %s,
                        evidence_gathering = %s
                    WHERE decision_id = %s
                    """,
                    [
                        base_score,
                        final_quality_score,
                        red_cards_json,
                        payload.get("first_principles", ""),
                        policy_understanding,
                        evidence_json,
                        decision_id,
                    ],
                )
            else:
                db.execute(
                    """
                    INSERT INTO decision_evaluations (
                        decision_id, cycle_id, ticker, timestamp, 
                        judge_a_score, final_quality_score, red_cards, 
                        first_principles_reasoning, policy_understanding, discrepancy_trigger, evidence_gathering
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        decision_id,
                        cycle_id,
                        ticker,
                        created_at,
                        base_score,
                        final_quality_score,
                        red_cards_json,
                        payload.get("first_principles", ""),
                        policy_understanding,
                        False,
                        evidence_json,
                    ],
                )

            logger.info(
                f"Decision {decision_id} Auto-Evaluated: Score {final_quality_score}"
                f" | failure_reason={failure_reason.value}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed LLM-as-a-Judge for {decision_id}: {e}", exc_info=True)
            return False
