import os
import sys
import pytest

# Add project root to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "debug"))

from self_improvement_minimax_test import load_prism_url

pytestmark = pytest.mark.asyncio

def test_load_prism_url():
    """Verify that load_prism_url returns a valid URL configuration."""
    url = load_prism_url()
    assert isinstance(url, str)
    assert url.startswith("http://")

@pytest.mark.skip(reason="Integration test makes live network requests to local synology NAS container.")
async def test_live_self_improving_harness():
    """Live integration test for the self-improving harness using MiniMax 2.7 M2.7."""
    from self_improvement_minimax_test import run_harness_via_http
    await run_harness_via_http()
