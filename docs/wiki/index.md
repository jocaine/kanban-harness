# KH Wiki Index

> 导航入口。新会话先读这个文件定位相关页面。
> Last updated: 2026-05-25 | Total pages: 11

## Architecture

KH 五层架构，从底层到顶层：

- [[layer1-runtime]] — 容器运行时 / OS 层（Dockerfile, ai-exec, toolchain）
- [[layer2-infra]] — 基础设施层（SQLite, Config, MCP Server, TaskBuffer）
- [[layer3-orchestration]] — 编排调度层（Scheduler, SessionManager, ChatTaskManager, Agent 执行）
- [[layer4-roles]] — AI 角色/提示词层（YAML 定义, system prompt, triggers）
- [[layer5-frontend]] — 前端层（FastAPI, REST API, SSE, Dashboard UI）
- [[workflows]] — 工作流（开发需求流程、调研需求流程、人类控制机制）

## Product

- [[positioning]] — 核心定位 + 差异化 + 用户画像
- [[decisions]] — 架构决策历史（带日期）+ 流程纪律
- [[market]] — 市场分析（开源/商业视角）
- [[workflows-product]] — 角色设计 + 协作工作流 + 产品化光谱
- [[rejected]] — 已否决方向
