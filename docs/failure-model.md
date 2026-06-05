# KH 故障模型（Failure Model）

KH 调度器的 5 类故障定义及恢复策略。

## 故障分类

| # | 类型 | 日志 tag | 触发条件 | 恢复策略 | 影响范围 |
|---|------|---------|---------|---------|---------|
| 1 | 配置故障 | `[FAULT:CONFIG]` | workflow.yaml 解析失败、环境变量缺失 | 保持上一次有效配置继续运行，log warning | 仅影响新配置生效，不中断调度 |
| 2 | Agent Session 故障 | `[FAULT:AGENT]` | Claude CLI crash、超时、stall（无心跳） | 指数退避重试（10s→20s→40s），2 次后标记 blocked | 仅影响单张卡片 |
| 3 | 看板/DB 故障 | `[FAULT:DB]` | SQLite 连接失败、MCP 查询超时 | 跳过本轮 tick，下轮重试；查询失败不杀正在跑的 agent | 本轮调度跳过，不影响运行中 session |
| 4 | Workspace 故障 | `[FAULT:WORKSPACE]` | git 操作失败、worktree 创建失败、路径校验失败 | fail 当前 attempt，触发 retry | 仅影响单张卡片 |
| 5 | 可观测性故障 | `[FAULT:OBSERVE]` | 日志写入失败、/dev/logs API 异常 | 静默忽略，不影响调度器主循环 | 无影响 |

## 核心原则

1. **故障隔离** — 一个 agent 挂了不影响其他 agent，DB 查询失败不杀正在跑的进程
2. **优雅降级** — 配置坏了用旧的，日志坏了不管，调度继续
3. **有限重试** — 最多 2 次指数退避，之后标记 blocked 等人工介入
4. **不丢状态** — session 状态持久化在 SQLite，进程重启后 reconcile 恢复

## 日志 tag 规范

故障日志统一使用 `[FAULT:<TYPE>]` 前缀：

```
[FAULT:CONFIG]     — 配置加载/解析失败
[FAULT:AGENT]      — agent 进程崩溃/超时/stall
[FAULT:DB]         — 数据库查询失败
[FAULT:WORKSPACE]  — git/文件系统操作失败
[FAULT:OBSERVE]    — 日志/监控自身故障
```

正常运行日志保持现有 tag（`[SCHED]`、`[RECONCILE]`、`[MOVE]` 等）不变。

## 代码对应关系

| 故障类型 | 代码位置 | try/except |
|---------|---------|-----------|
| CONFIG | `core/workflow_config.py:_load()` | yaml 解析失败 → warning + 保持旧值 |
| AGENT | `scheduler/handlers.py:handle_coach_dev_result()` | agent.execute() 异常 → fail_session() |
| AGENT | `scheduler/handlers.py:handle_comment_agent_result()` | agent.execute() 异常 → fail_session() |
| AGENT | `core/session_manager.py:check_timeouts()` | 超时/stall → kill + fail_session() |
| DB | `scheduler/engine.py:_get_card_status()` | 查询失败 → return None（不杀 session） |
| DB | `scheduler/engine.py:_tick()` | tick 整体异常 → log + 继续下一轮 |
| WORKSPACE | `agents/coach_dev.py:execute()` | cwd 校验失败 → raise → fail_session() |
| WORKSPACE | `agents/coach_dev.py:_setup_worktree()` | git 操作失败 → raise → fail_session() |
| OBSERVE | `main.py:RingBufferHandler` | 日志 handler 异常不传播 |
