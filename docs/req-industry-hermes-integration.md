# 需求：Industry 角色通过 Hermes 子进程实现带工具的评审

## 背景

当前 `comment_agent.py:_call_hermes()` 只是把 prompt 丢给 `hermes -z`，存在两个问题：
1. hermes 子进程没有 kanban MCP 配置，无法读卡片/写评论
2. prompt 里没有注入项目上下文（advisor_skill、product_memory、看板状态）

industry.yaml 里声明了 `allowed_tools: [web_search, web_fetch, kanban_get_requirement, kanban_list_comments, kanban_add_comment]`，但实际执行时这些 kanban 工具不可用。

## 方案

不引入新依赖，不改架构。在现有 subprocess 模式基础上，确保 hermes 子进程能同时使用 web 工具和 kanban MCP 工具。

### 实现步骤

#### 1. 为 hermes 子进程注入 kanban MCP 配置

修改 `comment_agent.py:_call_hermes()`，在调用前确保 hermes 的 MCP 配置指向本地 KH server。

复用 `hermes_chat.py:ensure_hermes_config()` 的逻辑——它已经在做这件事（把 `mcp_servers.kanban` 写入 `~/.hermes/config.yaml`，指向 `mcp_server/server.py` stdio 模式）。

**改动点：** `comment_agent.py:_call_hermes()` 调用前，调用 `ensure_hermes_config()`（幂等操作，多次调用无副作用）。

```python
# comment_agent.py:_call_hermes() 开头加：
from web.hermes_chat import ensure_hermes_config
await ensure_hermes_config()
```

这样 hermes 子进程启动时就能通过 MCP stdio 连到本地 kanban server，拥有 `kanban_*` 系列工具。

#### 2. 注入项目上下文到 prompt

当前 `_build_prompt()` 只拼了 system_prompt + 卡片信息 + 已有评论。缺少：
- 项目的 advisor_skill（产品顾问知识）
- product_memory（决策历史）
- 当前看板全局状态

**改动点：** `CommentAgent.__init__()` 接收 `project_id` 参数，`_build_prompt()` 查库注入上下文。

```python
class CommentAgent:
    def __init__(self, role_name: str, project_id: int = 0):
        self.role_config = registry.get(role_name)
        self.project_id = project_id
```

`_build_prompt()` 增加上下文段：

```python
async def _build_prompt(self, card: dict, comments: list[dict]) -> str:
    system = self.role_config.system_prompt
    
    # 注入项目上下文（仅 hermes provider 需要，因为它有工具能力）
    context_section = ""
    if self.project_id and self.role_config.model.provider == "hermes":
        context_section = await self._get_project_context()
    
    card_context = ...  # 现有逻辑不变
    
    return f"{system}\n\n{context_section}\n\n{card_context}"
```

新增方法：

```python
async def _get_project_context(self) -> str:
    """从数据库读取项目上下文，注入 prompt。"""
    import aiosqlite
    from core.database import DB_PATH
    
    sections = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_fact= aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, prefix, advisor_skill, product_memory FROM projects WHERE id=?",
            (self.project_id,),
        )
        proj = await cursor.fetchone()
        if proj:
            sections.append(f"## 项目：{proj['name']} ({proj['prefix']})")
            if proj["advisor_skill"]:
                sections.append(f"\n## 产品顾问知识\n\n{proj['advisor_skill'][:1500]}")
            if proj["product_memory"]:
                sections.append(f"\n## 产品记忆\n\n{proj['product_memory'][:1000]}")
        
        # 看板状态摘要
        cursor = await db.execute(
            "SELECT r.code, r.title, r.status, r.priority "
            "FROM requirements r JOIN versions v ON r.version_id=v.id "
            "WHERE v.project_id=? AND r.archived=0 "
            "ORDER BY r.priority LIMIT 15",
            (self.project_id,),
        )
        reqs = await cursor.fetchall()
        if reqs:
            sections.append("\n## 当前看板\n")
            for r in reqs:
                sections.append(f"- [{r['code']}] {r['title']} ({r['status']}, {r['priority']})")
    
    return "\n".join(sections)
```

#### 3. 调用方传入 project_id

**改动点：** `scheduler/engine.py:_trigger_comment_agent()` 和 `_run_comment_agent()` 传递 project_id。

```python
# engine.py:_run_comment_agent() 改为：
async def _run_comment_agent(self, session_id: int, role_name: str, card: dict, project_id: int):
    agent = CommAgent(role_name, project_id=project_id)
    ...

# engine.py:_trigger_comment_agent() 改为：
asyncio.create_task(self._run_comment_agent(session_id, role_name, card, event["project_id"]))
```

#### 4. hermes 子进程环境变量

**改动点：** `_call_hermes()` 传递 API 环境变量（复用 `hermes_chat.py:_build_hermes_env()` 逻辑）。

```python
async def _call_hermes(self, prompt: str, cfg) -> str:
    from web.hermes_chat import ensure_hermes_config, _build_hermes_env
    await ensure_hermes_config()
    
    cmd = ["hermes", "-z", prompt, "--yolo"]
    if cfg.toolsets:
        cmd.extend(["-t", ",".join(cfg.toolsets)])
    if cfg.name:
        cmd.extend(["--model", cfg.name])
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_build_hermes_env(),  # 传递 API key/base_url
    )
    ...
```

#### 5. industry.yaml toolsets 更新

当前 `toolsets: [web]`。hermes 的 `-t` 参数控制启用哪些 toolset。kanban 工具通过 MCP 注册，不需要在 `-t` 里声明（MCP 工具始终可用）。所以 `toolsets: [web]` 保持不变即可。

但需要确认：hermes `-t web` 是否会禁用 MCP 工具？如果是，则改为 `-t web,mcp` 或不传 `-t`（启用全部）。

**验证方法：**
```bash
hes est" -t web --yolo --dry-run 2>&1 | grep -i mcp
```

如果 MCP 工具被 `-t` 过滤掉了，改 industry.yaml：
```yaml
toolsets:
  - web
  - mcp
```

## 涉及文件

| 文件 | 改动 |
|------|------|
| `agents/comment_agent.py` | 主要改动：注入 config、context、env |
| `scheduler/engine.py` | 传递 project_id 到 CommentAgent |
| `agents/roles/industry.yaml` | 可能需要加 `mcp` toolset |
| `web/hermes_chat.py` | 无改动，复用现有函数 |

## 验收标准

1. `requirement_created` 事件触发 industry 角色时，hermes 子进程能成功调用 `web_search` 做竞品搜索
2. hermes 子进程能通过 MCP 读取卡片详情（`kanban_get_requirement`）
3. hermes 子进程产出的评审意见被写入 comments 表
4. prompt 中包含 advisor_skill 和 product_memory 上下文
5. 超时 120s 内完成（industry.yaml 已配置）

## 不做的事

- 不改 MCP server 代码
- 不改 hermes_chat.py 的 PM 流程
- 不引入新的 agent 框架或 SDK
- 不改 registry/role 数据结构
