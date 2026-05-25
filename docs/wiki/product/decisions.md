---
type: product
updated: 2026-05-25
tags: [decision, principle]
---

# 决策历史

相关：[[positioning]]、[[workflows-product]]、[[rejected]]

## 架构决策

- 2026-05-12：确认拆为独立项目，不与 Kanban MCP 混在一个项目管理
- 2026-05-12：共享层策略——抽出 kanban-core（数据模型+CRUD+状态机），两个项目都依赖它
- 2026-05-12：Industry 用 Hermes（本地，支持 tool use/联网），Coach/PM 用 Claude（API）
- 2026-05-12：部署方式 Docker Compose，零配置是底线
- 2026-05-12：人类交互通过 Web Dashboard，不通过 CLI
- 2026-05-12：Dashboard 内置对话窗口，不依赖外部 MCP 客户端
- 2026-05-12：MCP 层定位为"意图驱动的团队接口"，不暴露 CRUD
- 2026-05-13：PM 去掉 testing 权限，验收统一由 Coach-QA 负责
- 2026-05-16：调研需求独立工作流（PM 审计后直接 done + 写入记忆）
- 2026-05-16：产品记忆拆为市场分析和方向把控两章
- 2026-05-16：引入产品化光谱 L0-L4，当前目标 L1
- 2026-05-16：调研阶段不驱动架构选择——架构决策推迟到 pending→dev 转换节点
- 2026-05-16：耦合卡专属流程——调研内容本身依赖技术选型的卡片打 `coupling` tag
- 2026-05-25：v0.7 后台自治——chat 执行从 SSE 连接解耦为 background task，参见 [[layer2-infra]] 和 [[layer3-orchestration]]
- 2026-05-25：架构文档+产品记忆迁移为 LLM Wiki（Karpathy 模式），按需加载替代全量加载

## 流程纪律

### 调研阶段不驱动架构选择

调研产出只回答业务问题（值不值得做、做什么）。架构决策的触发时机是 pending→dev 转换节点：

- **不触发**：在已有技术栈内加功能
- **触发**：新项目第一个开发需求 / 引入新运行时或数据库 / CEO 主动点名技术
- 需触发时：PM 先在评论中咨询 Coach → Coach 输出技术方案 → CEO 确认 → 移入 dev

### 耦合卡专属流程

当调研内容本身依赖技术选型判断时，PM 打 `coupling` tag：

```
Industry（查）→ 行业+技术画像，只给事实不给结论
PM（判）→ 基于事实做技术判断（选型/结构/成本/风险）
CEO 决策 → 批准则创 dev 卡转 Coach-Dev，否决则记档
```

## MCP 层设计决策（2026-05-12）

意图驱动，非数据驱动。极简接口（4-5 个工具），外部 AI 无需理解 KH 内部结构。

规划工具：kh_brief / kh_submit_idea / kh_notify_event / kh_ask_pm / kh_approve_reject

不暴露：CRUD、agent session 细节、scheduler 内部状态。
