---
type: arch
updated: 2026-06-02
tags: [orchestration, scheduler, session, task, handlers]
---

# 第 3 层：编排调度层

**职责**：决定"何时调用谁"、管理 session/task 生命周期、事件路由、状态机流转、决策验证。

依赖：[[layer2-infra]]、[[layer1-runtime]]
被依赖：[[layer5-frontend]]（通过 API 间接控制）

## scheduler/engine.py — SchedulerEngine

| 管辖范围 | 说明 |
|----------|------|
| 主循环 `_poll_loop` | 每 30s tick（可配置） |
| `_tick` | reload_config → reconcile_sessions → reconcile_ceo_decisions → find_cards → process_events → recover_stuck |
| `_reconcile_running_sessions` | 卡片终态/移走 → cancel session |
| `_reconcile_ceo_decisions` | 卡片停滞超阈值 → 设 ceo_decision 升级；agent 恢复 → 清除 |
| `_find_actionable_cards` | 查询 status=dev AND type=dev 的卡片 |
| `_has_running_session` | 防止同一卡片重复触发 |
| `_repo_is_ready` | 确保 git 仓库就绪（clone/init/rename master→main） |
| `_trigger_coach_dev` | 创建 session → 设 assignee → spawn handler task |
| `_process_events` | 消费 agent_events，匹配触发角色 |
| `_trigger_comment_agent` | 设 assignee → 创建 session → spawn handler task |
| `_recover_stuck_cards` | 检查停滞卡片，重新触发对应角色 |
| 暂停/恢复 | `pause()` / `resume()` |

### 配置热加载

每个 tick 开始时调用 `workflow_config.reload_if_changed()`，检查 `config/workflow.yaml` 的 mtime，变更自动生效。

### CEO Decision Reconciliation（v0.8 新增）

```python
async def _reconcile_ceo_decisions(self):
    # 1. 扫描所有非终态、未归档卡片
    # 2. 已有 ceo_decision 且 agent 在跑 → 清除（agent 已恢复）
    # 3. 无 ceo_decision、无 agent 在跑、progressed_at 超阈值 → 升级
```

阈值由 `workflow_config.escalation_threshold`（默认 600s）控制。

## scheduler/handlers.py — 结果处理（v0.8 重构）

从 engine.py 分离出来的独立模块，职责：接收 agent 输出 → 验证决策 → 路由卡片。

| 函数 | 说明 |
|------|------|
| `handle_coach_dev_result` | 执行 CoachDev → 成功则移卡 testing + 关联 commit；无 commit 则 continuation_retry |
| `handle_comment_agent_result` | 执行 CommentAgent → 调用 `_validate_agent_decision` |
| `_validate_agent_decision` | **统一决策验证**：检查 DB 状态变化或 ceo_decision |
| `parse_pm_research_conclusion` | 解析 PM 评论中的结构化结论 |
| `append_research_to_memory` | 追加调研结论到项目 product_memory |

### _validate_agent_decision 流程

```
agent 执行结束
  → 读 DB: current_status vs old_status, ceo_decision
  → status_changed OR has_ceo_decision?
    → YES: session completed
      → 如果是 PM 完成 research 类型卡片 → 解析结论 → 写入产品记忆
    → NO: agent 未决策
      → 设置 ceo_decision(reason="agent_no_decision")
      → session failed
```

## core/session_manager.py — SessionManager

| 管辖范围 | 说明 |
|----------|------|
| `create_session` | 创建 running session，支持 token 字段 |
| `complete_session` | 标记完成 + 写 output_summary + 记录 tokens |
| `fail_session` | retry_ct < 2 则指数退避重试（10s×2^n，上限 300s），否则 blocked |
| `cancel_session` | kill 进程 + 标记 failed(cancelled:reason) |
| `continuation_retry` | 无 commit 时立即重试（不增 retry_count） |
| `heartbeat` | 更新 monotonic 时间戳 |
| `register_process` / `unregister_process` | 跟踪子进程 |
| `reconcile_sessions` | 4 类健康检查（进程崩溃/孤儿/超时/stall） |
| `_reconcile_loop` | 每 30s 执行健康检查 |

### Session 健康检查（reconcile_sessions）

| 检查类型 | 条件 | 动作 |
|---------|------|---- > 120s | kill + fail(stall:Ns) |

## core/chat_task_manager.py — ChatTaskManager

| 管辖范围 | 说明 |
|----------|------|
| `create_task` | 创建 chat_tasks 记录 + 内存 buffer |
| `run_task` | 消费 AI generator → buffer → 完成后持久化（含 tokens） |
| `get_task` / `get_active_task` | 查询任务状态 |
| `get_completed_response` | DB 回放已完成任务 |
| `recover_orphans_timeout | 120s | 无心跳多久算 stal600s | 停滞多久升级 CEO |

## agents/registry.py — AgentRegistry

| 管辖范围 | 说明 |
|----------|------|
| YAML 加载 | 启动时扫描 agents/roles/*.yaml |
| 权限检查 | `check_permission(role, action, resource)` |
| 移卡权限 | `check_move(role, from_status, to_status)` |
| 事件路由 | `roles_for_trigger(event, context)` |

## agents/ — Agent 实现

| 文件 | 说明 |
|------|------|
| coach_dev.py | worktree 管理 + 工具链预检 + Claude CLI + scaffold 模式 |
| comment_agent.py | hermes/claude_cli 双模式；PM 返回空 comment 由 harness 验证 DB |
| mcp_config.py | 为每个角色生成 .mcp.json + settings.json |

## | 故障类别 | 恢复行为 |
|---------|---------|
| 配置故障 | 跳过 dispatch，保持服务 |
| Workspace 故障 | 当次失败，走 retry |
| Agent session 故障 | 指数退避（10s×2^n，上限 5min，最多 2 次）→ blocked |
| DB 故障 | 整个 tick 跳过 |
| 可观测性故障 | 无影响 |
