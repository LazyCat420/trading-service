"""
Regression tests for the equation sandbox executor.

The 2026-07-15 cycle audit found every tournament equation eliminated with
"ImportError: __import__ not found": LLM-authored equations open with
`import numpy as np` / `import pandas as pd`, and the sandbox builtins had
no __import__ at all. These tests pin the restricted importer behavior.
"""
import pandas as pd
import pytest

from app.cognition.debate import equation_library


@pytest.fixture
def price_df(monkeypatch):
    df = pd.DataFrame(
        {
            "close": [100.0, 101.0, 99.5, 102.0, 103.5],
            "volume": [1000, 1100, 900, 1200, 1300],
        }
    )
    from app.trading import quant_edge_verifier

    monkeypatch.setattr(quant_edge_verifier, "load_historical_data", lambda ticker: df)
    return df


def test_import_numpy_and_pandas_allowed(price_df):
    code = (
        "import numpy as np\n"
        "import pandas as pd\n"
        "result = float(np.mean(df['close']))\n"
    )
    out = equation_library.execute_equation(code, "TEST")
    assert out.get("status") == "ok", out
    assert out["result"] == pytest.approx(price_df["close"].mean())


def test_from_import_math_allowed(price_df):
    code = "from math import sqrt\nresult = sqrt(16)\n"
    out = equation_library.execute_equation(code, "TEST")
    assert out.get("status") == "ok", out
    assert out["result"] == 4.0


def test_disallowed_import_blocked(price_df):
    code = "import os\nresult = 1\n"
    out = equation_library.execute_equation(code, "TEST")
    assert "error" in out
    assert "not allowed" in out["error"]


def test_no_import_still_works(price_df):
    code = "result = float(df['close'].iloc[-1])\n"
    out = equation_library.execute_equation(code, "TEST")
    assert out.get("status") == "ok", out
    assert out["result"] == 103.5


def test_common_builtins_available(price_df):
    code = (
        "vals = [float(x) for x in df['close'] if hasattr(x, 'real')]\n"
        "result = pow(max(vals), 1) if any(v > 0 for v in vals) and all(v > 0 for v in vals) else 0\n"
    )
    out = equation_library.execute_equation(code, "TEST")
    assert out.get("status") == "ok", out
    assert out["result"] == 103.5
