import os
import re

def test_ssh_config_exists():
    """Verify that the SSH config file exists at ~/.ssh/config."""
    ssh_config_path = os.path.expanduser("~/.ssh/config")
    assert os.path.isfile(ssh_config_path), f"SSH config file not found at {ssh_config_path}"

def test_ssh_sockets_directory_exists():
    """Verify that the SSH sockets directory exists at ~/.ssh/sockets."""
    sockets_dir = os.path.expanduser("~/.ssh/sockets")
    assert os.path.isdir(sockets_dir), f"SSH sockets directory not found at {sockets_dir}"

def test_ssh_config_multiplexing_settings():
    """Verify that ~/.ssh/config has ControlMaster auto, ControlPath, and ControlPersist for the NAS (10.0.0.16)."""
    ssh_config_path = os.path.expanduser("~/.ssh/config")
    with open(ssh_config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Search for Host blocks
    host_blocks = re.findall(r"(?:^|\n)\s*Host\s+([^\n]+)(.*?)(?=\n\s*Host\s+|\Z)", content, re.DOTALL)
    
    # We want to make sure there is a block matching 10.0.0.16, nas, or synology
    matched_block = None
    for host_pattern, block_body in host_blocks:
        patterns = [p.strip() for p in host_pattern.split()]
        if any(pat in ["10.0.0.16", "nas", "synology"] for pat in patterns):
            matched_block = block_body
            break

    assert matched_block is not None, "No Host block matches '10.0.0.16', 'nas', or 'synology' in ~/.ssh/config"

    # Now verify the directives in the matched block body
    directives = {}
    for line in matched_block.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            directives[parts[0].lower()] = parts[1]

    # Verify key multiplexing directives
    assert directives.get("controlmaster") == "auto", "ControlMaster is not set to auto"
    assert "controlpath" in directives, "ControlPath directive is missing"
    assert "controlpersist" in directives, "ControlPersist directive is missing"
    assert directives.get("hostname") == "10.0.0.16", "HostName is not set to 10.0.0.16"
