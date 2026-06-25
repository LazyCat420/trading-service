import sys
from app.tools.registry import registry
from app.v3.agents.junior_analyst import TOOL_WHITELIST as ja_tools
from app.v3.agents.fundamental_analyst import TOOL_WHITELIST as fa_tools
from app.v3.agents.quant_analyst import TOOL_WHITELIST as qa_tools

print("Registered tools:", list(registry.tools.keys()))
print("JA whitelisted tools:", ja_tools)
print("Are JA tools registered?", [t in registry.tools for t in ja_tools])
print("FA whitelisted tools:", fa_tools)
print("Are FA tools registered?", [t in registry.tools for t in fa_tools])
print("QA whitelisted tools:", qa_tools)
print("Are QA tools registered?", [t in registry.tools for t in qa_tools])
