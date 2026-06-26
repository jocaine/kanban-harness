# Kanban Harness

AI-powered kanban board with autonomous multi-role agents that research, plan, code, and review — while you make the final calls.

> [中文文档 →](docs/README_CN.md)

## Features

- **Multi-role AI agents** — PM (requirements), Industry Expert (research), Coach-Dev (code), Coach-QA (review)
- **CEO decision interface** — Reigns-style approve/reject/custom reply when agents need human input
- **Automated workflow** — Cards flow through: organizing → research → dev → testing → done
- **MCP server** — Integrate with Claude Code, VS Code, or any MCP-compatible IDE
- **Self-hosted** — SQLite + Docker, no external services required (besides your LLM API)
- **Dual-model routing** — Heavy model for complex tasks, light model for research/QA

## Architecture

```
Browser (localhost:8766)
        |
Web API (FastAPI + uvicorn)
  - Dashboard UI
  - CEO Decision Overlay
  - Hermes Chat
  - MCP Server
        |
Scheduler Engine (30s polling)
  - Event dispatch
  - Session management (max 5 concurrent)
  - Escalation logic (timeout/no-decision -> CEO)
        |
Agent Sessions
  - PM         (claude_cli)  -> organizing cards
  - Industry   (hermes)      -> research + web search
  - Coach-Dev  (claude_cli)  -> code implementation
  - Coach-QA   (claude_cli)  -> test + review
        |
   +---------+-----------+
   |         |           |
LLM API   SearXNG    Host Daemon
(yours)  (port 8888) (port 8771)
```

## Quick Start (Development)

```bash
git clone https://github.com/jocaine/kanban-harness.git
cd kanban-harness

# Configure
cp .env.example .env
# Edit .env — set API_KEY and API_BASE_URL at minimum

# Launch (builds image on first run)
docker compose up -d

# Open browser
open http://localhost:8766
```

## Quick Start (Self-hosted User)

```bash
# One-line install (Linux/WSL)
curl -fsSL https://aipitabox.site/docker-images/install-cli.sh | bash

# Setup — prompts for API key, URL, model
kh install

# Run
kh start
# Opens http://localhost:8765
```

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `API_KEY` | Yes | Your LLM API key (OpenAI / Anthropic / compatible proxy) |
| `API_BASE_URL` | Yes | LLM endpoint URL (e.g. `https://api.anthropic.com/v1`) |
| `CHAT_MODEL` | Yes | Model name (e.g. `claude-sonnet-4-6`) |
| `CHAT_MODEL_HEAVY` | No | Model for complex tasks (PM, Coach-Dev). Falls back to CHAT_MODEL |
| `CHAT_MODEL_LIGHT` | No | Model for lighter tasks (Industry, Coach-QA). Falls back to CHAT_MODEL |
| `TAVILY_API_KEY` | No | Better search quality (free 1000/month at tavily.com) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default: `INFO` |

## CLI Reference

```bash
kh install    # First-time setup: Docker check, image pull, config wizard
kh start      # Start all services
kh stop       # Stop all services
kh status     # Show running status and health
kh logs       # Stream last 50 lines of logs
kh config     # Reconfigure API key/URL/model interactively
kh update     # Pull latest image + self-update CLI
kh uninstall  # Remove container and image (keeps data)
```

## Project Structure

```
kanban_harness/
  agents/           # AI agent implementations + role configs (YAML)
  config/           # workflow.yaml (scheduler tuning)
  core/             # Database, card logger, stable build, workflow config
  deploy/           # CLI installer + cross-platform start/stop scripts
  docker/           # Dockerfiles for sub-services (firecrawl, hermes)
  mcp_server/       # MCP tool server (agent tools exposed via MCP protocol)
  scheduler/        # Engine (polling + dispatch) + handlers (result processing)
  scripts/          # Host daemon (process isolation)
  web/              # FastAPI app + static frontend (dashboard, CEO overlay)
  docker-compose.yml
  Dockerfile
  .env.example
```

## Tech Stack

- **Backend**: Python 3.11, FastAPI, uvicorn, aiosqlite (SQLite)
- **Frontend**: Vanilla JS, marked.js, DOMPurify
- **AI**: Claude CLI (`@anthropic-ai/claude-code`), Hermes agent framework
- **Infra**: Docker Compose, SearXNG (search), Firecrawl (web scrape)
- **Build tools in container**: git, ripgrep, fd, cmake, g++, pnpm

## Services (docker-compose)

| Service | Port | Purpose |
|---------|------|---------|
| web | 8766 | Main app (API + dashboard) |
| daemon | 8771 | Host process manager (workspace isolation) |
| searxng | 8888 | Self-hosted metasearch engine |
| firecrawl-lite | 3002 | Web content extraction |

## How It Works

1. You create a card (feature request, bug, idea) on the board
2. **PM agent** picks it up — breaks it into actionable tasks with acceptance criteria
3. If research is needed, **Industry Expert** searches the web and analyzes competitors
4. **Coach-Dev** implements the code in an isolated workspace
5. **Coach-QA** reviews and tests against the acceptance criteria
6. When agents need human judgment, the **CEO overlay** pops up with approve/reject buttons
7. Cards flow automatically until done — you only intervene when asked

## Contributing

```bash
# Dev setup
git clone https://github.com/jocaine/kanban-harness.git
cd kanban-harness
cp .env.example .env
# Fill .env with your LLM credentials
docker compose up -d

# Logs
docker compose logs -f web

# Restart after code changes (hot-reload is on, but sometimes needed)
docker compose restart web
```

## License

MIT
