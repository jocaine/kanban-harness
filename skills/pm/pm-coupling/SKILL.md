---
name: pm-coupling
description: "PM 耦合卡技术判断：调研依赖技术选型时的协作流程和输出模板"
tags: [kanban-harness, pm, coupling, tech-decision]
trigger: When card has coupling/coupling-urgent tag and needs PM technical judgment
---

# PM 耦合卡技术判断指南

## 什么是耦合卡

调研类型的卡片（从 research 移来），其调研内容**本身依赖技术选型判断**：
- 「调研用 Rust 重写服务的可行性」
- 「对比 PostgreSQL 和 SQLite 哪个适合我们」
- 「评估引入 Redis 的必要性」

这类卡片是"调研-开发耦合"场景。

## 标签体系

- `coupling` → 耦合卡（Industry 查 + PM 判）
- `coupling-urgent` → 紧急耦合卡（快速出初稿，降低调研深度要求）

## 协作流程

### 1. Industry 负责「查」

Industry 上网一次搜完行业信息 + 技术信息，**只给事实不给结论**：
- 各方案的社区活跃度、包生态
- 竞品分别用了什么技术
- 各方案的已知优缺点（事实性描述）
- 性能基准数据（如有）

### 2. PM 负责「判」

基于 Industry 的事实数据做技术判断，输出必须包含：
- **技术选型推荐 + 理由**：选什么、为什么
- **大概的项目结构和关键依赖**：让 Coach-Dev 能直接上手
- **成本和风险评估**：实现难度、维护成本、迁移风险
- **具体到 Coach-Dev 能直接拿去写代码的程度**

### 3. CEO 决策

- 批准 → PM 创建 dev 卡转 Coach-Dev
- 否决 → 记档到产品记忆

## PM 技术判断输出模板

```
[调研充分]

技术选型：推荐 XXX
理由：
1. ...
2. ...

项目结构建议：
- 核心模块：...
- 关键依赖：...
- 数据模型：...

成本评估：
- 实现周期：...
- 维护复杂度：...

风险：
- ...

---DETAIL---

逐条对照 Industry 数据：
1. 方案 A vs 方案 B 对比...
2. 社区生态评估...
（完整技术分析过程）
```

## 注意事项

- 耦合卡流程**不阻断流转**——Industry 和 PM 的协作通过评论异步完成
- 不需要"等审批"才能继续
- PM 的技术判断要具体，不能停留在"建议用 X"的层面
