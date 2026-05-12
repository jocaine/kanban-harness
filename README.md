# Kanban Harness

Local-first AI team orchestration engine. Kanban acts as the active scheduler, dispatching multiple AI roles (Industry Expert, PM, Coach) to work autonomously while humans maintain control through a Dashboard.

## What is this?

Kanban Harness turns a kanban board into a **proactive orchestration layer**:

- **Scheduler** decides when to trigger AI agents based on board state
- **Agents** (Industry/PM/Coach) execute research, planning, and review tasks
- **Dashboard** gives humans a CEO-level view: approve, redirect, or override

This is not a passive tool that waits for commands — it actively drives project progress.

## Architecture

```
┌─────────────────────────────────────────────┐
│                 Dashboard                    │
│         (FastAPI + Static Frontend)          │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│              Scheduler                       │
│   (cron / event-driven task dispatch)        │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│               Agents                         │
│  ┌──────────┐ ┌────┐ ┌───────────────────┐  │
│  │ Industry │ │ PM │ │ Coach (Dev/Review) │  │
│  └──────────┘ └────┘ └───────────────────┘  │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│                Core                          │
│   (SQLite data layer, state machine)         │
└─────────────────────────────────────────────┘
```

## Project Structure

```
kanban_harness/
├── core/           # Data layer (SQLite, models, state machine)
├── scheduler/      # Task scheduling and dispatch (v0.2)
├── agents/         # AI agent roles (v0.2)
├── web/            # Dashboard frontend + API
│   ├── api.py      # FastAPI routes
│   └── static/     # Frontend assets
├── docker/         # Docker deployment configs
├── main.py         # Entry point
├── requirements.txt
├── .env.example    # Configuration template
└── .gitignore
```

## Quick Start

```bash
# Clone
git clone https://github.com/jocaine/kanban-harness.git
cd kanban-harness

# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Run
python main.py
# Open http://localhost:8000
```

## Current Status (v0.1)

- [x] Core data layer (projects, versions, requirements, comments)
- [x] Web API (CRUD + Dashboard endpoints)
- [x] Scheduler API skeleton (mock responses)
- [x] Agent session tracking (DB schema ready)
- [ ] Scheduler implementation
- [ ] Agent orchestration (Claude API integration)
- [ ] Dashboard frontend redesign

## Relationship to Kanban MCP

| | Kanban MCP | Kanban Harness |
|---|---|---|
| Role | Passive MCP tool | Active orchestration engine |
| Trigger | AI assistant calls tools | Scheduler dispatches agents |
| Human interaction | Through AI chat | Through Dashboard |
| Deployment | MCP server | Docker / standalone |

Same data model, different execution philosophy.

## License

MIT
