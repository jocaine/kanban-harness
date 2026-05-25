---
type: product
updated: 2026-05-25
tags: [positioning, user-profile]
---

# 核心定位与用户画像

相关：[[decisions]]、[[workflows-product]]、[[market]]

## 产品定位

**"预设产品管理场景的 AI 团队成品"**——不是通用编排框架，不是积木，是开箱即用的 AI 员工团队。

| 竞品 | 给你什么 | Kanban Harness 给你什么 |
|------|----------|------------------------|
| CrewAI/AutoGen | 编排积木，自己搭 | 成品团队，开箱即用 |
| Claude Code/Cursor | 被动工具，人驱动 | 主动团队，自主运转 |
| Jira/Linear | 记录工具，人操作 | 执行引擎，AI 操作 |

## 解决的核心问题

**"我一个人精力有限，想法落不了地。"**

个人开发者的困境：有产品想法，但调研、规划、开发、测试全靠自己，精力分散导致项目停滞。KH 给你一个 AI 团队：Industry 盯行业动态，PM 整理优先级，Coach 写代码——你只做 CEO 该做的事：定方向、看报告、拍板。

## 与 Kanban MCP 的关系

同一品牌两个产品线，共享数据模型层（kanban-core）：
- **Kanban MCP**：AI 协作透明度基础设施（AI→Kanban，被动）
- **Kanban Harness**：AI 团队编排引擎（Kanban→AI，主动）

控制流方向完全反转。

## 用户画像

- 背景：Qt/C++ 客户端开发者，对 Web 和 DevOps 不熟悉
- 使用方式：通过 Web Dashboard 监控 AI 团队，偶尔介入决策
- 目标：让 AI 团队帮自己推进项目，自己只做方向决策
- 偏好：简单直接，零配置，MVP 先行
- 技术约束：本地机器运行 Docker，需要能跑 Hermes（至少 8GB VRAM）
- **核心身份认知**：我是 CEO 和产品决策者，AI 团队是我的员工

## 产品化目标

productization_target: L1（可用）

| 级别 | 描述 | 特征 |
|------|------|------|
| L0 原型 | 能跑就行 | cron + raw 输出，无 UI |
| L1 可用 | 能用 | Docker 一键启动 + Dashboard 可看可操作 |
| L2 好用 | 体验完善 | 通知、异常处理、可控性 |
| L3 产品化 | 成熟 | 文档完整、安装流畅、降级路径清晰 |
| L4 商业化 | 可销售 | 多租户、权限、审计、SLA |

升级须 CEO 拍板。
