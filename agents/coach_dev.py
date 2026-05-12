"""Coach-Dev Agent — executes coding tasks via claude CLI on feature branches."""

import asyncio
import logging
import os
import json
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600  # 10 minutes
REPO_PATH = os.getenv("KH_REPO_PATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CoachDev:
    def __init__(self, repo_path: str = ""):
        self.repo_path = repo_path or REPO_PATH
        self.timeout = DEFAULT_TIMEOUT

    async def execute(self, card: dict) -> dict:
        code = card.get("code", "unknown")
        title = card.get("title", "")
        description = card.get("description", "")
        branch_name = f"feature/{code.lower()}"

        logger.info(f"Coach-Dev starting: [{code}] {title}")

        try:
            await self._create_branch(branch_name)
            prompt = self._build_prompt(card)
            output = await self._run_claude(prompt)
            has_commits = await self._check_commits(branch_name)

            if has_commits:
                commit_hash = await self._get_latest_commit()
                summary = f"Branch: {branch_name}, commit: {commit_hash[:8]}"
                logger.info(f"Coach-Dev completed: [{code}] {summary}")
                return {"success": True, "summary": summary, "branch": branch_name, "commit": commit_hash}
            else:
                logger.warning(f"Coach-Dev produced no commits for [{code}]")
                return {"success": False, "summary": "No commits produced", "output": output}

        except asyncio.TimeoutError:
            logger.error(f"Coach-Dev timed out for [{code}]")
            raise
        except Exception as e:
            logger.error(f"Coach-Dev failed for [{code}]: {e}")
            raise

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

    async def _create_branch(self, branch_name: str):
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", "-b", branch_name,
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Branch might already exist, try switching to it
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", branch_name,
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

    async def _run_claude(self, prompt: str) -> str:
        cmd = [
            "claude", "-p", prompt,
            "--allowedTools", "Bash,Edit,Read,Write",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.repo_path,
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
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "main.." + branch_name, "--oneline",
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return len(stdout.decode().strip()) > 0

    async def _get_latest_commit(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
