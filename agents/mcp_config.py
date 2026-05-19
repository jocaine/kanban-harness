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
    os.makedirs(config_dir, exist_ok=True)

    db_path = os.path.abspath(os.getenv("DB_PATH", "data/kanban.db"))

    config = {
        "mcpServers": {
            "kanban": {
                "command": "python3",
                "args": [AGENT_SERVER_PATH],
                "env": {
                    "DB_PATH": db_path,
                    "KH_AGENT_ROLE": role,
                    "KH_PROJECT_ID": str(project_id),
                },
            }
        }
    }

    config_path = os.path.join(config_dir, ".mcp.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    # Grant MCP tool permissions so CLI doesn't hang on permission prompts
    claude_dir = os.path.join(config_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    settings = {
        "permissions": {
            "allow": ["mcp__kanban__*"]
        }
    }
    settings_path = os.path.join(claude_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return config_dir
