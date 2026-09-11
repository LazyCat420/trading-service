"""Render code-verified financial relationships selected by the deciding agent.

Version two keeps action, confidence, sizing and evidence selection authored by
the model. The financial sentences and numerical provenance are rendered by
code. Conflicting authored sentences are rejected, never silently corrected.
"""
from copy import deepcopy
import json

from app.v3.financial_evidence import calculated_facts, number


def reasoning_catalog(record):
    facts = calculated_facts(record)
    steps = {}

    def add(key, ids, statement):
        if all(i in facts for i in ids):
            steps[key] = {"id": key, "fact_ids": ids,
                          "statement": statement + " " + " ".join(f"[{i}]" for i in ids)}

    def available(key):
        return key in facts and facts[key]["status"] == "known"

    def sign_step(key, label):
        if key not in facts:
            return
        value = number(facts[key]["value"])
        state = "unavailable" if value is None else "positive" if value > 0 else "negative" if value < 0 else "zero"
        add(key, [key], f"The supplied {label} is {state}.")

    for key, label in (("operating_margin_pct", "operating margin"),
                       ("free_cash_flow", "free cash flow"),
                       ("gross_margin_pct", "gross margin"), ("roic_pct", "return on invested capital")):
        sign_step(key, label)
    if "calc_headroom_pct" in facts:
        value = number(facts["calc_headroom_pct"]["value"])
        text = ("Reserved concentration capacity is unknown." if value is None else
                "The single-name limit leaves no additional capacity." if value <= 0 else
                "The remaining single-name capacity limits additional sizing.")
        add("headroom", ["calc_headroom_pct"], text)
    if "calc_proposal_fits" in facts:
        value = facts["calc_proposal_fits"]["value"]
        add("proposal_fit", ["calc_proposal_fits"],
            "The supplied purchase fits the available capacity." if value is True else
            "The supplied purchase exceeds the available capacity." if value is False else
            "Whether the supplied purchase fits remains unresolved.")
    if "calc_range_position_pct" in facts:
        value = number(facts["calc_range_position_pct"]["value"])
        text = ("The close's range location is unknown." if value is None else
                "The close is below support." if value < 0 else
                "The close is above resistance." if value > 100 else
                "The close is at the range midpoint." if value == 50 else
                "The close is nearer support than resistance." if value < 50 else
                "The close is nearer resistance than support.")
        add("range_position", ["calc_range_position_pct"], text)
    if "calc_holding_return_pct" in facts:
        value = number(facts["calc_holding_return_pct"]["value"])
        text = ("The holding return is unknown." if value is None else
                "The position is below its acquisition cost." if value < 0 else
                "The position is above its acquisition cost." if value > 0 else
                "The position is at its acquisition cost.")
        add("holding_return", ["calc_holding_return_pct"], text)
    if "calc_forward_peg" in facts:
        add("forward_peg", ["calc_forward_peg"],
            "The forward growth-adjusted multiple uses the supplied next-year earnings growth."
            if available("calc_forward_peg") else
            "The required earnings-growth observation is unavailable, so forward PEG is unresolved.")
    for scenario in ("current", "conditional"):
        key = f"calc_proposed_{scenario}_reward_risk"
        if key not in facts:
            continue
        value = number(facts[key]["value"])
        context = "at the current reference price" if scenario == "current" else "at the hypothetical future entry"
        text = (f"Reward/risk {context} is unknown." if value is None else
                f"Prospective gain exceeds defined downside {context}." if value > 1 else
                f"Defined downside exceeds prospective gain {context}." if value < 1 else
                f"Prospective gain and defined downside balance {context}.")
        add(f"{scenario}_reward_risk", [key], text)
    for key, label in (("volume_five_session_trend", "dated volume trend"),
                       ("sma_200", "long moving average"),
                       ("debt_to_equity_prior", "prior leverage observation")):
        if key in facts:
            add(key, [key], f"The supplied {label} is " + ("available." if available(key) else "unavailable."))
    if "underlying_filing_count" in facts:
        add("source_count", ["underlying_filing_count"],
            "The repeated reports share an underlying filing and do not provide independent corroboration."
            if facts["underlying_filing_count"]["value"] == 1 else
            "The count identifies the underlying filings represented in the supplied reports.")
    if facts.get("guidance_record", {}).get("value") == "met":
        add("guidance", ["guidance_record"], "The supplied record says management met its guidance.")
    if "support" in facts and "rsi_14" in facts:
        add("price_and_oscillator", ["support", "rsi_14"], "Price support and the oscillator measure different quantities.")
    for key, label in (("gross_margin_pct", "gross margin"), ("roic_pct", "return on invested capital")):
        peer = "sector_" + key
        if available(key) and available(peer):
            left, right = number(facts[key]["value"]), number(facts[peer]["value"])
            if left is not None and right is not None and facts[key]["unit"] == facts[peer]["unit"]:
                relation = "exceeds" if left > right else "is below" if left < right else "matches"
                add("compare_" + key, [key, peer], f"The supplied company {label} {relation} the sector's same metric.")
    # Source IDs are an equivalent explicit selection form. Map them to the
    # most specific code relationship that uses the observation; never guess
    # an unknown ID or supply evidence the model did not select.
    primary = list(steps.values())
    for key, fact in facts.items():
        matches = [step for step in primary if key in step['fact_ids']]
        chosen = next((step for step in matches if step['fact_ids'] == [key]), None)
        if chosen:
            if key not in steps:
                steps[key] = {**chosen, 'id':key}
            steps['observation:'+key] = {**chosen, 'id':'observation:'+key}
        else:
            step = {'id':key,'fact_ids':[key], 'statement':'This supplied observation is '+('available.' if fact['status']=='known' else 'unavailable.')+' ['+key+']'}
            steps[key] = step
            steps['observation:'+key] = {**step,'id':'observation:'+key}
    return steps


def reasoning_prompt(record):
    return ("\n## SELECTABLE FINANCIAL REASONING STEPS\n"
            "These sentences and their numeric records are computed by code. Select the steps that support YOUR decision. "
            "Do not write financial sentences or recopy values. For each question select the steps that answer its actual subject.\n"
            + json.dumps([step for key,step in reasoning_catalog(record).items() if not key.startswith('observation:')], separators=(",", ":")))


def render_reasoning_artifact(artifact, record):
    """Return (rendered artifact, errors); verify supplied rendered fields too."""
    if not isinstance(artifact, dict) or artifact.get("financial_reasoning_version") != 2:
        return artifact, []
    result = deepcopy(artifact)
    errors = []
    catalog = reasoning_catalog(record)
    facts = calculated_facts(record)
    selected = []

    def selection_help(field):
        # Guide a new model selection, without changing the rejected artifact.
        hint = " Available step IDs: " + json.dumps(sorted(catalog)) + "."
        if field.startswith("research_answers."):
            from app.v3.financial_claims import _question_requirements
            question_id = field.removeprefix("research_answers.")
            question = next((q for q in record.get("questions", [])
                             if q["id"] == question_id), None)
            if question:
                requirements = _question_requirements(question["question"])
                candidates = [step for key, step in catalog.items()
                              if not key.startswith("observation:") and requirements
                              and any(set(step["fact_ids"]) & group for group in requirements)]
                hint += " Relevant evidence candidates (select enough to cover every part): " + json.dumps(candidates) + "."
        return hint

    def select(ids, field):
        if not isinstance(ids, list) or not ids:
            errors.append(f"{field}: select nonempty step IDs from the supplied catalog." + selection_help(field))
            return []
        unknown = [i for i in ids if not isinstance(i, str) or i not in catalog]
        if unknown:
            errors.append(f"{field}: unknown step IDs {json.dumps(unknown)}; select only IDs from the supplied catalog." + selection_help(field))
            return []
        # Repeating the same source reference does not add evidence or weight.
        # Preserve authored selection lists, but render each chosen step once.
        chosen = [catalog[i] for i in dict.fromkeys(ids)]
        selected.extend(f for step in chosen for f in step["fact_ids"])
        return chosen

    chosen = select(result.get("reasoning_steps"), "reasoning_steps")
    expected_reasoning = " ".join(dict.fromkeys(step["statement"] for step in chosen))
    if "reasoning" in result and result["reasoning"] != expected_reasoning:
        errors.append("reasoning conflicts with the selected code-verified steps; remove authored financial prose.")
    else:
        result["reasoning"] = expected_reasoning
    questions = {q["id"]: q for q in record.get("questions", [])}
    answers = result.get("research_answers")
    if isinstance(answers, dict):
        mapped = []
        for item_id, value in answers.items():
            if isinstance(value, list):
                mapped.append({'item_id':item_id, 'step_ids':value})
            elif isinstance(value, dict) and ('item_id' not in value or value['item_id']==item_id):
                mapped.append({**value,'item_id':item_id})
            else:
                errors.append('research_answers map contains a conflicting question identity or invalid selection.')
        answers = mapped
    if not isinstance(answers, list):
        answers = []
        errors.append("research_answers must contain the supplied question IDs and selected step_ids.")
    rendered_answers = []
    # Imported lazily to keep the catalog independent of the validator.
    from app.v3.financial_claims import _question_requirements, _supports_question_date
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("item_id"), str) or answer["item_id"] not in questions:
            errors.append("research_answers contains an unknown question ID. Allowed question IDs: "
                          + json.dumps(list(questions)) + "."
                          + (" Return research_answers: []; the financial record has no questions." if not questions else ""))
            continue
        q = questions[answer["item_id"]]
        steps = select(answer.get("step_ids"), "research_answers." + answer["item_id"])
        ids = list(dict.fromkeys(f for step in steps for f in step["fact_ids"]))
        requirements = _question_requirements(q["question"])
        supported = bool(requirements) and all(set(ids) & group for group in requirements)
        known = bool(ids) and all(facts[i]["status"] == "known" and _supports_question_date(facts[i], q["question"]) for i in ids)
        statements = list(dict.fromkeys(step["statement"] for step in steps))
        # Complete a requested relationship only from operands the model
        # actually selected. This never adds a missing source observation.
        for step in catalog.values():
            operands = set(step['fact_ids'])
            if (len(operands) > 1 and operands <= set(ids) and requirements
                    and all(any(f in group for group in requirements) for f in operands)
                    and step['statement'] not in statements):
                statements.append(step['statement'])
        expected = {"question": q["question"], "fact_ids": ids,
                    "status": "answered" if supported and known else "unresolved",
                    "answer": " ".join(statements)}
        if not supported:
            expected["answer"] = "The selected evidence does not establish an answer to the requested question."
        rendered = deepcopy(answer)
        for key, value in expected.items():
            if key in answer and answer[key] != value:
                errors.append(f"research_answers.{answer['item_id']}.{key} conflicts with the selected evidence.")
            else:
                rendered[key] = value
        rendered_answers.append(rendered)
    result["research_answers"] = rendered_answers
    selected = list(dict.fromkeys(selected))
    claims = [{"fact_id": i, **{k: facts[i][k] for k in ("metric", "value", "unit", "as_of", "source")}} for i in selected]
    if "financial_claims" in result and result["financial_claims"] != claims:
        errors.append("financial_claims conflicts with the source records selected by the reasoning steps.")
    else:
        result["financial_claims"] = claims
    result["_financial_explanation_provenance"] = "model_selected_steps_code_rendered_statements"
    return (deepcopy(artifact) if errors else result), errors
