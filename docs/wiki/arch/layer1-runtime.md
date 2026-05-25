---
type: arch
updated: 2026-05-25
tags: [runtime, docker]
---

# 第 1 层：容器运行时 / OS 层

**职责**：为 AI agent 提供干净、无干扰、工具齐全的执行环境。

依赖：无，是一切的基座。
被依赖：[[layer2-infra]]、[[layer3-orchestration]]

## Dockerfile

| 管辖范围 | 说明 |
|----------|------|
| 基础镜像 | python:3.11-slim（Debian，轻量） |
| AI 友好环境变量 | 9 个 ENV 消除交互/颜色/编码干扰 |
| 系统工具 | git, curl, jq, gcc, cmake, ripgrep, fd-find, tree, procps |
| Node.js | nodesource 20.x + pnpm |
| AI CLI 工具 | hermes-agent (pip), claude-code (npm) |
| Git 预配置 | user.name/email 供 Coach-Dev commit 用 |
| 构建分层 | 按变化频率排列，利用 Docker 缓存 |

## scripts/ai-exec

| 管辖范围 | 说明 |
|----------|------|
| 命令超时保护 | 默认 120s，超时 SIGKILL + 返回 137 |
| 输出截断 | stdout/stderr 超 50KB 截断，显示实际大小 |
| 退出码标准化 | 超时=137, not found=127, 正常透传 |
| 安装建议 | command not found 时 apt-cache 搜索候选包 |

## config/toolchain_map.json

| 管辖范围 | 说明 |
|----------|------|
| 技术栈检测 | 9 种语言/框架的检测关键词 |
| 验证命令 | 每种栈的 check 命令列表 |
| 安装方式 | apt 包名 + install_hint 一行命令 |

## docker-compose.yml

| 管辖范围 | 说明 |
|----------|------|
| 容器编排 | 定义服务组合和网络 |
| 端口映射 | 8765 对外暴露 |
| 卷挂载 | data/ 持久化 |
