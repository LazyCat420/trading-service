import ast
import os
from pathlib import Path

def test_no_low_max_tokens_in_codebase():
    base_dir = Path(__file__).parent.parent.parent / "app"
    failures = []
    
    for py_file in base_dir.rglob("*.py"):
        try:
            with open(py_file, "r") as f:
                content = f.read()
            tree = ast.parse(content)
            
            for node in ast.walk(tree):
                # Check keyword arguments (e.g. call(max_tokens=256))
                if isinstance(node, ast.keyword) and node.arg == "max_tokens":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                        val = node.value.value
                        if val < 8192:
                            failures.append(f"{py_file.name}:{node.lineno} -> max_tokens={val}")
                            
        except Exception as e:
            print(f"Failed to parse {py_file}: {e}")

    if failures:
        print("\nLow max_tokens limits found in the codebase:")
        for fail in failures:
            print(f" - {fail}")
        
    assert not failures, f"Found {len(failures)} places with max_tokens < 8192!"
