"""Permission Gateway — enforces role-based access control for agent API calls."""

import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from agents.registry import registry

logger = logging.getLogger(__name__)

# Map (method, path_keyword) → (action, resource)
ROUTE_RULES: list[tuple[str, str, str, str]] = [
    ("POST", "/requirements", "create", "requirements"),
    ("POST", "/comments", "create", "comments"),
    ("PUT", "/move", "move", "requirements"),
    ("PUT", "/requirements", "write", "requirements"),
    ("PUT", "/architecture", "write", "architecture"),
    ("PUT", "/product-memory", "write", "product_memory"),
    ("DELETE", "/requirements", "write", "requirements"),
    ("DELETE", "/comments", "write", "comments"),
]


class PermissionGateway(BaseHTTPMiddleware):
    """Middleware that enforces agent role permissions.

    - Requests WITHOUT X-Agent-Role header: pass through (human user)
    - Requests WITH X-Agent-Role header: checked against registry
    """

    async def dispatch(self, request: Request, call_next):
        role = request.headers.get("X-Agent-Role", "").strip()

        if not role:
            return await call_next(request)

        agent_role = registry.get(role)
        if not agent_role:
            return JSONResponse(
                status_code=403,
                content={"detail": f"Unknown agent role: '{role}'"},
            )

        path = request.url.path
        method = request.method

        action, resource = self._resolve_permission(method, path)

        if action and resource and action != "move":
            if not registry.check_permission(role, action, resource):
                logger.warning(f"Permission denied: {role} cannot {action} {resource}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": f"Role '{role}' cannot {action} {resource}",
                        "role": role,
                        "allowed": getattr(
                            registry.get_permissions(role), f"can_{action}", []
                        ),
                    },
                )

        request.state.agent_role = role
        return await call_next(request)

    def _resolve_permission(self, method: str, path: str) -> tuple[str, str]:
        for rule_method, keyword, action, resource in ROUTE_RULES:
            if method == rule_method and keyword in path:
                return action, resource
        return "", ""
