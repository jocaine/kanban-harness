---
type: product
updated: 2026-05-25
tags: [workflow, principle]
---

# 角色设计与工作流

相关：[[layer4-roles]]、[[decisions]]、[[positioning]]

## 角色设计

**Industry（Hermes + tools）**
- 定期按调研主题工作，产出写入看板卡片/评论
- 双视角分析：每项调研需同时评估开源视角和商业视角
- 信号冲突标注 `⚠️ 信号冲突`，不做倾向性结论

**PM（Claude）**
- 职责：需求拆解、优先级排序、分配任务、调研审计、移卡 dev 前架构判断
- 调研审计三步：①判断可靠性 ②提炼结构化表述 ③分类归入市场分析或方向把控
- 按优先级分四级审计深度：P0 深度审计、P1 批量审阅、P2 自动入库+周度巡检、P3 自动存档

**Coach-Dev（Claude）**
- 在 feature branch 写代码，commit 关联卡片
- 可退回需求（dev→pending），可请求搁置（dev→blocked）
- 不参与调研阶段讨论

**Coach-QA（Claude，隔离 session）**
- 代码审查 + 需求验收，只看 diff 不看开发过程

## 质量门禁

Coach-Dev commit → 自动测试 → Coach-QA 验收 → 人类批准 merge。测试失败重试最多 2 次，QA 拒绝可修改 1 次，超限标记 blocked。

## MVP 验证路径

1. ✅ cron + Claude CLI，Kanban 主动触发 Coach 做一张卡
2. ✅ Industry + Hermes + 搜索工具，验证调研产出价值
3. 加入 Review，验证 Dev/Review 分离
4. 加入 PM 审计，验证调研需求独立工作流
5. Docker 打包，一键部署
