"""MCP config generation for agent roles.

Generates per-role .mcp.json files so each Claude CLI subprocess
connects only to the MCP servers its role is allowed to use.
"""

import json
import os
from pathlib import Path

CONFIG_BASE = "/tmp/kh-agent-configs"
AGENT_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_server",
    "agent_server.py",
)


def ensure_agent_mcp_config(role: str, project_id: int) -> str:
    """Generate /tmp/kh-agent-configs/{role}/.mcp.json and return the directory path.

    The Claude CLI subprocess uses this directory as cwd to discover the MCP config.
    Also generates .claude/settings.json to grant MCP tool permissions non-interactively.
    """
    config_dir = os.path.join(CONFIG_BASE, role)
    return ensure_agent_mcp_config_at(config_dir, role, project_id)


def ensure_agent_mcp_config_at(target_dir: str, role: str, project_id: int, requirement_id: int = 0) -> str:
    """Generate .mcp.json + .claude/settings.json at a specific directory.

    Used by coach_dev to place MCP config directly in the project worktree,
    so claude CLI can use both MCP tools and workspace tools with the correct cwd.

    Returns the target directory path.
    """
    os.makedirs(target_dir, exist_ok=True)

    db_path = os.path.abspath(os.getenv("DB_PATH", "data/kanban.db"))

    env_vars = {
        "DB_PATH": db_path,
        "KH_AGENT_ROLE": role,
        "KH_PROJECT_ID": str(project_id),
    }
    if requirement_id:
        env_vars["KH_REQUIREMENT_ID"] = str(requirement_id)

    config = {
        "mcpServers": {
            "kanban": {
                "command": "python3",
                "args": [AGENT_SERVER_PATH],
                "env": env_vars,
            }
        }
    }

    config_path = os.path.join(target_dir, ".mcp.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    claude_dir = os.path.join(target_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    settings = {
        "permissions": {
            "allow": ["mcp__kanban__*", "Bash", "Edit", "Read", "Write"]
        }
    }
    settings_path = os.path.join(claude_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return target_dir
