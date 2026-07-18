"""
Equation Lab — offline R&D that turns pitched formula stubs into REAL,
backtested strategies.

Tournament pitch personas name equations ("Mean Reversion Extension (MRE)")
and auto-save them, but with a formula-text stub whose code is
`result = {"unbacktestable": True}` — so Stage 2's backtest filter passes
every pitch through unbacktested and the jury forever sees "Backtest PnL:
N/A". The library's most-used equations (49 uses) had NO executable code.

This lab runs nightly (scheduler, off-hours): it picks the most-used
unbacktestable equations, asks the quant LLM to write REAL vectorized
signal code for the sandbox contract, validates the code end-to-end on
liquid reference tickers, saves it to the library, and runs the standard
backtest so win_rate/sharpe/max-drawdown stats become real. From then on,
tournament pitches that cite the equation get an actual backtest number in
front of the jury — closing the strategy-R&D loop the user asked for.

Energy bounds: MAX_EQUATIONS_PER_RUN per night, one code-fix retry per
equation, LOW llm priority.
"""

import json
import logging

from app.services.prism_agent_caller import llm, Priority
from app.cognition.debate.equation_library import (
    execute_equation,
    get_equation_by_name,
    save_equation,
)
from app.cognition.debate.backtest_runner import run_backtest_for_equation
from app.db.connection import get_db
from app.utils.text_utils import extract_json_str

logger = logging.getLogger(__name__)

# Per-night compile budget lives in the parameter store (EQUATION_LAB_MAX_PER_RUN).
VALIDATION_TICKERS = ["AAPL", "MSFT", "NVDA"]
MIN_SIGNALS_REQUIRED = 4          # across the validation set, else code is degenerate

CODER_SYSTEM_PROMPT = """You are a senior quantitative developer. You convert a
trading-strategy FORMULA into executable Python signal code for a restricted
sandbox.

## SANDBOX CONTRACT (STRICT)
Your code runs via exec() with ONLY these names available:
- `df`: pandas DataFrame indexed by date with columns:
  open, high, low, close, volume, rsi_14, macd, macd_signal, macd_hist,
  atr_14, support, resistance  (technical columns may contain NaN — handle it)
- `params`: dict of optional parameters
- `np`, `pd`: numpy and pandas
No imports. No file/network access. No functions from outside the sandbox.

## REQUIRED OUTPUT OF YOUR CODE
Your code MUST end with:
    result = {"signals": signals}
where `signals` is a list of dicts:
    {"date": "<YYYY-MM-DD>", "action": "BUY" or "SELL", "price": <float close>}
Rules for signals:
- Derive entry/exit logic faithfully from the FORMULA below (interpret
  thresholds sensibly when unspecified; prefer robust defaults).
- Alternate BUY/SELL (a SELL closes the prior BUY). Start flat, end however
  the data ends. No shorting.
- Vectorized pandas where practical; a simple loop over df.itertuples() is
  acceptable. NEVER fabricate signals — they must come from the rule.
- Guard NaN: skip rows where a needed input is NaN.

## RESPONSE FORMAT
Respond with ONLY a JSON object (no markdown fences, no prose):
{"code": "<the python code as one JSON string>",
 "interpretation": "<1-2 sentences: how you translated the formula into rules>"}
"""


def _pick_candidates(limit: int) -> list[dict]:
    """Most-used equations whose stored code is a non-executable stub."""
    with get_db() as db:
        rows = db.execute(
            "SELECT name, description, code, usage_count FROM quant_equation_library "
            "WHERE code ILIKE '%%unbacktestable%%' "
            "ORDER BY usage_count DESC NULLS LAST LIMIT %s",
            [limit],
        ).fetchall()
    return [
        {"name": r[0], "description": r[1] or "", "code": r[2], "usage_count": r[3] or 0}
        for r in rows
    ]


def _validate_code(code: str) -> tuple[bool, str, int]:
    """Run the candidate code on the validation tickers.

    Returns (ok, error_detail, total_signals)."""
    total_signals = 0
    last_err = ""
    ran_somewhere = False
    for ticker in VALIDATION_TICKERS:
        res = execute_equation(code, ticker)
        if res.get("error"):
            last_err = f"{ticker}: {res['error']}"
            continue
        ran_somewhere = True
        payload = res.get("result")
        if not isinstance(payload, dict) or "signals" not in payload:
            return False, f"{ticker}: result lacks 'signals' key (got {type(payload).__name__})", 0
        sigs = payload["signals"]
        if not isinstance(sigs, list):
            return False, f"{ticker}: signals is not a list", 0
        for s in sigs[:200]:
            if not (isinstance(s, dict) and s.get("action") in ("BUY", "SELL")):
                return False, f"{ticker}: malformed signal entry {str(s)[:80]}", 0
        total_signals += len(sigs)
    if not ran_somewhere:
        return False, f"code failed on every validation ticker (last: {last_err})", 0
    if total_signals < MIN_SIGNALS_REQUIRED:
        return False, f"only {total_signals} signals across {len(VALIDATION_TICKERS)} tickers — degenerate rule", total_signals
    return True, "", total_signals


async def compile_equation(name: str) -> dict:
    """Ask the LLM to write real code for one stubbed equation, validate, save,
    and backtest it. Returns a status dict."""
    eq = get_equation_by_name(name)
    if not eq:
        return {"name": name, "status": "error", "detail": "not found"}

    user_prompt = (
        f"## FORMULA TO IMPLEMENT\nName: {eq['name']}\n"
        f"Description/Formula: {eq['description'][:1200]}\n\n"
        "Write the sandbox signal code now. JSON only."
    )

    code = None
    interpretation = ""
    error_feedback = ""
    for attempt in (1, 2):
        prompt = user_prompt if not error_feedback else (
            user_prompt + f"\n\nYOUR PREVIOUS CODE FAILED VALIDATION: {error_feedback}\n"
            "Fix the code and respond with the same JSON format."
        )
        try:
            response, _, _ = await llm.chat(
                system=CODER_SYSTEM_PROMPT,
                user=prompt,
                temperature=0.2,
                max_tokens=4096,
                priority=Priority.LOW,
                agent_name="equation_lab",
            )
        except Exception as e:
            return {"name": name, "status": "error", "detail": f"llm: {type(e).__name__}: {e}"}

        try:
            parsed = json.loads(extract_json_str(response))
            code = parsed.get("code")
            interpretation = str(parsed.get("interpretation", ""))[:400]
        except Exception:
            error_feedback = "response was not parseable JSON with a 'code' string"
            continue
        if not code or not isinstance(code, str):
            error_feedback = "JSON had no 'code' string"
            continue

        ok, detail, n_signals = _validate_code(code)
        if ok:
            break
        error_feedback = detail
        logger.info("[EQLAB] %s attempt %d failed validation: %s", name, attempt, detail)
        code = None

    if not code:
        return {"name": name, "status": "failed_validation", "detail": error_feedback}

    # Persist the real implementation (save_equation upserts by name and
    # applies the dangerous-code blocklist).
    desc = eq["description"]
    if "[EQLAB]" not in (desc or ""):
        desc = f"{desc}\n[EQLAB] Compiled to executable signals. {interpretation}"[:2000]
    save_res = save_equation(name=name, code=code, description=desc, author_agent="equation_lab")
    if isinstance(save_res, dict) and save_res.get("error"):
        return {"name": name, "status": "error", "detail": f"save: {save_res['error']}"}

    # Standard backtest per validation ticker — writes win_rate/sharpe stats.
    backtests = {}
    for ticker in VALIDATION_TICKERS:
        try:
            bt = run_backtest_for_equation(name, ticker)
            backtests[ticker] = {
                k: bt.get(k) for k in ("total_trades", "win_rate_pct", "average_return_pct", "cumulative_return_pct", "max_drawdown_pct")
            } if isinstance(bt, dict) and not bt.get("error") else {"error": str(bt.get("error"))[:120] if isinstance(bt, dict) else "?"}
        except Exception as e:
            backtests[ticker] = {"error": f"{type(e).__name__}: {e}"}

    logger.info("[EQLAB] COMPILED '%s' (usage=%s): %s", name, eq.get("usage_count"),
                json.dumps(backtests)[:400])
    return {"name": name, "status": "compiled", "backtests": backtests}


async def run_equation_lab() -> dict:
    """Nightly entry point: compile up to EQUATION_LAB_MAX_PER_RUN stubbed equations."""
    from app.services.parameter_store import get_param
    candidates = _pick_candidates(int(get_param("EQUATION_LAB_MAX_PER_RUN")))
    if not candidates:
        logger.info("[EQLAB] No unbacktestable equations left — library fully executable.")
        return {"status": "idle", "compiled": 0}

    results = []
    for cand in candidates:
        logger.info("[EQLAB] Compiling '%s' (used %d times, still a stub)",
                    cand["name"], cand["usage_count"])
        try:
            results.append(await compile_equation(cand["name"]))
        except Exception as e:
            logger.error("[EQLAB] compile_equation crashed for %s: %s", cand["name"], e)
            results.append({"name": cand["name"], "status": "error", "detail": str(e)[:200]})

    compiled = sum(1 for r in results if r.get("status") == "compiled")
    logger.info("[EQLAB] Run complete: %d/%d compiled. %s",
                compiled, len(results), json.dumps([{r['name']: r['status']} for r in results])[:300])
    return {"status": "ok", "compiled": compiled, "results": results}
