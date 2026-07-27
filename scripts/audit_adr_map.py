"""Verify every hard-coded ADR mapping against live market data.

KNOWN_ADR_MAP silently rewrites a requested ticker. If an entry is wrong,
collection, analysis, the decision and the trade all happen against a
different security and nothing in the cycle says so. A wrong entry is
therefore worse than a missing one — a missing one just drops the ticker.

Two independent checks, because either alone has been wrong here:

  LIQUIDITY  the destination must actually trade. 000660.KS -> SKHYV pointed
             a 20-bars/month KRX line at a 1-bar/month ADR, and the cycle
             produced a full analysis off 2 price rows.

  IDENTITY   the destination must be the SAME COMPANY. Liquidity alone is not
             enough: KKOYF trades 20 bars/month and is Kesko Oyj, a Finnish
             grocer, not Kakao. Audited 2026-07-27, two live entries pointed
             at outright different companies (NAVER -> Naspers,
             MediaTek -> Murata).

Run from the container so yfinance and the app package are importable:

    sudo docker exec -w /app trading-service python scripts/audit_adr_map.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yfinance as yf  # noqa: E402

from app.utils.us_ticker_resolver import KNOWN_ADR_MAP  # noqa: E402

MIN_BARS_PER_MONTH = 10

# Tokens that carry no identifying information, so overlap on them proves
# nothing. "GROUP HOLDING LIMITED" matches half the market.
_NOISE = {
    "INC", "INC.", "CORP", "CORP.", "CO", "CO.", "LTD", "LTD.", "PLC", "AG",
    "SA", "SE", "NV", "N.V.", "GROUP", "HOLDING", "HOLDINGS", "LIMITED",
    "COMPANY", "THE", "AMERICAN", "DEPOSITARY", "SHARES", "ADR", "CORPORATION",
}


def _names(ticker: str) -> list[str]:
    """Both shortName and longName, because either alone gives false verdicts.

    HK cross-listings carry a trading-suffix shortName — 9888.HK is "BIDU-SW",
    9999.HK is "NTES", 9618.HK is "JD-SW" — while longName is the real
    "Baidu, Inc." / "NetEase, Inc." / "JD.com, Inc.". Reading shortName first
    flagged all three correct mappings as WRONG COMPANY, which would have
    deleted good entries. Compare against every name either side publishes.
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return []
    return [
        (info.get(k) or "").upper()
        for k in ("shortName", "longName")
        if info.get(k)
    ]


def _bars(ticker: str) -> int:
    try:
        df = yf.Ticker(ticker).history(period="1mo", auto_adjust=True)
        if df is None or df.empty:
            return 0
        return len(df.dropna(subset=["Close"]))
    except Exception:
        return 0


def _tokens(name: str) -> set[str]:
    raw = name.replace("-", " ").replace(".", " ").split()
    return {t for t in raw if t not in _NOISE and len(t) > 2}


def identity_matches(foreign_names: list[str], us_names: list[str]) -> bool | None:
    """True/False, or None when neither side published a usable name.

    Any name on one side matching any name on the other counts. Substring
    both ways rather than exact token equality: "SK HYNIX" vs "SK HYNIX INC.
    - AMERICAN DEPOSITARY" and "BABA-W" vs "ALIBABA GROUP HOLDING" are both
    genuine matches that exact comparison rejects.
    """
    if not foreign_names or not us_names:
        return None

    usable = False
    for fname in foreign_names:
        for uname in us_names:
            ft, ut = _tokens(fname), _tokens(uname)
            if not ft or not ut:
                continue
            usable = True
            if ft & ut:
                return True
            # One name may be an abbreviation of the other.
            for a in ft:
                for b in ut:
                    if len(a) >= 4 and (a in b or b in a):
                        return True
    return False if usable else None


def main() -> int:
    problems: list[str] = []
    unverified: list[str] = []

    print(f"auditing {len(KNOWN_ADR_MAP)} ADR mappings\n")
    print(f"{'foreign':<12} {'us':<8} {'bars':>5}  identity")
    print("-" * 62)

    for foreign, us in sorted(KNOWN_ADR_MAP.items()):
        bars = _bars(us)
        verdict = identity_matches(_names(foreign), _names(us))

        if verdict is False:
            flag = "WRONG COMPANY"
            problems.append(f"{foreign} -> {us}: different company")
        elif verdict is None:
            flag = "unverifiable"
            unverified.append(f"{foreign} -> {us}")
        else:
            flag = "ok"

        if bars < MIN_BARS_PER_MONTH:
            flag = f"{flag}, ILLIQUID"
            problems.append(f"{foreign} -> {us}: only {bars} bar(s)/month")

        print(f"{foreign:<12} {us:<8} {bars:>5}  {flag}")

    print()
    if unverified:
        # Not a failure: a delisted or thin symbol may legitimately have no
        # name on file. Surfaced so it is a decision, not an oversight.
        print(f"{len(unverified)} unverifiable (no name returned):")
        for u in unverified:
            print(f"  {u}")
        print()

    if problems:
        print(f"FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 1

    print("PASS — every mapping trades and names match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
