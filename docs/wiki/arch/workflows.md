---
type: arch
updated: 2026-05-25
tags: [workflow]
---

# 工作流

依赖：[[layer3-orchestration]]、[[layer4-roles]]

## 开发需求流程

```
PM 拆卡 → organizing → [人类批准] → dev → Coach-Dev 写码 → testing → Coach-QA 验收 → [人类批准 merge] → done
```

## 调研需求流程

```
调研需求 → research → Industry 执行 → PM 审计（提炼+分类）→ 写入产品记忆 → done
```

跳过 DEV/QA，参见 [[decisions]] 中 2026-05-16 决策。

## 人类控制机制

- 全局暂停 / 恢复
- 卡片否决 + 写原因
- 方向调整（修改产品记忆）
- merge 权限独占（AI 不能 push）

## 层间依赖规则

```
第 5 层（前端）  → 只调用第 2 层的 REST API，不直接碰编排逻辑
第 4 层（提示词）→ 被第 3 层加载和使用，自身无运行时依赖
第 3 层（编排）  → 依赖第 2 层读写数据，依赖第 1 层执行命令
第 2 层（基础设施）→ 依赖第 1 层的 SQLite/文件系统
第 1 层（OS）    → 无依赖，是一切的基座
```

**禁止跨层调用**：
- 前端不能直接调用 scheduler 方法（通过 API 间接控制）
- 提示词 YAML 不能包含 Python 代码
- 编排层不能硬编码 UI 相关逻辑
