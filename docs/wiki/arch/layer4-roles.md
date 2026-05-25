---
type: arch
updated: 2026-05-25
tags: [roles]
---

# 第 4 层：AI 角色/提示词层

**职责**：声明式定义角色行为，不含执行逻辑。

依赖：无运行时依赖
被依赖：[[layer3-orchestration]]（加载和使用）

## agents/roles/pm.yaml

| 管辖范围 | 说明 |
|----------|------|
| 角色身份 | display_name="产品经理", icon, color |
| 模型配置 | provider=claude_cli, model=claude-sonnet-4-6, timeout=600 |
| system_prompt | 需求拆解、调研审计、优先级判断 |
| allowed_tools | kanban MCP 工具列表（move, comment, create 等） |
| permissions | can_read/write/move 的具体资源和状态转换 |
| triggers | status_changed + condition 表达式 |

## agents/roles/industry.yaml

| 管辖范围 | 说明 |
|----------|------|
| 角色身份 | display_name="行业顾问" |
| 模型配置 | provider=anthropic (API 模式) |
| system_prompt | 调研方法论、双视角分析、信号冲突标注规则 |
| allowed_tools | web_search, web_extract, kanban_add_comment |
| triggers | status_changed → new_status=='research' |

## agents/roles/coach_dev.yaml

| 管辖范围 | 说明 |
|----------|------|
| 角色身份 | display_name="开发教练" |
| 模型配置 | provider=claude_cli |
| permissions | can_move: [dev->testing, dev->pending, dev->blocked] |
| triggers | status_changed → new_status=='dev' |

## agents/roles/coach_review.yaml

| 管辖范围 | 说明 |
|----------|------|
| 角色身份 | display_name="审查教练" |
| 模型配置 | provider=anthropic (隔离 session) |
| permissions | can_move: [testing->done, testing->dev] |
| triggers | status_changed → new_status=='testing' |
