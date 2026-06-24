"""
Conftest for unit tests that need the tool registry populated.

Mocks the psycopg module so tool imports can succeed without a real database,
then forces the tool registration by importing app.tools.
"""

import sys
import pytest
from unittest.mock import MagicMock
@pytest.fixture(autouse=True, scope="session")
def disable_prism_routing_globally():
    """Globally disable Prism routing in unit tests to prevent network/endpoint dependencies."""
    from app.config import settings
    # Pydantic settings are mutable by default, but using object.__setattr__ to bypass any frozen checks safely.
    object.__setattr__(settings, "PRISM_ENABLED", False)
    object.__setattr__(settings, "PRISM_AGENT_ROUTING", False)

@pytest.fixture(autouse=True, scope="session")
def mock_new_agents_globally():
    """Globally mock the new specialist agents that make external LLM calls."""
    from unittest.mock import AsyncMock, patch, MagicMock
    import sys
    import app
    import app.agents
    
    # Dynamically inject portfolio_allocator_agent if it doesn't exist to prevent import/attribute errors in tests
    if "app.agents.portfolio_allocator_agent" not in sys.modules:
        mock_pa_module = MagicMock()
        mock_pa_module.run_portfolio_allocator = AsyncMock(return_value={})
        setattr(app.agents, "portfolio_allocator_agent", mock_pa_module)
        sys.modules["app.agents.portfolio_allocator_agent"] = mock_pa_module

    # Dynamically inject post_mortem_auditor_agent if it doesn't exist to prevent attribute errors in tests
    if "app.agents.post_mortem_auditor_agent" not in sys.modules:
        mock_pm_module = MagicMock()
        mock_pm_module.run_post_mortem = AsyncMock(return_value=None)
        setattr(app.agents, "post_mortem_auditor_agent", mock_pm_module)
        sys.modules["app.agents.post_mortem_auditor_agent"] = mock_pm_module

    # Dynamically inject legacy cycle phases if they don't exist
    if not hasattr(app, "cycle"):
        mock_cycle = MagicMock()
        setattr(app, "cycle", mock_cycle)
        sys.modules["app.cycle"] = mock_cycle
        
        mock_phases = MagicMock()
        setattr(mock_cycle, "phases", mock_phases)
        sys.modules["app.cycle.phases"] = mock_phases
        
        mock_ph6 = MagicMock()
        mock_ph6.run_post_mortem = AsyncMock(return_value=None)
        setattr(mock_phases, "phase6_post", mock_ph6)
        sys.modules["app.cycle.phases.phase6_post"] = mock_ph6
        
        mock_tp = MagicMock()
        mock_tp.run_portfolio_allocator = AsyncMock(return_value={})
        setattr(mock_cycle, "trading_phase", mock_tp)
        sys.modules["app.cycle.trading_phase"] = mock_tp
        
    with patch("app.agents.portfolio_allocator_agent.run_portfolio_allocator", new_callable=AsyncMock) as mock_pa, \
         patch("app.agents.post_mortem_auditor_agent.run_post_mortem", new_callable=AsyncMock) as mock_pm, \
         patch("app.cycle.phases.phase6_post.run_post_mortem", mock_pm):
        
        # Conditionally patch trading_phase.run_portfolio_allocator if it exists
        import app.cycle.trading_phase
        if hasattr(app.cycle.trading_phase, "run_portfolio_allocator"):
            with patch("app.cycle.trading_phase.run_portfolio_allocator", mock_pa):
                mock_pa.return_value = {}
                mock_pm.return_value = None
                yield mock_pa, mock_pm
        else:
            mock_pa.return_value = {}
            mock_pm.return_value = None
            yield mock_pa, mock_pm

@pytest.fixture(autouse=True, scope="session")
def mock_psycopg():
    """Mock psycopg so tool modules can import without a real DB driver."""
    # Only mock if psycopg isn't already importable
    if "psycopg" not in sys.modules:
        mock_module = MagicMock()
        mock_pool = MagicMock()
        sys.modules["psycopg"] = mock_module
        sys.modules["psycopg.rows"] = MagicMock()
        sys.modules["psycopg_pool"] = mock_pool

    # Now force-import all tool modules so decorators run and register tools
    try:
        import app.tools  # noqa: F401 — triggers all tool registrations
    except Exception:
        pass  # Some tools may fail deeper imports; that's OK for unit tests

    yield
