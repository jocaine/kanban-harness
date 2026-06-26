# Kanban Harness (看板引擎)

AI 驱动的看板系统，多角色 Agent 自主完成调研、拆解、开发、审查 —— 你只需要在关键节点拍板。

## 特性

- **多角色 AI Agent** — 产品经理（需求拆解）、行业顾问（调研）、Coach-Dev（写代码）、Coach-QA（测试审查）
- **CEO 决策界面** — 王权风格的确认/否决/自定义回复，Agent 需要人类判断时弹出
- **自动化工作流** — 卡片自动流转：organizing → research → dev → testing → done
- **MCP 服务器** — 可接入 Claude Code、VS Code 或任何 MCP 兼容 IDE
- **完全自托管** — SQLite + Docker，除了 LLM API 不依赖任何外部服务
- **双模型路由** — 重模型处理复杂任务，轻模型处理调研/QA

## 快速开始（开发模式）

```bash
git clone https://github.com/jocaine/kanban-harness.git
cd kanban-harness

# 配置
cp .env.example .env
# 编辑 .env —— 至少填写 API_KEY 和 API_BASE_URL

# 启动（首次会构建镜像，约 5-10 分钟）
docker compose up -d

# 打开浏览器
# http://localhost:8766
```

## 快速开始（用户自托管）

```bash
# 一键安装（Linux/WSL）
curl -fsSL https://aipitabox.site/docker-images/install-cli.sh | bash

# 安装引导 —— 会提示输入 API key、URL、模型
kh install

# 启动
kh start
# 浏览器打开 http://localhost:8765
```

## 配置说明

复制 `.env.example` 为 `.env` 并填写：

| 变量 | 必填 | 说明 |
|------|------|------|
| `API_KEY` | 是 | LLM API 密钥（OpenAI / Anthropic / 兼容中转站） |
| `API_BASE_URL` | 是 | LLM 接口地址（如 `https://api.anthropic.com/v1`） |
| `CHAT_MODEL` | 是 | 模型名称（如 `claude-sonnet-4-6`） |
| `CHAT_MODEL_HEAVY` | 否 | 复杂任务模型（PM、Coach-Dev）。不填则用 CHAT_MODEL |
| `CHAT_MODEL_LIGHT` | 否 | 轻量任务模型（行业顾问、Coach-QA）。不填则用 CHAT_MODEL |
| `TAVILY_API_KEY` | 否 | 搜索质量提升（tavily.com 免费 1000 次/月） |
| `LOG_LEVEL` | 否 | `DEBUG` / `INFO` / `WARNING` / `ERROR`，默认 `INFO` |

### 国内网络说明

- Docker 镜像：`kh install` 自动检测网络环境，国内会使用阿里云镜像源
- LLM 中转站：`API_BASE_URL` 填你的中转站地址即可（如 `http://192.168.x.x:8317/v1`）
- SearXNG 搜索：内置自部署，不走外网搜索引擎直连

### WSL 用户注意

- 确保 Docker Desktop 已启用 WSL 2 集成
- `kh` CLI 会自动检测 WSL 环境并适配

## CLI 命令

```bash
kh install    # 首次安装：Docker 检测、镜像拉取、配置向导
kh start      # 启动所有服务
kh stop       # 停止所有服务
kh status     # 查看运行状态
kh logs       # 查看最近 50 行日志
kh config     # 重新配置 API key/URL/模型
kh update     # 拉取最新镜像 + 自更新 CLI
kh uninstall  # 删除容器和镜像（保留数据）
```

## 工作原理

1. 你在看板上创建一张卡片（功能需求、bug、想法）
2. **产品经理 Agent** 接手 —— 拆解为可执行任务，定义验收标准
3. 需要调研时，**行业顾问** 搜索网络、分析竞品
4. **Coach-Dev** 在隔离工作区中实现代码
5. **Coach-QA** 根据验收标准进行测试审查
6. 当 Agent 需要人类判断时，**CEO 决策界面**弹出确认/否决按钮
7. 卡片自动流转直到完成 —— 你只在被问到时介入

## 服务组成

| 服务 | 端口 | 用途 |
|------|------|------|
| web | 8766（开发）/ 8765（用户） | 主应用（API + 看板界面） |
| daemon | 8771 | 宿主机进程管理（工作区隔离） |
| searxng | 8888 | 自托管元搜索引擎 |
| firecrawl-lite | 3002 | 网页内容提取 |

## 技术栈

- **后端**: Python 3.11, FastAPI, uvicorn, aiosqlite (SQLite)
- **前端**: 原生 JS, marked.js, DOMPurify
- **AI**: Claude CLI (`@anthropic-ai/claude-code`), Hermes agent 框架
- **基础设施**: Docker Compose, SearXNG（搜索）, Firecrawl（网页抓取）

## License

MIT
