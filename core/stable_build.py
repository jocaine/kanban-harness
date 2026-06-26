"""Stable build management — maintain a 'stable' snapshot per project for run/export.

Each project workspace has:
- dev branch: coach-dev works here
- main branch: only receives merges when cards are done
- tag 'stable': moves forward on each card completion (for run)
- version tags 'v0.x': immutable, created on version release (for export)

The stable worktree lives at workspace/_stable/ and always checks out the 'stable' tag.
"""

import asyncio
import logging
import os

logger = logging.getLogger("kh.core.stable_build")

WORKSPACE_BASE = os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces"))


def _workspace(project_id: int) -> str:
    return os.path.join(WORKSPACE_BASE, f"project_{project_id}")


def _stable_path(project_id: int) -> str:
    return os.path.join(_workspace(project_id), "_stable")


async def _git(workspace: str, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", workspace, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()


async def ensure_dev_branch(project_id: int):
    """Ensure the workspace has a dev branch and is checked out to it."""
    ws = _workspace(project_id)
    if not os.path.isdir(os.path.join(ws, ".git")):
        return

    rc, current, _ = await _git(ws, "branch", "--show-current")
    if current == "dev":
        return

    rc, branches, _ = await _git(ws, "branch", "--list", "dev")
    if "dev" in branches:
        await _git(ws, "checkout", "dev")
    else:
        await _git(ws, "checkout", "-b", "dev")
    logger.info("[STABLE] project_%d: switched to dev branch", project_id)


async def promote_to_stable(project_id: int, card_code: str = "") -> dict:
    """Merge current dev HEAD into main and update the stable tag.

    Called when a card moves to done.
    Returns {"ok": True, "commit": "abc123"} or {"ok": False, "error": "..."}.
    """
    ws = _workspace(project_id)
    if not os.path.isdir(os.path.join(ws, ".git")):
        return {"ok": False, "error": "not a git repo"}

    rc, _, _ = await _git(ws, "rev-parse", "dev")
    if rc != 0:
        return {"ok": False, "error": "dev branch not found"}

    rc, _, _ = await _git(ws, "checkout", "main")
    if rc != 0:
        rc, _, _ = await _git(ws, "checkout", "-b", "main")
        if rc != 0:
            return {"ok": False, "error": "cannot checkout main"}

    msg = f"stable: {card_code}" if card_code else "stable: card done"
    rc, _, err = await _git(ws, "merge", "dev", "--no-edit", "-m", msg)
    if rc != 0:
        await _git(ws, "merge", "--abort")
        await _git(ws, "checkout", "dev")
        return {"ok": False, "error": f"merge conflict: {err}"}

    rc, commit, _ = await _git(ws, "rev-parse", "HEAD")
    await _git(ws, "tag", "-f", "stable")

    await _git(ws, "checkout", "dev")
    await _update_stable_worktree(project_id)

    logger.info("[STABLE] project_%d: promoted %s -> stable (%s)", project_id, card_code, commit[:7])
    return {"ok": True, "commit": commit}


async def tag_version_release(project_id: int, version_name: str) -> dict:
    """Create an immutable version tag on current stable. Called on version release."""
    ws = _workspace(project_id)
    tag_name = version_name if version_name.startswith("v") else f"v{version_name}"

    rc, _, err = await _git(ws, "tag", tag_name, "stable")
    if rc != 0:
        return {"ok": False, "error": f"tag failed: {err}"}

    logger.info("[STABLE] project_%d: tagged %s", project_id, tag_name)
    return {"ok": True, "tag": tag_name}


async def _update_stable_worktree(project_id: int):
    """Create or update the _stable/ worktree to point at the stable tag."""
    ws = _workspace(project_id)
    stable_dir = _stable_path(project_id)

    if os.path.isdir(stable_dir):
        await _git(stable_dir, "checkout", "stable")
        return

    rc, _, err = await _git(ws, "worktree", "add", "--detach", stable_dir, "stable")
    if rc != 0:
        logger.error("[STABLE] worktree add failed: %s", err)


async def get_stable_dir(project_id: int) -> str | None:
    """Return the stable worktree path if it exists and has a stable tag."""
    stable_dir = _stable_path(project_id)
    if os.path.isdir(stable_dir):
        return stable_dir

    ws = _workspace(project_id)
    rc, _, _ = await _git(ws, "rev-parse", "stable")
    if rc != 0:
        return None

    await _update_stable_worktree(project_id)
    return stable_dir if os.path.isdir(stable_dir) else None


async def get_version_dir(project_id: int, version_tag: str) -> str | None:
    """Checkout a specific version tag into a temp worktree for export."""
    ws = _workspace(project_id)
    tag_name = version_tag if version_tag.startswith("v") else f"v{version_tag}"

    rc, _, _ = await _git(ws, "rev-parse", tag_name)
    if rc != 0:
        return None

    export_dir = os.path.join(ws, f"_export_{tag_name}")
    if os.path.isdir(export_dir):
        return export_dir

    rc, _, _ = await _git(ws, "worktree", "add", "--detach", export_dir, tag_name)
    if rc != 0:
        return None
    return export_dir
