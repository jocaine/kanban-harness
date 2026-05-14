"""Coach-Dev Agent — executes coding tasks via claude CLI in isolated git worktrees."""

import asyncio
import logging
import os
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600  # 10 minutes
WORKTREE_BASE = os.getenv("KH_WORKTREE_BASE", "/tmp/kh-worktrees")


class CoachDev:
    def __init__(self, repo_path: str, project_id: int = 0):
        self.repo_path = repo_path
        self.project_id = project_id
        self.timeout = DEFAULT_TIMEOUT

    def _worktree_dir(self, code: str) -> str:
        """Per-project isolated worktree path: /tmp/kh-worktrees/project_{id}/{code}"""
        project_dir = os.path.join(WORKTREE_BASE, f"project_{self.project_id}")
        os.makedirs(project_dir, exist_ok=True)
        return os.path.join(project_dir, code.lower())

    async def execute(self, card: dict) -> dict:
        code = card.get("code", "unknown")
        title = card.get("title", "")
        description = card.get("description", "")
        branch_name = f"feature/{code.lower()}"
        worktree_path = self._worktree_dir(code)

        logger.info(f"Coach-Dev starting: [{code}] {title}")

        try:
            await self._setup_worktree(branch_name, worktree_path)
            prompt = self._build_prompt(card)
            output = await self._run_claude(prompt, worktree_path)
            has_commits = await self._check_commits(branch_name)

            if has_commits:
                commit_hash = await self._get_latest_commit(worktree_path)
                commit_message = await self._get_commit_message(worktree_path)
                summary = f"Branch: {branch_name}, commit: {commit_hash[:8]}"
                logger.info(f"Coach-Dev completed: [{code}] {summary}")
                return {
                    "success": True, "summary": summary,
                    "branch": branch_name, "commit": commit_hash,
                    "commit_message": commit_message,
                }
            else:
                logger.warning(f"Coach-Dev produced no commits for [{code}]")
                return {"success": False, "summary": "No commits produced", "output": output}

        except asyncio.TimeoutError:
            logger.error(f"Coach-Dev timed out for [{code}]")
            raise
        except Exception as e:
            logger.error(f"Coach-Dev failed for [{code}]: {e}")
            raise
        finally:
            await self._cleanup_worktree(worktree_path)

    def _build_prompt(self, card: dict) -> str:
        code = card.get("code", "")
        title = card.get("title", "")
        description = card.get("description", "")
        priority = card.get("priority", "P2")

        return (
            f"You are implementing requirement [{code}] for the Kanban Harness project.\n\n"
            f"## Task\n\n"
            f"**Title:** {title}\n"
            f"**Priority:** {priority}\n\n"
            f"## Description\n\n{description}\n\n"
            f"## Instructions\n\n"
            f"1. Read the existing codebase to understand the project structure\n"
            f"2. Implement the requirement described above\n"
            f"3. Commit your changes with a message starting with '{code}: '\n"
            f"4. Keep changes focused and minimal\n"
            f"5. Do not modify unrelated files\n"
        )

    async def _setup_worktree(self, branch_name: str, worktree_path: str):
        os.makedirs(WORKTREE_BASE, exist_ok=True)

        # Remove stale worktree if exists
        if os.path.exists(worktree_path):
            await self._run_git("worktree", "remove", "--force", worktree_path)

        # Create branch from main if it doesn't exist
        proc = await self._run_git("branch", "--list", branch_name)
        if not proc.strip():
            await self._run_git("branch", branch_name, "main")

        # Create worktree
        await self._run_git("worktree", "add", worktree_path, branch_name)

    async def _cleanup_worktree(self, worktree_path: str):
        try:
            if os.path.exists(worktree_path):
                await self._run_git("worktree", "remove", "--force", worktree_path)
        except Exception as e:
            logger.warning(f"Worktree cleanup failed: {e}")
            # Fallback: just delete the directory
            if os.path.exists(worktree_path):
                shutil.rmtree(worktree_path, ignore_errors=True)
            try:
                await self._run_git("worktree", "prune")
            except Exception:
                pass

    async def _run_git(self, *args) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if err and "already exists" not in err:
                logger.debug(f"git {' '.join(args)}: {err}")
        return stdout.decode(errors="replace").strip()

    async def _run_claude(self, prompt: str, worktree_path: str) -> str:
        cmd = [
            "claude", "-p", prompt,
            "--allowedTools", "Bash,Edit,Read,Write",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
            return stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            raise

    async def _check_commits(self, branch_name: str) -> bool:
        output = await self._run_git("log", f"main..{branch_name}", "--oneline")
        return len(output.strip()) > 0

    async def _get_latest_commit(self, worktree_path: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    async def _get_commit_message(self, worktree_path: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "-1", "--format=%s",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
