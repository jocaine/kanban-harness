---
type: arch
updated: 2026-06-02
tags: [workflow, decision]
---

# 工作流

依赖：[[layer3-orchestration]]、[[layer4-roles]]

## 开发需求流程

```
CEO 建卡(organizing) → PM 网关 → dev → Coach-Dev 写码 → testing → Coach-Review 验收 → done
                        ↓ 需调研
                     research → Industry 执行 → organizing → PM 审计 → dev
```

## 调研需求流程

```
CEO 建 research 卡 → organizing → PM 分发 → research → Industry 调研
                                                         ↓
                                              organizing → PM 审计（提炼+分类）→ done（写入产品记忆）
```

跳过 DEV/QA，参见 decisions 中 2026-05-16 决策。

## 角色执行模式

| 角色 | Provider | 工具 | 输出处理 |
|------|----------|------|---------|
| PM | claude_cli | 原子决策工具（pm_approve/pm_send_to_research/pm_ask_ceo） | stdout 忽略，harness 验证 DB |
| Industry | hermes | 原子决策工具（industry_complete/industry_ask_ceo）+ web_search | stdout 忽略，harness 验证 DB |
| Coach-Dev | claude_cli | 文件系统 + git | 产出 commit，harness 关联并移卡 |
| Coach-Review | claude_cli | 看板 MCP 工具 | stdout 代发评论 |

## 原子决策工具（v0.8）

**设计原因**：v0.7 中 agent 经常"只评论不移卡"，导致死循环。原子工具保证：
- 评论和状态转换绑定在一个工具调用中
- harness 通过 `_validate_agent_decision` 验证最终不变式

| 工具 | 角色 | 操作 |
|------|------|------|
| pm_approve | PM | 写评论 + 移卡（dev类型→dev, research类型→done） |
| pm_send_to_research | PM | 写评论 + 移卡→research |
| pm_ask_ceo | PM | 写评论 + 设 ceo_decision |
| industry_complete | Industry | 写评论(summary) + 写detail + 移卡→organizing |
| industry_ask_ceo | Industry | 写评论 + 设 ceo_decision |

## CEO Decision 升级流程

### 触发条件（任一）

1. Agent 执行完毕但未做决策（`_validate_agent_decision` 检测）
2. 卡片停滞超过 escalation_threshold（默认 600s）且无 agent 在跑

### 升级后的状态

```json
requirements.ceo_decision = {
  "role": "pm",
  "reason": "stuck_timeout" | "agent_no_decision",
  "message": "描述信息",
  "actions": ["retry", "reply_to_role", "move_to_dev", "archive"],
  "since": "2026-06-02 10:30:00"
}
```

### CEO 响应选项

| 决策 | 效果 |
|------|------|
| approve_dev | research类型→done, dev类型→dev |
| request_more_research | 移卡→research |
| reply_to_role | 写评论 + 保持当前列 + emit ceo_replied 事件触发角色重新执行 |
| retry | 清除 ceo_decision + 不移卡（等待 agent 重新触发） |

### 自动清除

当 ceo_decision 存在但对应 agent 已恢复运行时，reconcile 自动清除。

## PM 失败处理

当 PM 工具调用失败（决策验证不通过）：
1. `_validate_agent_decision` 检测到 DB 无状态变化且无 ceo_decision
2. 设置 ceo_decision(reason="agent_no_decision")
3. session 标记 failed
4. CEO 在王权面板看到并处理

## 产品记忆归档

PM 将 ch 卡标记 done 时，handlers 自动：
1. 读取 PM 最新评论
2. 解析结构化结论（可靠性、提炼要点、归档建议）
3. 追加到项目 product_memory 的"调研结论"章节

## 人类控制机制

- 全局暂停 / 恢复（scheduler pause/resume）
- CEO Decision 面板（批准/退回/回复/归档）
- 方向调整（修改产品记忆）
- merge 权限独占（AI 不能 push）
- 卡片手动移动（需提供原因）

## 层间依赖规则

```
第 5 层（前端）  → 只调 REST API
第 4 层（提示词）→ 被第 3 层加载，自身无运行时依赖
第 3 层（编排）  → 依赖第 2 层读写，依赖第 1 层执行
第 2 层（基础设施）→ 依赖第 1 层 SQLite/文件系统
第 1 层（OS）    → 无依赖
```
