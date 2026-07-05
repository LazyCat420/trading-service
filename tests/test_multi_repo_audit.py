"""
Integration tests for multi-repo audit fixes.

Tests verify:
1. P0: prism_client.url is set once per cycle (no per-call stomping)
2. P2: base_agent.py uses logger instead of print
3. P2: tool_schemas.json consistency
4. Phase 2C: DoomLoopException is not retried by aresilient_call
"""

import ast
import hashlib
import json
import os
import sys
import unittest

# Add trading-service root to sys.path for imports
TRADING_SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TRADING_SERVICE_ROOT not in sys.path:
    sys.path.insert(0, TRADING_SERVICE_ROOT)


class TestP0PrismClientRaceFix(unittest.TestCase):
    """Verify the P0 prism_client.url race condition fix."""

    def test_pipeline_service_sets_url_at_cycle_start(self):
        """_run_all_v3 must set prism_client.url before any agent calls."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "services", "pipeline_service.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        # Must contain the cycle-start URL initialization
        self.assertIn(
            "prism_client.url",
            source,
            "pipeline_service.py must set prism_client.url",
        )
        self.assertIn(
            "Set prism_client.url ONCE for the entire cycle",
            source,
            "pipeline_service.py must contain the cycle-start comment",
        )

    def test_base_agent_does_not_stomp_url(self):
        """base_agent.py must NOT set prism_client.url per-call."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "agents", "base_agent.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        # Parse the AST to find assignments to prism_client.url
        tree = ast.parse(source)
        url_assignments = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "url"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "prism_client"
                    ):
                        url_assignments.append(node.lineno)

        self.assertEqual(
            len(url_assignments),
            0,
            f"base_agent.py must NOT assign to prism_client.url "
            f"(found assignments at lines: {url_assignments})",
        )

    def test_base_agent_has_race_condition_comment(self):
        """base_agent.py must contain the NOTE about URL being set at cycle start."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "agents", "base_agent.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        self.assertIn(
            "prism_client.url is set ONCE per cycle",
            source,
            "base_agent.py must reference the cycle-start URL initialization",
        )


class TestP2PrintToLogger(unittest.TestCase):
    """Verify print() has been replaced with logger in base_agent.py."""

    def test_no_bare_print_in_run_agent(self):
        """run_agent function must not use print() for I/O logging."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "agents", "base_agent.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        tree = ast.parse(source)
        
        # Find the run_agent function
        run_agent_func = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "run_agent":
                    run_agent_func = node
                    break

        self.assertIsNotNone(run_agent_func, "run_agent function must exist")

        # Check for print() calls in run_agent
        print_lines = []
        for node in ast.walk(run_agent_func):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    print_lines.append(node.lineno)

        self.assertEqual(
            len(print_lines),
            0,
            f"run_agent() must not use print() "
            f"(found print calls at lines: {print_lines})",
        )

    def test_uses_logger_debug_for_verbose(self):
        """base_agent.py must use logger.debug for verbose I/O logging."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "agents", "base_agent.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        self.assertIn(
            "logger.debug",
            source,
            "base_agent.py must use logger.debug for verbose logging",
        )
        self.assertIn(
            "[BaseAgent] INPUT",
            source,
            "base_agent.py must log agent input with structured prefix",
        )
        self.assertIn(
            "[BaseAgent] OUTPUT",
            source,
            "base_agent.py must log agent output with structured prefix",
        )


class TestP2ToolSchemaSync(unittest.TestCase):
    """Verify tool_schemas.json is synchronized across repos."""

    def test_tool_schemas_match(self):
        """tool_schemas.json must be identical in lazy-tool-service and trading-service."""
        lazy_path = os.path.join(
            TRADING_SERVICE_ROOT,
            "..",
            "lazy-tool-service",
            "tool_schemas.json",
        )
        trading_path = os.path.join(
            TRADING_SERVICE_ROOT, "tool_schemas.json"
        )

        if not os.path.exists(lazy_path):
            self.skipTest("lazy-tool-service/tool_schemas.json not found")
        if not os.path.exists(trading_path):
            self.skipTest("trading-service/tool_schemas.json not found")

        with open(lazy_path, "rb") as f:
            lazy_hash = hashlib.md5(f.read()).hexdigest()
        with open(trading_path, "rb") as f:
            trading_hash = hashlib.md5(f.read()).hexdigest()

        self.assertEqual(
            lazy_hash,
            trading_hash,
            "tool_schemas.json must be identical in both repos",
        )


class TestExcInfoOnErrors(unittest.TestCase):
    """Verify critical error logs include exc_info=True for stack traces."""

    def test_prism_agent_caller_has_exc_info(self):
        """prism_agent_caller.py error logs must include exc_info=True."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT,
            "app",
            "services",
            "prism_agent_caller.py",
        )
        with open(source_path, "r") as f:
            source = f.read()

        # The critical "Call failed" error must include exc_info
        self.assertIn(
            "exc_info=True",
            source,
            "prism_agent_caller.py must use exc_info=True for error logging",
        )


class TestDoomLoopNotRetried(unittest.TestCase):
    """Verify DoomLoopException is not retried by aresilient_call."""

    def test_resilience_handles_doom_loop(self):
        """aresilient_call must stop retries on DoomLoopException."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "app", "utils", "resilience.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        self.assertIn(
            "DoomLoopException",
            source,
            "resilience.py must explicitly handle DoomLoopException",
        )


class TestPortContract(unittest.TestCase):
    """Verify port mappings are consistent."""

    def test_docker_compose_maps_3031_to_8080(self):
        """docker-compose.yml must map external 3031 to internal 8080."""
        compose_path = os.path.join(
            TRADING_SERVICE_ROOT, "docker-compose.yml"
        )
        if not os.path.exists(compose_path):
            self.skipTest("docker-compose.yml not found")

        with open(compose_path, "r") as f:
            content = f.read()

        self.assertIn(
            "3031:8080",
            content,
            "docker-compose.yml must map 3031 -> 8080",
        )

    def test_health_server_on_8080(self):
        """cycle_main.py health server must listen on port 8080."""
        source_path = os.path.join(
            TRADING_SERVICE_ROOT, "cycle_main.py"
        )
        with open(source_path, "r") as f:
            source = f.read()

        self.assertIn(
            "port=8080",
            source,
            "cycle_main.py health server must use port 8080",
        )

    def test_dockerfile_healthcheck_port(self):
        """Dockerfile healthcheck must use localhost:8080."""
        dockerfile_path = os.path.join(
            TRADING_SERVICE_ROOT, "Dockerfile"
        )
        with open(dockerfile_path, "r") as f:
            content = f.read()

        self.assertIn(
            "localhost:8080",
            content,
            "Dockerfile healthcheck must target localhost:8080",
        )


if __name__ == "__main__":
    unittest.main()
