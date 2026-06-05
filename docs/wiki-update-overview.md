---
type: arch
updated: 2026-06-02
tags: [overview]
---

# Kanban Harness 架构概要

> 本文档为 wiki 的快速入口摘要。完整文档见 LLM Wiki 各页面。

## 五层架构

| 层 | 职责 | 关键文件 |
|----|------|----------|
| L5 前端 | Dashboard + SSE + REST API | web/ |
| L4 角色 | 声明式 YAML 定义角色行为 | agents/roles/*.yaml |
| L3 编排 | 调度、session 管理、事件路由、决策验证 | scheduler/engine.py, scheduler/handlers.py, core/session_manager.py |
| L2 基础设施 | SQLite、MCP server、buffer、配置热加载 | core/database.py, mcp_server/, core/workflow_config.py |
| L1 运行时 | Docker、进程管理 | Dockerfile, main.py |

## 核心工作流

### 开发需求
```
CEO 建卡 → organizing → PM 网关（拆解/分发）→ dev → Coach-Dev 写码 → testing → Coach-Review 验收 → done
```

### 调研需求
```
CEO 建卡 → organizing → PM 分发到 research → Industry 调研 → [转给PM] → organizing → PM 审计 → done（写入产品记忆）
```

### 异常升级（CEO Decision）
```
任何角色执行失败 / 卡片停滞超时 → ceo_decision 字段标记 → Dashboard 王权面板展示 → CEO 决策（批准/重试/回复/归档）
```

## 关键设计决策（v0.8 当前）

### 原子决策工具（v0.8）

Agent 不再分开调用 `add_comment` +irement`，而是使用**原子决策工具**：

| 工具 | 作用 | 自动副作用 |
|------|------|-----------|
| `pm_approve` | PM 批准卡片 | 写评论 + 移卡到 dev（dev类型）或 done（research类型） |
| `pm_send_to_research` | PM 退回调研 | 写评论 + 移卡到 research |
| `pm_ask_ceo` | PM 问 CEO | 写评论 + 设置 ceo_decision |
| `industry_complete` | 行业顾问完成 | 写评论（含 detail）+ 移卡到 organizing |
| `industry_ask_ceo` | 行业顾问问 CEO | 写评论 + 设置 ceo_decision |

**设计原因**：消除 agent "只评论不移卡"的死循环。原子工具保证评论和状态转换要么一起发生，要么都不发生。

### 决策验证机制

Harness 在 agent 执行完毕后检查 DB 状态：
```
agent 执行结束
  → _validate_agent_decision() 检查:
    - 卡片状态是否变化？
    - 或 ceo_decision 是否被设置？
  → 是 → session 标记 completed
  → 否 → 立即升级：设置 ceo_decision + session 标记 failed
```

### CEO Decision Reconciliation（v0.8）

每个 scheduler tick 执行 `_reconcile_ceo_decisions()`：
- 扫描所有非终态、无 running session 的卡片
- 如果 `progressed_at` 超过阈值（默认 600s）且无 agent 在跑 → 自动升级
- 如果已有 ceo_decision 且 agent 已恢复运行 → 自动清除 ceo_decision

### Token 消耗统计（v0.8）

- Agent session 完成时记录 `input_tokens/output_tokens/total_tokens`
- Chat task 完成时同样记录 token 用量
- API 端点 `/api/stats/tokens` 提供按角色、按时段汇总

### Session 生命周期

| 状态 | 说明 |
|------|------|
| running | 正在执行 |
| completed | 成功完成（决策验证通过） |
| failed | 失败，retry_count < 2 自动重试（指数退避 10s×2^n） |
| blocked | 超过重试次数，等待人工干预 |
| cancelled | re kill + fail

2. **Card Reconciliation**（SchedulerEngine._reconcile_running_sessions，每 tick）：
   - 卡片到终态（done/archived）→ cancel session
   - 卡片移出角色管辖列 → cancel session

3. **CEO Decision Reconciliation**（SchedulerEngine._reconcile_ceo_decisions，每 tick）：
   - 卡片停滞超阈值 → 设置 ceo_decision 升级
   - agent 已恢复 → 清除 ceo_decision

### 心跳 + Stall 检测

agent 执行期间通过 `on_heartbeat` 回调更新时间戳。超过 120s 无心跳视为 stall，kill 进程并 fail session。

### 可观测性 API

| 端点 | 用途 |
|------|------|
| `GET /api/scheduler/state` | 聚合快照：running/stale/blocked/pending 分组 + 心跳可视化 |
| `GET /api/stats/tokens` | Token 消耗统计（按角色、今日、本周、总计） |
| `GET /api/cards/{code}/debug` | 单卡调试：session 历史、评论、commits、workspace 路径 |
| `GET /api/dev/logs/layers` | 日志按架构层分组 |

## 文件结构（核心）

```
kanban_harness/
├── main.py                    # FastAPI 入口 + lifespan + RingBufferHandler
├── config/
│   └── workflow.yaml          # 热加载运行时配置（轮询间隔、并发上限、超时）
├── core/
│   ├── database.py            # SQLite DDL + 迁移 + 连接管理
│   ├── session_manager.py     # Session 生命周期 + 双层 reconcile + stall 检测
│   ├── chat_task_manager.py   # 对话任务生命周期（与 SessionManager 同构）
│   ├── task_buffer.py         # 进程内发布-订阅 buffer（SSE 背压）
│   ├── workflow_config.py     # 热加载 workflow.yaml 单例
│   ├── config.py              # 工作区路径解析 + git clone/init
│   └── secret_filter.py       # 日志敏感信息过滤
├── scheduler/
│   ├── engine.py              # 主循环 + reconcile + dispatch + CEO decision
│   └── handlers.py            # Agent 结果处理 + 决策验证 + 产品记忆归档
├── agents/
│   ├── registry.py            # YAML 加载 + 权限检查 + 事件路由
│   ├── coach_dev.py           # worktree 管理 + Claude CLI 调用
│   ├── comment_agent.py       # 评论生成（hermes/claude_cli 双模式）
│   ├── mcp_config.py          # 为每个角色生成 MCP 配置
│   └── roles/*.yaml           # 角色声明（pm/industry/coach_dev/coach_review）
├── mcp_server/
│   ├── server.py              # FastMCP stdio 模式，8 个工具
│   └── kh_client.py           # httpx 异步 REST 客户端
├── web/
│   ├── api.py                 # 完整 REST API + CEO 决策端点 + 可观测性
│   ├── chat.py                # SSE 对话 + tool loop + 多 provider 支持
│   ├── hermes_chat.py         # Hermes CLI 桥接
│   └── middleware.py          # 权限网关 + 请求日志
└── web/static/                # Dashboard 前端（vanilla JS + SSE）
```

## 层间依赖规则

```
第 5 层（前端）  → 只调用第 2 层的 REST API，不直接碰编排逻辑
第 4 层（提示词）→ 被第 3 层加载和使用，自身无运行时依赖
第 3 层（编排）  → 依赖第 2 层读写数据，依赖第 1 层执行命令
第 2 层（基础设施）→ 依赖第 1 层的 SQLite/文件系统
第 1 层（OS）    → 无依赖，是一切的基座
```

**禁止跨层调用**：
- 前端不能直接调用 scheduler 方法（通过 API 间接控制）
- 提示词 YAML 不能包含 Python 代码
- 编排层不能硬编码 UI 相关逻辑
