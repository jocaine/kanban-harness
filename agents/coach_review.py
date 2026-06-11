"""Coach-Review Agent — QA with actual workspace access to read code and run tests."""

import asyncio
import json
import logging
import os
import shutil
import time

import aiosqlite

from agents.registry import registry
from agents.mcp_config import ensure_agent_mcp_config_at
from core.workspace import WORKSPACE_BASE
from core.database import DB_PATH

logger = logging.getLogger("kh.agent.coach_review")

DEFAULT_TIMEOUT = 600
WORKTREE_BASE = os.getenv("KH_WORKTREE_BASE", "/tmp/kh-worktrees")


class CoachReview:
    def __init__(self, repo_path: str, project_id: int = 0, on_heartbeat=None):
        self.repo_path = repo_path
        self.project_id = project_id
        self.role_config = registry.get("coach_review")
        self.timeout = (
            self.role_config.model.timeout if self.role_config else DEFAULT_TIMEOUT
        )
        self._on_heartbeat = on_heartbeat

    def _worktree_dir(self, code: str) -> str:
        project_dir = os.path.join(WORKTREE_BASE, f"project_{self.project_id}")
        os.makedirs(project_dir, exist_ok=True)
        return os.path.join(project_dir, f"review-{code.lower()}")

    async def execute(self, card: dict, branch_name: str = "") -> dict:
        """Review code on the feature branch with actual filesystem access."""
        code = card.get("code", "unknown")
        title = card.get("title", "")
        req_id = card.get("id", 0)

        if not branch_name:
            branch_name = f"feature/{code.lower()}"

        logger.info("Coach-Review 启动: [%s] %s (branch=%s)", code, title, branch_name)

        branch_exists = await self._branch_exists(branch_name)
        if not branch_exists:
            logger.warning("[%s] 分支 %s 不存在, 回退到 main", code, branch_name)
            branch_name = "main"

        worktree_path = self._worktree_dir(code)

        try:
            await self._setup_worktree(branch_name, worktree_path)
            ensure_agent_mcp_config_at(
                worktree_path, "coach_review", self.project_id, req_id
            )
            prompt = await self._build_prompt(card, branch_name)
            result_text, usage = await self._run_claude(prompt, worktree_path, req_id)

            tokens = {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }

            return {
                "task_done": True,
                "success": True,
                "summary": f"Coach-Review completed [{code}]",
                "tokens": tokens,
            }

        except asyncio.TimeoutError:
            logger.error("[FAULT:AGENT] coach_review 超时: [%s]", code)
            raise
        except Exception as e:
            logger.error("[FAULT:AGENT] coach_review 失败: [%s]: %s", code, e)
            raise
        finally:
            await self._cleanup_worktree(worktree_path)
            self._cleanup_mcp_config(worktree_path)

    # ==================== Prompt building ====================

    async def _build_prompt(self, card: dict, branch_name: str) -> str:
        code = card.get("code", "")
        title = card.get("title", "")
        description = card.get("description", "")
        priority = card.get("priority", "P2")
        req_id = card.get("id", 0)

        sections = []

        sections.append(
            f"## 审查任务\n\n"
            f"- 需求ID: {req_id}\n"
            f"- 编号: {code}\n"
            f"- 标题: {title}\n"
            f"- 优先级: {priority}\n"
            f"- 分支: {branch_name}\n\n"
            f"### 需求描述\n\n{description or '(无描述)'}"
        )

        comments = await self._get_comments(req_id)
        if comments:
            sections.append("### 已有评论\n")
            for c in comments:
                text = c["content"]
                if c.get("detail"):
                    text += "\n_(有完整版，可通过 read_comment_detail 工具查看)_"
                sections.append(f"**{c['author']}:** {text}\n")

        sections.append(
            "\n## 指令\n\n"
            "你现在在代码工作目录中，可以直接查看文件和运行测试。\n\n"
            "**必须执行的步骤：**\n"
            "1. `git log main..HEAD --oneline` 查看提交历史\n"
            "2. `git diff --stat main..HEAD` 查看变更文件列表\n"
            "3. 用 Read 工具查看关键源文件\n"
            "4. 运行测试（如果有 package.json 用 npm test，有 pytest.ini 用 pytest）\n"
            "5. 逐项对照验收标准验证\n\n"
            "**审查完成后调用决策工具：**\n"
            f"- 通过: decide(requirement_id={req_id}, comment='审查结论', target='done')\n"
            f"- 打回: decide(requirement_id={req_id}, comment='问题说明', target='dev')\n"
            f"- 请CEO裁决: ask_ceo(requirement_id={req_id}, comment='背景', question='问题')\n\n"
            "**禁止：** 未读代码未跑测试就判断通过或打回。"
        )

        return "\n\n".join(sections)

    async def _get_comments(self, requirement_id: int) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, author, content, detail FROM comments "
                "WHERE requirement_id=? ORDER BY created_at",
                (requirement_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    # ==================== Claude CLI execution ====================

    async def _run_claude(self, prompt: str, work_path: str, requirement_id: int = 0) -> tuple[str, dict]:
        role_cfg = self.role_config
        mcp_tools = [f"mcp__kanban__{t}" for t in role_cfg.allowed_tools]
        workspace_tools = getattr(role_cfg, "workspace_tools", []) or []
        all_tools = ",".join(workspace_tools + mcp_tools)

        cmd = [
            "claude",
            "-p", prompt,
      "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", all_tools,
            "--model", role_cfg.model.name or "claude-sonnet-4-6",
            "--append-system-prompt", role_cfg.system_prompt,
        ]

        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
        if "API_KEY" in os.environ and "ANTHROPIC_AUTH_TOKEN" not in env:
            env["ANTHROPIC_AUTH_TOKEN"] = os.environ["API_KEY"]
        if "API_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in env:
            base = os.environ["API_BASE_URL"].rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            env["ANTHROPIC_BASE_URL"] = base

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=work_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        if self._on_heartbeat:
            self._on_heartbeat()

        result_text = ""
        usage = {}
        start = time.monotonic()
        model_name = role_cfg.model.name or "claude-sonnet-4-6"

        try:
            async for line in proc.stdout:
                if self._on_heartbeat:
                    self._on_heartbeat()
                elapsed = time.monotonic() - start
                if elapsed > self.timeout:
                    raise asyncio.TimeoutError()
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "result":
                    usage = event.get("usage", {})
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

    # ==================== Git/worktree helpers ====================

    async def _branch_exists(self, branch_name: str) -> bool:
        result = await self._run_git("branch", "--list", branch_name)
        return len(result.strip()) > 0

    async def _setup_worktree(self, branch_name: str, worktree_path: str):
        os.makedirs(WORKTREE_BASE, exist_ok=True)
        if os.path.exists(worktree_path):
            await self._run_git("worktree", "remove", "--force", worktree_path)
        await self._run_git("worktree", "add", worktree_path, branch_name)

    async def _cleanup_worktree(self, worktree_path: str):
        try:
            if os.path.exists(worktree_path):
                await self._run_git("worktree", "remove", "--force", worktree_path)
        except Exception as e:
            logger.warning("[FAULT:WORKSPACE] review worktree cleanup: %s", e)
            if os.path.exists(worktree_path):
                shutil.rmtree(worktree_path, ignore_errors=True)
            try:
                await self._run_git("worktree", "prune")
            except Exception:
                pass

    def _cleanup_mcp_config(self, work_path: str):
        for name in (".mcp.json", ".claude"):
            target = os.path.join(work_path, name)
            if os.path.isfile(target):
                os.remove(target)
            elif os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)

    async def _run_git(self, *args) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(errors="replace").strip()
