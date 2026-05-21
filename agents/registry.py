"""Agent role registry — loads YAML configs and provides role lookup."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("kh.agent.registry")

ROLES_DIR = Path(__file__).parent / "roles"


@dataclass
class ModelConfig:
    provider: str = "anthropic"
    name: str = ""
    base_url: str = ""
    timeout: int = 600
    toolsets: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class Permissions:
    can_read: list[str] = field(default_factory=list)
    can_write: list[str] = field(default_factory=list)
    can_move: list[str] = field(default_factory=list)
    can_create: list[str] = field(default_factory=list)


@dataclass
class TriggerRule:
    event: str = ""
    condition: str = ""


@dataclass
class AgentRole:
    role: str
    display_name: str
    description: str
    icon: str
    color: str
    model: ModelConfig
    system_prompt: str
    allowed_tools: list[str]
    permissions: Permissions
    triggers: list[TriggerRule]


class AgentRegistry:
    """Singleton registry of all agent role definitions."""

    def __init__(self):
        self._roles: dict[str, AgentRole] = {}
        self._load_all()

    def _load_all(self):
        if not ROLES_DIR.exists():
            logger.warning(f"Roles directory not found: {ROLES_DIR}")
            return
        for yaml_file in sorted(ROLES_DIR.glob("*.yaml")):
            try:
                self._load_role(yaml_file)
            except Exception as e:
                logger.error(f"Failed to load role {yaml_file.name}: {e}")

    def _load_role(self, path: Path):
        with open(path) as f:
            data = yaml.safe_load(f)

        role = AgentRole(
            role=data["role"],
            display_name=data.get("display_name", data["role"]),
            description=data.get("description", ""),
            icon=data.get("icon", "🤖"),
            color=data.get("color", "#6366f1"),
            model=ModelConfig(**data.get("model", {})),
            system_prompt=data.get("system_prompt", ""),
            allowed_tools=data.get("allowed_tools", []),
            permissions=Permissions(**data.get("permissions", {})),
            triggers=[TriggerRule(**t) for t in data.get("triggers", [])],
        )
        self._roles[role.role] = role
        logger.info(f"Loaded agent role: {role.role} ({role.display_name})")

    def get(self, role_name: str) -> AgentRole | None:
        return self._roles.get(role_name)

    def all_roles(self) -> dict[str, AgentRole]:
        return dict(self._roles)

    def check_permission(self, role_name: str, action: str, resource: str) -> bool:
        """Check if role can perform action on resource.

        action: 'read' | 'write' | 'move' | 'create'
        resource: 'requirements' | 'comments' | etc.
        """
        perms = self.get_permissions(role_name)
        if not perms:
            return False
        perm_list = getattr(perms, f"can_{action}", [])
        return resource in perm_list

    def get_permissions(self, role_name: str) -> Permissions | None:
        role = self._roles.get(role_name)
        return role.permissions if role else None

    def check_move(self, role_name: str, from_status: str, to_status: str) -> bool:
        """Check if role can move a card between specific statuses."""
        perms = self.get_permissions(role_name)
        if not perms:
            return False
        transition = f"{from_status}->{to_status}"
        return transition in perms.can_move

    def roles_for_trigger(self, event: str, context: dict) -> list[str]:
        """Find all roles that should be triggered by an event."""
        matched = []
        for role_name, role in self._roles.items():
            for trigger in role.triggers:
                if trigger.event == event:
                    if self._evaluate_condition(trigger.condition, context):
                        matched.append(role_name)
                        break
        return matched

    def _evaluate_condition(self, condition: str, context: dict) -> bool:
        """Simple condition evaluator. Empty cn = always true."""
        if not condition:
            return True
        try:
            return bool(eval(condition, {"__builtins__": {}}, context))
        except Exception:
            return False


registry = AgentRegistry()
