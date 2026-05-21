"""Permission Gateway & Request Logger — enforces role-based access control and logs all API calls."""

import logging
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from agents.registry import registry

logger = logging.getLogger("kh.web.middleware")
api_logger = logging.getLogger("kh.web.middleware.api")

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


class RequestLogger(BaseHTTPMiddleware):
    """Logs every API request with caller identity, path, status, and duration."""

    SKIP_PREFIXES = ("/static/", "/favicon.ico")
    QUIET_PATHS = ("/api/scheduler/status", "/api/agents/sessions", "/api/agents/status")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        method = request.method

        # Suppress polling endpoints (GET-only, high frequency, no useful signal)
        if method == "GET" and path in self.QUIET_PATHS:
            return await call_next(request)

        role = request.headers.get("X-Agent-Role", "").strip()
        caller_id = request.headers.get("X-Caller-ID", "").strip()
        caller = role or caller_id or "human"

        # Only log agent/mcp operations, skip human browser requests
        if caller == "human":
            return await call_next(request)

        start = time.time()
        response = await call_next(request)
        elapsed_ms = (time.time() - start) * 1000

        api_logger.info(
            "[API] %-12s | %-6s %-35s | %d | %.0fms",
            caller, method, path, response.status_code, elapsed_ms,
        )
        return response
