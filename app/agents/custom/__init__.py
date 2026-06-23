# app/agents/custom/__init__.py
import importlib
import pkgutil
import logging

logger = logging.getLogger(__name__)

# Cache of loaded custom agents: { "agent_name": { "identity": "...", "enabled_tools": [...] } }
_custom_agents = {}

def load_custom_agents():
    """Dynamically discover and load all custom agent definitions in this package."""
    global _custom_agents
    if _custom_agents:
        return _custom_agents

    import app.agents.custom as custom_pkg
    
    for _, module_name, _ in pkgutil.iter_modules(custom_pkg.__path__):
        try:
            module = importlib.import_module(f"app.agents.custom.{module_name}")
            if hasattr(module, "AGENT_NAME") and hasattr(module, "IDENTITY"):
                agent_name = module.AGENT_NAME
                identity = module.IDENTITY
                enabled_tools = getattr(module, "ENABLED_TOOLS", [])
                
                _custom_agents[agent_name] = {
                    "identity": identity,
                    "enabled_tools": enabled_tools
                }
                logger.debug(f"Loaded custom agent '{agent_name}' with {len(enabled_tools)} tools.")
        except Exception as e:
            logger.error(f"Failed to load custom agent module {module_name}: {e}")

    # Load archived custom agents as fallback
    import os
    archive_dir = os.path.join(os.path.dirname(__file__), "archive")
    if os.path.isdir(archive_dir):
        for _, module_name, _ in pkgutil.iter_modules([archive_dir]):
            try:
                module = importlib.import_module(f"app.agents.custom.archive.{module_name}")
                if hasattr(module, "AGENT_NAME") and hasattr(module, "IDENTITY"):
                    agent_name = module.AGENT_NAME
                    identity = module.IDENTITY
                    enabled_tools = getattr(module, "ENABLED_TOOLS", [])
                    
                    if agent_name not in _custom_agents:
                        _custom_agents[agent_name] = {
                            "identity": identity,
                            "enabled_tools": enabled_tools
                        }
                        logger.debug(f"Loaded archived custom agent '{agent_name}' (fallback).")
            except Exception as e:
                logger.error(f"Failed to load archived custom agent module {module_name}: {e}")

    return _custom_agents

def get_custom_agent(agent_name: str) -> dict | None:
    """Get the identity and tools for a specific custom agent."""
    agents = load_custom_agents()
    return agents.get(agent_name)
