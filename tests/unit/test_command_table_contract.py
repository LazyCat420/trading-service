"""
Contract Test: Enforce V3 Command Table Usage (v3_system_commands).

Asserts that:
1. cycle_main.py drain_schedule_refreshes queries v3_system_commands.
2. active cycle start/stop execution commands in cycle_main.py poll v3_system_commands.
"""

import os
import re

SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_drain_schedule_refreshes_uses_v3_system_commands():
    """Verify that cycle_main.py drain_schedule_refreshes queries v3_system_commands."""
    cycle_main_path = os.path.join(SERVICE_ROOT, "cycle_main.py")
    with open(cycle_main_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract drain_schedule_refreshes function
    match = re.search(r"def drain_schedule_refreshes\(\):.*?(?=\ndef |\Z)", content, re.DOTALL)
    assert match is not None, "drain_schedule_refreshes function not found in cycle_main.py"
    fn_body = match.group(0)

    assert "v3_system_commands" in fn_body, "drain_schedule_refreshes must query v3_system_commands"
    assert "FROM system_commands" not in fn_body, "drain_schedule_refreshes must NOT query legacy system_commands"
    assert "UPDATE system_commands" not in fn_body, "drain_schedule_refreshes must NOT update legacy system_commands"


def test_cycle_main_poll_system_commands_uses_v3_system_commands():
    """Verify that cycle_main.py poll_system_commands polls v3_system_commands."""
    cycle_main_path = os.path.join(SERVICE_ROOT, "cycle_main.py")
    with open(cycle_main_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract poll_system_commands function
    match = re.search(r"async def poll_system_commands\(.*?\):.*?(?=\nasync def |\ndef |\Z)", content, re.DOTALL)
    assert match is not None, "poll_system_commands function not found in cycle_main.py"
    fn_body = match.group(0)

    assert "v3_system_commands" in fn_body, "poll_system_commands must query v3_system_commands"
    assert "FROM system_commands" not in fn_body, "poll_system_commands must NOT query legacy system_commands"
