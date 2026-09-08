"""A percent label must not present stored fractions as percentage points."""
from app.tools.finance_tools import _format_fundamentals


def test_market_data_scales_fractional_fundamentals_once():
    row = ('2026-09-08', 2800891392, 28.57, 16.29, .7, 49.13, .0301,
           .14, 3425912064, -.017, 70.6, 1.86, 253.05, 106.3, .5306)
    text = _format_fundamentals([row])
    for correct in ('ShortFloat%: 53.06', 'NetMargin%: 3.01', 'ROE%: 14',
                    'RevenueGrowth%: -1.7', 'D/E: 70.6', 'PE: 28.57'):
        assert correct in text
    assert 'ShortFloat%: 0.53' not in text


def test_zero_is_zero_and_missing_percent_is_not_invented():
    row = ('2026-09-08', None, None, None, None, None, 0, None, None, None,
           None, None, None, None, None)
    text = _format_fundamentals([row])
    assert 'NetMargin%: 0' in text
    assert 'ShortFloat%: 0' not in text
