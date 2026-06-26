import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))

from app.utils.text_utils import parse_json_response, parse_malformed_text_response

def test_pm_parser():
    pm_output = """Here's a quick breakdown of your watchlist:

## ? Watchlist Summary

**Gainers (6):** AAPL, MSFT, SWBI, AMD, DKS, HCA, HIG, CPS, PAM, CE
**Decliners (8):** NVDA, AMP, TSLA, BCE, MRVL, UBS, IP, BCE

### ? Notable Movers

| Ticker | Action | Detail |
|--------|--------|--------|
| **MSFT** | ? +5.71% | Highest gainer, rel vol 4.36x ? strong conviction |
| **AAPL** | ? +3.14% | Rel vol 4.58x ? heavy volume behind the move |
| **CPS** | ? +3.78% | Up on a pullback (-2.66% from SMA-20) |
| **MRVL** | ? -5.15% | Biggest decliner, though rel vol is light (0.86x) |
| **NVDA** | ? -1.64% | RSI 38.7, 7.31% below SMA-20 ? getting oversold |

### ?? Overbought (RSI > 70)
- **DKS** ? RSI 76.2, +6.56% above SMA-20
- **PNC** ? RSI 71.2, +5.84% above SMA-20
- **IP** ? RSI 70.0, +9.67% above SMA-20 (most extended)

### ? Oversold / Pullback Candidates (RSI < 40)
- **MSFT** ? RSI 31.3, +5.71% today ? bounce play?
- **AAPL** ? RSI 33.5, +3.14% today ? bounce play?
- **BCE** ? RSI 32.5, -3.78% from SMA-20
- **NVDA** ? RSI 38.7, -7.31% from SMA-20

### ? Key Observation
**AAPL & MSFT** are the most interesting right now ? both had massive relative v
olume spikes (4.58x and 4.36x) on up days while sitting at RSI levels of 31?33.
That's a classic oversold bounce with institutional participation. Worth watchin
g for continuation.

Want me to pull any deeper analysis on specific tickers?"""

    print("--- Testing parse_json_response ---")
    parsed = parse_json_response(pm_output)
    print(f"Result: {parsed}")
    
    assert isinstance(parsed, dict), "Should return a dict"
    assert "selected_tickers" in parsed, "Should extract selected_tickers"
    assert "AAPL" in parsed["selected_tickers"], "Should have AAPL"
    assert "MSFT" in parsed["selected_tickers"], "Should have MSFT"
    print("Test passed!")

if __name__ == "__main__":
    test_pm_parser()
