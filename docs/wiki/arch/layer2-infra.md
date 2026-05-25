---
type: arch
updated: 2026-05-25
tags: [infra, database, buffer, mcp]
---

# 第 2 层：基础设施层

**职责**：数据持久化、配置管理、协议适配、进程内运行时基础设施。不含业务逻辑。

依赖：[[layer1-runtime]]
被依赖：[[layer3-orchestration]]、[[layer5-frontend]]

## core/database.py

| 管辖范围 | 说明 |
|----------|------|
| DB 连接管理 | `get_db()` 异步上下文管理器，WAL 模式 |
| 全量表结构定义 | `init_db()` 创建 13 张表 |
| 数据迁移 | `_migrate_db()` 处理 schema 演进 |
| 编号生成 | `next_code()` 生成 KH-001 格式编号 |
| 前缀生成 | `generate_prefix()` 从项目名提取 2-3 字母前缀 |

## core/task_buffer.py (v0.7)

| 管辖范围 | 说明 |
|----------|------|
| TaskBufferManager | 进程级单例，管理活跃 chat task 的内存缓冲 |
| 发布-订阅 | append_chunk 时 notify 所有 subscriber（asyncio.Event） |
| TTL 驱逐 | 已完成任务 1h 后自动清理，上限 100 个活跃任务 |
| TaskState/TaskChunk | 数据结构：chunks list + done flag + subscribers |

## core/config.py

| 管辖范围 | 说明 |
|----------|------|
| 工作区路径解析 | `get_project_repo_path()` — 解析/创建项目 git 仓库目录 |
| 自动 clone | 有 remote URL 时自动 git clone |
| 自动 init | 无 remote 时 git init + 空 commit |
| 自动 pull | 已有仓库时 `--ff-only` 拉取最新 |

## 数据模型

```
projects (根节点)
  ├──1:N── versions
  │          └──1:N── requirements (核心实体)
  │                     ├──1:N── comments
  │                     ├──1:N── attachments
  │                     └──1:N── requirement_commits
  ├──1:1── project_architecture
  ├──1:N── agent_sessions
  ├──1:N── agent_events (→ requirement_id 弱引用)
  ├──1:N── scheduled_tasks
  ├──1:N── chat_messages
  └──1:N── chat_tasks (v0.7)
```

### 各表功能

| 表名 | 功能 | 分类 |
|------|------|------|
| projects | 项目元数据 + product_memory | 看板核心 |
| versions | 版本/里程碑 | 看板核心 |
| requirements | 需求卡片，状态机载体 | 看板核心 |
| comments | 卡片评论，AI 角色协作载体 | 看板核心 |
| attachments | 卡片附件 | 看板核心 |
| project_architecture | 项目架构文档（markdown） | 知识存储 |
| requirement_commits | 需求与 git commit 关联 | Git 集成 |
| agent_sessions | AI 会话生命周期跟踪 | 编排支撑 |
| agent_events | 异步事件队列 | 编排支撑 |
| scheduled_tasks | 定时任务配置 | 编排支撑 |
| chat_messages | 对话历史 | 前端支撑 |
| chat_tasks | 后台对话任务生命周期 (v0.7) | 编排支撑 |

### 关键设计点

- 全部 CASCADE 删除：删 project 自动清理所有子数据
- agent_events.requirement_id 无外键约束（事件可能在卡片删除后仍需保留）
- agent_sessions 通过 input_context JSON 字段关联卡片（非外键，是技术债）
- advisor_skill / product_memory 直接存 projects 表字段（1:1 且内容小）

## mcp_server/

| 文件 | 说明 |
|------|------|
| server.py | FastMCP 实例，stdio 模式。工具：kh_brief, kh_submit_idea, kh_notify_event, kh_ask_pm, kh_approve/reject, kh_web_search, kh_web_extract |
| kh_client.py | 对 KH REST API 的 httpx 异步封装，X-Caller-ID header 标记来源 |
