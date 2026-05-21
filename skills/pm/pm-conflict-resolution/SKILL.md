---
name: pm-conflict-resolution
description: "PM 退回争议处理：Dev退回响应流程、blocked仲裁、PM侧陈述规则"
tags: [kanban-harness, pm, conflict, dev-pushback]
trigger: When Dev pushes back a card to organizing or card enters blocked status
---

# PM 退回争议处理指南

## 退回处理流程

当 Dev 将卡片退回到 organizing 列时，按以下步骤处理：

### 第一次退回

1. **阅读 Dev 评论** — 理解其提出的矛盾点和质疑
2. **判断合理性**：
   - **合理** → 修改 description 解决矛盾，再推回 dev，评论说明改了什么
   - **不合理** → 写评论反驳，推回 dev 并说明理由

### 再次退回（blocked）

如果 Dev 再次退回或标记 blocked：
1. **写 PM 侧陈述** — 在评论中完整阐述 PM 的立场和理由
2. **等待人类裁决** — blocked 状态意味着 PM 和 Dev 无法达成共识，需要 CEO 介入
3. **不要继续推拉** — 两次退回后不再强推，交给 CEO 决策

## PM 侧陈述要求

陈述必须包含：
- **需求背景**：为什么要做这个功能
- **PM 的判断**：为什么认为当前方案可行
- **对 Dev 质疑的回应**：逐条回应 Dev 提出的问题
- **建议方案**：如果有折中方案，提出来

## 关键原则

- 认真审视 Dev 提出的矛盾点，**不要简单驳回**
- Dev 的技术判断通常比 PM 更准确，尊重技术约束
- 如果 Dev 的质疑涉及架构问题，考虑咨询 Coach
- 目标是解决问题，不是赢得争论
