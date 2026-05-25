---
type: arch
updated: 2026-05-25
tags: [frontend, api, sse]
---

# 第 5 层：前端层

**职责**：CEO 的操作界面——看报告、批方案、踩刹车、对话指挥。

依赖：[[layer2-infra]]（通过 REST API）、[[layer3-orchestration]]（通过 ChatTaskManager）
被依赖：无（最顶层）

## main.py

| 管辖范围 | 说明 |
|----------|------|
| FastAPI 应用入口 | 创建 app 实例，挂载路由和中间件 |
| 生命周期管理 | lifespan: init_db → ensure_hermes_config → recover_orphans → scheduler.start |
| 静态文件服务 | mount /static 目录 |

## web/api.py

| 管辖范围 | 说明 |
|----------|------|
| 看板数据 CRUD | projects/versions/requirements/comments 完整 REST API |
| 权限强制 | 检查 X-Agent-Role header 的移动/创建权限 |
| 事件 emit | 创建需求/移动状态时自动写入 agent_events |
| 调度器控制 | /scheduler/status, /pause, /resume |
| Agent 活动查询 | /agents/sessions, /agents/status |
| CEO 决策接口 | /decisions/pending, /decisions/{rid}/submit |

## web/chat.py

| 管辖范围 | 说明 |
|----------|------|
| POST /chat/stream | SSE 流式对话（v0.7: 后台 task 驱动） |
| POST /chat/tasks | 创建后台对话任务，立即返回 task_id |
| GET /chat/tasks/active | 查询项目当前运行中的任务（重连用） |
| GET /chat/tasks/{id}/stream | SSE 观察端点，支持断点续传 |
| _execute_and_save | 薄胶水：构建 AI generator → 委托 ChatTaskManager |
| _stream_from_buffer | SSE 格式化：从 buffer 读取 + keepalive |
| _build_pm_system_prompt | 注入项目上下文+看板状态+对话历史 |
| _chat_with_tools | 多轮 tool loop（最多 5 轮） |

## web/hermes_chat.py

| 管辖范围 | 说明 |
|----------|------|
| stream_hermes | Hermes subprocess 桥接（prompt → spawn → 解析输出） |
| _detect_role | 根据关键词判断 pm/coach_dev/industry/coach_review |
| _build_hermes_prompt | 注入项目信息+看板状态+对话历史 |
| ensure_hermes_config | 启动时确保 MCP 指向本地 |

## web/middleware.py

| 管辖范围 | 说明 |
|----------|------|
| PermissionGateway | 拦截带 X-Agent-Role header 的请求，强制权限检查 |
| RequestLogger | 记录 agent/mcp 操作日志 |

## web/static/

| 管辖范围 | 说明 |
|----------|------|
| app.js | 前端逻辑（vanilla JS）：看板渲染、对话（两步 task 提交 + reconnect）、角色面板 |
| style.css | 全局样式 |
| avatars/ | 4 个角色头像 |
| lib/ | marked.min.js + purify.min.js |
