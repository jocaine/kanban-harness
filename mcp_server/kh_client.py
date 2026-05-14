"""HTTP client for Kanban Harness REST API."""

import os
import logging
import time
import httpx
from typing import Any

logger = logging.getLogger("kh.mcp.client")

KH_BASE_URL = os.getenv("KH_BASE_URL", "http://localhost:8000")


class KHClient:
    def __init__(self, base_url: str = "", caller_id: str = "mcp"):
        self.base_url = (base_url or KH_BASE_URL).rstrip("/")
        self.caller_id = caller_id

    def _headers(self) -> dict:
        return {"X-Caller-ID": self.caller_id} if self.caller_id else {}

    async def _get(self, path: str) -> Any:
        t0 = time.time()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}{path}", headers=self._headers(), timeout=30)
            elapsed = (time.time() - t0) * 1000
            logger.info("GET %s → %d (%.0fms)", path, resp.status_code, elapsed)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> Any:
        t0 = time.time()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=30)
            elapsed = (time.time() - t0) * 1000
            logger.info("POST %s → %d (%.0fms) body=%s", path, resp.status_code, elapsed,
                       str(json)[:100] if json else "null")
            resp.raise_for_status()
            return resp.json()

    async def _put(self, path: str, json: dict | None = None) -> Any:
        t0 = time.time()
        async with httpx.AsyncClient() as client:
            resp = await client.put(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=30)
            elapsed = (time.time() - t0) * 1000
            logger.info("PUT %s → %d (%.0fms) body=%s", path, resp.status_code, elapsed,
                       str(json)[:100] if json else "null")
            resp.raise_for_status()
            return resp.json()

    async def list_projects(self) -> list[dict]:
        return await self._get("/api/projects")

    async def list_versions(self, project_id: int) -> list[dict]:
        return await self._get(f"/api/projects/{project_id}/versions")

    async def list_requirements(self, version_id: int) -> list[dict]:
        return await self._get(f"/api/versions/{version_id}/requirements")

    async def create_requirement(self, version_id: int, title: str, description: str = "",
                                  priority: str = "P2", status: str = "pending") -> dict:
        return await self._post("/api/requirements", json={
            "version_id": version_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": status,
        })

    async def update_requirement(self, req_id: int, **kwargs) -> dict:
        return await self._put(f"/api/requirements/{req_id}", json=kwargs)

    async def move_requirement(self, req_id: int, status: str, position: int = 0) -> dict:
        return await self._put(f"/api/requirements/{req_id}/move", json={
            "status": status,
            "position": position,
        })

    async def add_comment(self, req_id: int, content: str, author: str = "system") -> dict:
        return await self._post(f"/api/requirements/{req_id}/comments", json={
            "author": author,
            "content": content,
        })

    async def get_scheduler_status(self) -> dict:
        return await self._get("/api/scheduler/status")

    async def get_agents_status(self) -> dict:
        return await self._get("/api/agents/status")

    async def list_agent_sessions(self) -> list[dict]:
        return await self._get("/api/agents/sessions")
