"""Workspace management — each project gets its own git repo directory."""

import asyncio
import logging
import os

logger = logging.getLogger("kh.core.config")

WORKSPACE_BASE = os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces"))


def validate_path_within_workspace(path: str) -> str:
    """Resolve path and verify it's within WORKSPACE_BASE. Raises ValueError on traversal."""
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(WORKSPACE_BASE)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise ValueError(f"Path traversal blocked: {path} resolves to {real_path} (outside {real_base})")
    return real_path


async def get_project_repo_path(project_id: int, git_remote_url: str = "") -> str:
    """Resolve or create the workspace directory for a project.

    - If workspace/project_{id} exists and is a git repo → return it
    - If git_remote_url provided and dir doesn't exist → clone
    - Otherwise → git init an empty repo
    """
    workspace_dir = os.path.join(WORKSPACE_BASE, f"project_{project_id}")
    workspace_dir = validate_path_within_workspace(workspace_dir)

    if os.path.isdir(os.path.join(workspace_dir, ".git")):
        if git_remote_url:
            await _pull_if_needed(workspace_dir)
        return workspace_dir

    os.makedirs(WORKSPACE_BASE, exist_ok=True)

    if git_remote_url:
        logger.info(f"Cloning {git_remote_url} → {workspace_dir}")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", git_remote_url, workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(f"Clone failed: {err}")
            raise RuntimeError(f"git clone failed: {err}")
    else:
        os.makedirs(workspace_dir, exist_ok=True)
        logger.info(f"Initializing empty repo at {workspace_dir}")
        proc = await asyncio.create_subprocess_exec(
            "git", "init", workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace_dir, "commit", "--allow-empty", "-m", "init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    return workspace_dir


async def _pull_if_needed(workspace_dir: str):
    """Pull latest from remote if the repo has a remote configured."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", workspace_dir, "remote", "get-url", "origin",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout.decode().strip():
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace_dir, "pull", "--ff-only",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
