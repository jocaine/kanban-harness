"""Coach-Dev Agent — implements code via claude CLI with MCP tools + workspace access."""

import asyncio
import json
import logging
import os
import shutil
import time

import aiosqlite

from agents.registry import registry
from agents.mcp_config import ensure_agent_mcp_config_at
from core.workspace import WORKSPACE_BASE, validate_path_within_workspace
from core.database import DB_PATH

logger = logging.getLogger("kh.agent.coach_dev")

DEFAULT_TIMEOUT = 600
WORKTREE_BASE = os.getenv("KH_WORKTREE_BASE", "/tmp/kh-worktrees")


class CoachDev:
    def __init__(self, repo_path: str, project_id: int = 0, on_heartbeat=None):
        self.repo_path = repo_path
        self.project_id = project_id
        self.role_config = registry.get("coach_dev")
        self.timeout = self.role_config.model.timeout if self.role_config else DEFAULT_TIMEOUT
        self._on_heartbeat = on_heartbeat

    def _worktree_dir(self, code: str) -> str:
        project_dir = os.path.join(WORKTREE_BASE, f"project_{self.project_id}")
        os.makedirs(project_dir, exist_ok=True)
        return os.path.join(project_dir, code.lower())

    async def execute(self, card: dict) -> dict:
        """Execute dev task: setup workspace, call claude CLI with MCP + workspace tools."""
        code = card.get("code", "unknown")
        title = card.get("title", "")
        branch_name = f"feature/{code.lower()}"
        worktree_path = self._worktree_dir(code)

        if not os.path.isdir(self.repo_path):
            raise RuntimeError(f"repo_path does not exist: {self.repo_path}")
        real_repo = os.path.realpath(self.repo_path)
        real_workspace = os.path.realpath(WORKSPACE_BASE)
        if not real_repo.startswith(real_workspace + os.sep) and real_repo != real_workspace:
            raise RuntimeError(f"repo_path outside workspace: {real_repo}")

        logger.info("Coach-Dev 启动: [%s] %s", code, title)

        current_branch = await self._get_current_branch()
        if current_branch and current_branch.startswith("feature/"):
            await self._run_git("checkout", "main")

        is_scaffold = await self._is_scaffold_mode()

        try:
            if is_scaffold:
                work_path = self.repo_path
                branch_list = await self._run_git("branch", "--list", branch_name)
                if branch_list.strip():
                    await self._run_git("checkout", branch_name)
                else:
                    await self._run_git("checkout", "-b", branch_name)
            else:
                await self._setup_worktree(branch_name, worktree_path)
                work_path = worktree_path

            # Generate MCP config in work directory (scoped to this card)
            ensure_agent_mcp_config_at(work_path, "coach_dev", self.project_id, card.get("id", 0))

            # Build prompt with full context
            prompt = await self._build_prompt(card, is_scaffold)

            # Run claude CLI with MCP tools + workspace tools
            output, usage, ops_log = await self._run_claude(prompt, work_path, card.get("id", 0))

            tokens = {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }

            # Verify actual work output (anti-hallucination)
            verification = await self._verify_work_output(work_path, branch_name, ops_log)

            if verification["valid"]:
                commit_hash = await self._get_latest_commit(work_path)
                commit_msg = await self._get_commit_message(work_path)
                summary = f"Branch: {branch_name}, commit: {commit_hash[:8]}"
                logger.info("Coach-Dev 完成: [%s] %s (files=%d, bash=%d, edits=%d)",
                            code, summary, verification["files_changed"],
                            verification["bash_ops"], verification["file_ops"])
            else:
                commit_hash = ""
                commit_msg = ""
                reason = verification["reason"]
                summary = f"验证失败: {reason}"
                logger.warning("[VERIFY-FAIL] %s: %s", code, reason)

            return {
                "task_done": True,
                "success": verification["valid"],
                "summary": summary,
                "branch": branch_name,
                "commit": commit_hash,
                "commit_message": commit_msg,
                "is_scaffold": is_scaffold,
                "tokens": tokens,
                "verification": verification,
            }

        except asyncio.TimeoutError:
            logger.error("[FAULT:AGENT] coach_dev 超时: [%s]", code)
            raise
        except Exception as e:
            logger.error("[FAULT:AGENT] coach_dev 失败: [%s]: %s", code, e)
            raise
        finally:
            if is_scaffold:
                await self._run_git("checkout", "main")
                await self._run_git("merge", branch_name, "--no-edit")
            else:
                await self._cleanup_worktree(worktree_path)
            self._cleanup_mcp_config(work_path)

    # ==================== Prompt building ====================

    async def _build_prompt(self, card: dict, is_scaffold: bool) -> str:
        """Build prompt with full project context, comments, and task description."""
        code = card.get("code", "")
        title = card.get("title", "")
        description = card.get("description", "")
        priority = card.get("priority", "P2")
        req_id = card.get("id", 0)

        sections = []

        # Project context
        context = await self._get_project_context()
        if context:
            sections.append(context)

        # Card info
        sections.append(
            f"## 当前任务\n\n"
            f"- 需求ID: {req_id}\n"
            f"- 编号: {code}\n"
            f"- 标题: {title}\n"
            f"- 优先级: {priority}\n\n"
            f"### 需求描述\n\n{description or '(无描述)'}"
        )

        # Comments history
        comments = await self._get_comments(req_id)
        if comments:
            sections.append("### 已有评论\n")
            for c in comments:
                text = c["content"]
                if c.get("detail"):
                    text += "\n_(有完整版，可通过 read_comment_detail 工具查看)_"
                sections.append(f"**{c['author']}:** {text}\n")

        # Architecture doc (for scaffold mode)
        if is_scaffold:
            arch = await self._get_architecture()
            if arch:
                sections.append(f"### 架构文档\n\n{arch}")
            sections.append(
                "\n## 指令\n\n"
                "这是脚手架模式（空项目），请按架构文档创建项目骨架：\n"
                "1. 创建目录结构和依赖文件\n"
                "2. 创建配置文件和入口文件\n"
                "3. git commit（message 以卡片编号开头）\n"
                "4. 完成后调用 decide(requirement_id=%d, comment='项目骨架已创建', target='testing')" % req_id
            )
        else:
            sections.append(
                "\n## 指令\n\n"
                "请在当前工作目录中实现上述需求。完成后：\n"
                "- 调用 decide(requirement_id=%d, comment='实现说明', target='testing')\n"
                "- 如果需求有问题无法实现：decide(requirement_id=%d, comment='退回原因', target='organizing')\n"
                "- 如果需要 CEO 决策：ask_ceo(requirement_id=%d, comment='背景', question='问题')"
                % (req_id, req_id, req_id)
            )

        return "\n\n".join(sections)

    async def _get_project_context(self) -> str:
        """Read project context from wiki."""
        from core.wiki import get_wiki_for_prompt
        return get_wiki_for_prompt(self.project_id)

    async def _get_comments(self, requirement_id: int) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, author, content, detail FROM comments "
                "WHERE requirement_id=? ORDER BY created_at",
                (requirement_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _get_architecture(self) -> str:
        from core.wiki import read_wiki_page
        return read_wiki_page(self.project_id, "arch/overview")

    # ==================== Claude CLI execution ====================

    async def _run_claude(self, prompt: str, work_path: str, requirement_id: int = 0) -> tuple[str, dict]:
        """Run claude CLI with MCP tools + workspace tools, return (result_text, usage_dict)."""
        role_cfg = self.role_config
        mcp_tools = [f"mcp__kanban__{t}" for t in role_cfg.allowed_tools]
        workspace_tools = role_cfg.workspace_tools
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
        ops_log = []  # Collect workspace operations for card_log
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

                self._extract_ops(event, ops_log)

            await proc.wait()

            # Flush workspace operation logs
            if requirement_id and ops_log:
                await self._flush_ops_log(requirement_id, ops_log)

            return result_text, usage, ops_log

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

    # ==================== Workspace operation logging ====================

    def _extract_ops(self, event: dict, ops_log: list):
        """Extract tool_use events from stream-json and append to ops_log."""
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                entry = {"tool": tool_name}
                if tool_name == "Bash":
                    entry["detail"] = tool_input.get("command", "")[:200]
                elif tool_name in ("Edit", "Write"):
                    entry["detail"] = tool_input.get("file_path", "")
                elif tool_name == "Read":
                    entry["detail"] = tool_input.get("file_path", "")
                elif tool_name.startswith("mcp__kanban__"):
                    entry["tool"] = tool_name.replace("mcp__kanban__", "")
                    entry["detail"] = json.dumps(tool_input, ensure_ascii=False)[:150]
                else:
                    entry["detail"] = str(tool_input)[:100]
                ops_log.append(entry)

    async def _flush_ops_log(self, requirement_id: int, ops_log: list):
        """Write collected workspace operations to card_log."""
        from core.card_logger import card_log

        bash_ops = [op for op in ops_log if op["tool"] == "Bash"]
        file_ops = [op for op in ops_log if op["tool"] in ("Edit", "Write", "Read")]
        mcp_ops = [op for op in ops_log if op["tool"] not in ("Bash", "Edit", "Write", "Read")]

        if bash_ops:
            cmds = [op["detail"] for op in bash_ops]
            summary = "; ".join(cmds[:10])
            if len(cmds) > 10:
                summary += f" ...+{len(cmds) - 10} more"
            await card_log(requirement_id, f"[workspace] 执行命令 ({len(bash_ops)}): {summary}", source="coach_dev")

        if file_ops:
            paths = list(set(op["detail"] for op in file_ops))
            summary = ", ".join(paths[:8])
            if len(paths) > 8:
                summary += f" ...+{len(paths) - 8} more"
            await card_log(requirement_id, f"[workspace] 文件操作 ({len(file_ops)}): {summary}", source="coach_dev")

        if mcp_ops:
            calls = [f"{op['tool']}({op.get('detail', '')})" for op in mcp_ops]
            await card_log(requirement_id, f"[decision] {'; '.join(calls)}", source="coach_dev")

    # ==================== Work output verification ====================

    async def _verify_work_output(self, work_path: str, branch_name: str, ops_log: list) -> dict:
        """Verify agent actually produced real code — not hallucinated descriptions.

        Checks:
        1. Real commits exist on branch (ahead of main)
        2. Diff is non-empty (excludes .mcp.json/.claude config artifacts)
        3. Agent actually used workspace tools (Bash/Edit/Write) per ops_log
        """
        has_commits = await self._check_commits(branch_name)
        if not has_commits:
            # Check ops_log: if agent never used workspace tools, it hallucinated
            ops_validation = self._validate_ops_log(ops_log)
            if not ops_validation["used_workspace_tools"]:
                return {"valid": False, "reason": "no_commits_no_workspace_tools",
                        "files_changed": 0, "bash_ops": 0, "file_ops": 0}
            return {"valid": False, "reason": "no_commits",
                    "files_changed": 0,
                    "bash_ops": ops_validation["bash_ops_count"],
                    "file_ops": ops_validation["file_ops_count"]}

        # Verify diff is non-trivial (not just config artifacts)
        diff_stat = await self._run_git_in(work_path, "diff", "--stat", f"main..HEAD")
        ignored_patterns = (".mcp.json", ".claude/")
        real_changes = [
            line for line in diff_stat.splitlines()
            if line.strip() and not any(p in line for p in ignored_patterns)
            and "|" in line  # only file stat lines, not the summary
        ]

        if not real_changes:
            return {"valid": False, "reason": "only_config_artifacts",
                    "files_changed": 0, "bash_ops": 0, "file_ops": 0}

        # Validate commit object exists
        commit_hash = await self._get_latest_commit(work_path)
        verify_result = await self._run_git_in(work_path, "cat-file", "-t", commit_hash)
        if "commit" not in verify_result:
            return {"valid": False, "reason": "commit_object_invalid",
                    "files_changed": 0, "bash_ops": 0, "file_ops": 0}

        # Cross-check ops_log
        ops_validation = self._validate_ops_log(ops_log)

        return {
            "valid": True,
            "reason": "ok",
            "files_changed": len(real_changes),
            "bash_ops": ops_validation["bash_ops_count"],
            "file_ops": ops_validation["file_ops_count"],
            "has_git_operations": ops_validation["has_git_operations"],
        }

    def _validate_ops_log(self, ops_log: list) -> dict:
        """Check if agent actually used workspace tools (vs pure text hallucination)."""
        bash_ops = [op for op in ops_log if op["tool"] == "Bash"]
        file_ops = [op for op in ops_log if op["tool"] in ("Edit", "Write")]

        has_file_writes = len(file_ops) > 0
        has_git_commit = any(
            "git commit" in op.get("detail", "") or "git add" in op.get("detail", "")
            for op in bash_ops
        )

        return {
            "used_workspace_tools": has_file_writes or len(bash_ops) > 0,
            "has_git_operations": has_git_commit,
            "file_ops_count": len(file_ops),
            "bash_ops_count": len(bash_ops),
        }

    async def _run_git_in(self, cwd: str, *args) -> str:
        """Run git command in a specific directory (not self.repo_path)."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(errors="replace").strip()

    # ==================== Git helpers ====================

    async def _setup_worktree(self, branch_name: str, worktree_path: str):
        os.makedirs(WORKTREE_BASE, exist_ok=True)
        if os.path.exists(worktree_path):
            await self._run_git("worktree", "remove", "--force", worktree_path)
        proc = await self._run_git("branch", "--list", branch_name)
        if not proc.strip():
            await self._run_git("branch", branch_name, "main")
        await self._run_git("worktree", "add", worktree_path, branch_name)

    async def _cleanup_worktree(self, worktree_path: str):
        try:
            if os.path.exists(worktree_path):
                await self._run_git("worktree", "remove", "--force", worktree_path)
        except Exception as e:
            logger.warning("[FAULT:WORKSPACE] worktree 清理失败: %s", e)
            if os.path.exists(worktree_path):
                shutil.rmtree(worktree_path, ignore_errors=True)
            try:
                await self._run_git("worktree", "prune")
            except Exception:
                pass

    def _cleanup_mcp_config(self, work_path: str):
        """Remove generated .mcp.json and .claude/ from worktree after execution."""
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
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if err and "already exists" not in err:
                logger.debug("git %s: %s", " ".join(args), err)
        return stdout.decode(errors="replace").strip()

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
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self.repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        count = int(stdout.decode().strip() or "0")
        return count <= 1
