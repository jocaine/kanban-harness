"""Coach-Dev Agent — executes coding tasks via claude CLI in isolated git worktrees."""

import asyncio
import logging
import os
import shutil
from datetime import datetime

from core.workspace import WORKSPACE_BASE, validate_path_within_workspace

logger = logging.getLogger("kh.agent.coach_dev")


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cjk
    return cjk // 2 + other // 4 + 1

DEFAULT_TIMEOUT = 600  # 10 minutes
WORKTREE_BASE = os.getenv("KH_WORKTREE_BASE", "/tmp/kh-worktrees")


class CoachDev:
    def __init__(self, repo_path: str, project_id: int = 0, on_heartbeat=None):
        self.repo_path = repo_path
        self.project_id = project_id
        self.timeout = DEFAULT_TIMEOUT
        self._on_heartbeat = on_heartbeat

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

        # KH-109: Validate cwd before launching subprocess
        if not os.path.isdir(self.repo_path):
            raise RuntimeError(f"repo_path does not exist: {self.repo_path}")
        real_repo = os.path.realpath(self.repo_path)
        real_workspace = os.path.realpath(WORKSPACE_BASE)
        if not real_repo.startswith(real_workspace + os.sep) and real_repo != real_workspace:
            raise RuntimeError(f"repo_path outside workspace: {real_repo} (workspace: {real_workspace})")

        logger.info(f"Coach-Dev 启动: [{code}] {title}")

        # Recovery: if on a stale feature branch (from crash before merge-back),
        # checkout main so _is_scaffold_mode() and _setup_worktree() work correctly
        current_branch = await self._get_current_branch()
        if current_branch and current_branch.startswith("feature/"):
            logger.info(f"Coach-Dev: 在旧分支 {current_branch}, 切换到 main")
            await self._run_git("checkout", "main")

        # Detect scaffold mode — empty repo works directly, no worktree needed
        is_scaffold = await self._is_scaffold_mode()

        try:
            if is_scaffold:
                architecture = await self._get_architecture()
                prompt = self._build_scaffold_prompt(card, architecture)
                logger.info(f"Coach-Dev 脚手架模式: [{code}] (架构文档: {len(architecture)} 字符)")
                work_path = self.repo_path
                # Branch may exist from a previous scaffold run that merged back
                branch_list = await self._run_git("branch", "--list", branch_name)
                if branch_list.strip():
                    await self._run_git("checkout", branch_name)
                else:
                    await self._run_git("checkout", "-b", branch_name)
            else:
                await self._setup_worktree(branch_name, worktree_path)
                prompt = self._build_prompt(card)
                work_path = worktree_path

            output, usage = await self._run_claude(prompt, work_path)
            has_commits = await self._check_commits(branch_name)

            if has_commits:
                commit_hash = await self._get_latest_commit(work_path)
                commit_message = await self._get_commit_message(work_path)
                summary = f"Branch: {branch_name}, commit: {commit_hash[:8]}"
                logger.info(f"Coach-Dev 完成: [{code}] {summary}")
                tokens = {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }
                return {
                    "task_done": True,
                    "signal": "to_testing",
                    "success": True, "summary": summary,
                    "branch": branch_name, "commit": commit_hash,
                    "commit_message": commit_message,
                    "is_scaffold": is_scaffold,
                    "tokens": tokens,
                }
            else:
                logger.warning(f"Coach-Dev 未产生 commit: [{code}]")
                tokens = {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }
                return {
                    "task_done": False, "signal": "", "success": False,
                    "summary": "No commits produced", "output": output,
                    "tokens": tokens,
                }

        except asyncio.TimeoutError:
            logger.error("[FAULT:AGENT] coach_dev 超时: [%s]", code)
            raise
        except Exception as e:
            logger.error("[FAULT:AGENT] coach_dev 失败: [%s]: %s", code, e)
            raise
        finally:
            if is_scaffold:
                # After scaffold, merge back to main so next trigger starts from clean main
                await self._run_git("checkout", "main")
                await self._run_git("merge", branch_name, "--no-edit")
            else:
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

    def _build_scaffold_prompt(self, card: dict, architecture: str) -> str:
        """Build prompt for project scaffold generation (empty repo + architecture doc)."""
        code = card.get("code", "")
        title = card.get("title", "")

        return (
            f"You are scaffolding a new project based on the architecture document below.\n\n"
            f"## Task: [{code}] {title}\n\n"
            f"## Architecture Document\n\n{architecture}\n\n"
            f"## Instructions\n\n"
            f"1. Create the project directory structure as described in the architecture\n"
            f"2. Create dependency files (requirements.txt, package.json, etc.) with the specified dependencies\n"
            f"3. Create a README.md with project overview and setup instructions\n"
            f"4. Create basic configuration files (.gitignore, Dockerfile if applicable, etc.)\n"
            f"5. Create placeholder entry points (main.py, index.ts, etc.) with minimal boilerplate\n"
            f"6. Commit all files with message '{code}: project scaffold'\n"
            f"7. Do NOT implement business logic — only create the skeleton structure\n"
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
            logger.warning("[FAULT:WORKSPACE] worktree 清理失败: %s", e)
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

    async def _run_claude(self, prompt: str, worktree_path: str) -> tuple[str, dict]:
        """Run claude CLI, return (result_text, usage_dict).

        usage_dict contains real token counts from stream-json result event.
        """
        import json as _json
        import time as _time
        from core.telemetry import trace_llm_call

        cmd = [
            "claude", "-p", prompt,
            "--allowedTools", "Bash,Edit,Read,Write",
            "--output-format", "stream-json",
            "--verbose",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._on_heartbeat:
            self._on_heartbeat()

        result_text = ""
        start = _time.monotonic()

        with trace_llm_call("claude-sonnet-4-6", role="coach_dev"):
            try:
                async for line in proc.stdout:
                    if self._on_heartbeat:
                        self._on_heartbeat()

                    elapsed = _time.monotonic() - start
                    if elapsed > self.timeout:
                        raise asyncio.TimeoutError()

                    line_str = line.decode(errors="replace").strip()
                    if not line_str:
                        continue

                    try:
                        event = _json.loads(line_str)
                    except (ValueError, TypeError):
                        continue

                    if event.get("type") == "result":
                        usage = event.get("usage", {})
                        self._record_usage(event)
                        result_text = event.get("result", "")

                await proc.wait()
                return result_text, usage

            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    proc.kill()
                raise
            except Exception:
                if proc.returncode is None:
                    proc.kill()
                raise

    def _record_usage(self, event: dict):
        """Record real token usage from stream-json result event."""
        from core.telemetry import get_stats

        usage = event.get("usage", {})
        stats = get_stats()
        if stats and usage:
            stats.record_call(
                model="claude-sonnet-4-6",
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                latency_ms=event.get("duration_api_ms", 0),
            )

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

    async def _get_current_branch(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self.repo_path, "rev-parse", "--abbrev-ref", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    async def _is_scaffold_mode(self) -> bool:
        """Check if repo is empty (only init commit) — triggers scaffold mode."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self.repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        count = int(stdout.decode().strip() or "0")
        return count <= 1

    async def _get_architecture(self) -> str:
        """Read project architecture document from database."""
        import aiosqlite
        from core.database import DB_PATH

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT content FROM project_architecture WHERE project_id=?",
                (self.project_id,),
            )
            row = await cursor.fetchone()
            return row["content"] if row else ""
