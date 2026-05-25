---
type: arch
updated: 2026-05-25
tags: [orchestration, scheduler, session, task]
---

# 第 3 层：编排调度层

**职责**：决定"何时调用谁"、管理 session/task 生命周期、事件路由、状态机流转。

依赖：[[layer2-infra]]、[[layer1-runtime]]
被依赖：[[layer5-frontend]]（通过 API 间接控制）

## scheduler/engine.py — SchedulerEngine

| 管辖范围 | 说明 |
|----------|------|
| 主循环 `_poll_loop` | 每 30s tick 执行任务 |
| `_tick` | 找 dev 卡片 + 处理 pending 事件 |
| `_find_actionable_cards` | 查询 status=dev 且未 archived 的卡片 |
| `_has_running_session` | 防止同一卡片重复触发 |
| `_trigger_coach_dev` | 创建 session → 分配 assignee → spawn asyncio task |
| `_run_agent` | 执行 Coach-Dev，成功则移卡到 testing + 关联 commit |
| `_process_events` | 消费 agent_events 表，匹配触发角色 |
| `_trigger_comment_agent` | 为匹配角色创建 session 并 spawn 执行 |
| `_recover_stuck_pending_cards` | 每 10 tick 检查卡死卡片，重新 emit 事件 |
| 暂停/恢复 | `pause()` / `resume()` 控制调度器 |

## core/session_manager.py — SessionManager

| 管辖范围 | 说明 |
|----------|------|
| `create_session` | 创建 running 状态的 session 记录 |
| `complete_session` | 标记完成 + 写 output_summary |
| `fail_session` | 失败处理：retry_count < 2 则自动重试，否则标记 blocked |
| `check_timeouts` | 扫描超时 session（默认 600s） |
| `_timeout_loop` | 每 30s 执行一次超时检查 |

## core/chat_task_manager.py — ChatTaskManager (v0.7)

| 管辖范围 | 说明 |
|----------|------|
| `create_task` | 创建 chat_tasks 记录 + 内存 buffer，返回 task_id |
| `run_task` | 消费 AI generator，写 chunk 到 buffer，完成后持久化到 DB |
| `get_task` / `get_active_task` | 查询任务状态（先查 buffer，再查 DB） |
| `get_completed_response` | 从 DB 读已完成任务的响应（buffer 驱逐后的回放） |
| `recover_orphans` | 启动时将 running 状态的孤儿任务标记为 failed |

**与 SessionManager 的关系**：同构设计，不同消费者。SessionManager 服务 scheduler 驱动的自动化 agent；ChatTaskManager 服务用户发起的对话任务。

## agents/registry.py — AgentRegistry

| 管辖范围 | 说明 |
|----------|------|
| YAML 加载 | 启动时扫描 agents/roles/*.yaml 加载角色定义 |
| 角色查询 | `get(role_name)` 返回 AgentRole dataclass |
| 权限检查 | `check_permission(role, action, resource)` |
| 事件路由 | `roles_for_trigger(event, context)` — 匹配触发角色 |

## agents/ — Agent 实现

| 文件 | 说明 |
|------|------|
| coach_dev.py | worktree 管理 + 工具链预检 + Claude CLI 调用 + scaffold 模式 |
| pm_agent.py | CLI 模式执行 PM 操作（调研审计、需求拆解） |
| comment_agent.py | API 模式执行评论生成（prompt 构建 + 上下文注入） |
| event_types.py | 事件类型常量 |
