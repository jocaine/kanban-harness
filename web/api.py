import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import os
import aiosqlite

logger = logging.getLogger("kh.web.api")

from core.database import get_db, next_code, generate_prefix
from agents.registry import registry
from web.chat import router as chat_router

router = APIRouter()
router.include_router(chat_router)


def _normalize_newlines(s: str) -> str:
    """Convert literal \\n sequences to real newlines (common when AI agents submit text)."""
    if s and '\\n' in s:
        return s.replace('\\n', '\n')
    return s

# ==================== Pydantic Models ====================

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    color: str = "#4f46e5"
    prefix: str = ""

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    prefix: Optional[str] = None

class VersionCreate(BaseModel):
    project_id: int
    name: str
    description: str = ""

class VersionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    git_tag: Optional[str] = None

class ReqCreate(BaseModel):
    version_id: int
    title: str
    description: str = ""
    priority: str = "P2"
    type: str = "dev"
    status: str = "organizing"
    assignee: str = ""
    deadline: str = ""
    estimated_hours: float = 0
    notes: str = ""
    queue_reason: str = ""
    agent_timeout: Optional[int] = None
    initial_comment: str = ""

class ReqUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    deadline: Optional[str] = None
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    queue_reason: Optional[str] = None
    agent_timeout: Optional[int] = None

class ReqMove(BaseModel):
    status: str
    position: int = 0
    reason: str = ""

class CommentCreate(BaseModel):
    author: str = ""
    content: str

# ==================== 0. App Version ====================


def _get_app_version() -> str:
    """Read APP_VERSION from env, fallback to git describe or 'dev'."""
    ver = os.getenv("APP_VERSION", "")
    if ver:
        return ver
    try:
        import subprocess
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "dev"


@router.get("/version")
async def get_version():
    return {"version": _get_app_version()}


# ==================== 1. Kanban Data API ====================

@router.get("/projects")
async def list_projects(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT id, name, description, color, prefix, archived, created_at, updated_at, "
        "git_repo_path, git_remote_url, "
        "(SELECT COUNT(*) FROM versions WHERE project_id=p.id) as version_count, "
        "(SELECT COUNT(*) FROM requirements r JOIN versions v ON r.version_id=v.id "
        "WHERE v.project_id=p.id AND r.archived=0) as req_count "
        "FROM projects p WHERE p.archived=0 ORDER BY p.updated_at DESC"
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    return rows

@router.post("/projects")
async def create_project(data: ProjectCreate, db: aiosqlite.Connection = Depends(get_db)):
    prefix = data.prefix.strip().upper() if data.prefix else generate_prefix(data.name)
    check = await db.execute("SELECT 1 FROM projects WHERE prefix=? AND archived=0", (prefix,))
    if await check.fetchone():
        raise HTTPException(409, f"prefix '{prefix}' already exists")
    cursor = await db.execute(
        "INSERT INTO projects (name, description, color, prefix) VALUES (?, ?, ?, ?)",
        (data.name, data.description, data.color, prefix)
    )
    await db.commit()
    row = await db.execute("SELECT * FROM projects WHERE id=?", (cursor.lastrowid,))
    return dict(await row.fetchone())

@router.put("/projects/{pid}")
async def update_project(pid: int, data: ProjectUpdate, db: aiosqlite.Connection = Depends(get_db)):
    updates, params = [], []
    for field in ("name", "description", "color", "prefix"):
        val = getattr(data, field)
        if val is not None:
            if field == "prefix":
                val = val.strip().upper()
                check = await db.execute("SELECT 1 FROM projects WHERE prefix=? AND archived=0 AND id!=?", (val, pid))
                if await check.fetchone():
                    raise HTTPException(409, f"prefix '{val}' already exists")
            updates.append(f"{field}=?")
            params.append(val)
    if not updates:
        raise HTTPException(400, "nothing to update")
    updates.append("updated_at=datetime('now','localtime')")
    params.append(pid)
    await db.execute(f"UPDATE projects SET {','.join(updates)} WHERE id=?", params)
    await db.commit()
    row = await db.execute("SELECT * FROM projects WHERE id=?", (pid,))
    return dict(await row.fetchone())

@router.get("/projects/{pid}/versions")
async def list_versions(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT v.*, "
        "(SELECT COUNT(*) FROM requirements WHERE version_id=v.id AND archived=0) as req_count, "
        "(SELECT COUNT(*) FROM requirements WHERE version_id=v.id AND archived=0 AND status='done') as done_count "
        "FROM versions v WHERE v.project_id=? ORDER BY v.position, v.created_at DESC",
        (pid,)
    )
    return [dict(row) for row in await cursor.fetchall()]

@router.post("/versions")
async def create_version(data: VersionCreate, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM versions WHERE project_id=?", (data.project_id,))
    pos = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "INSERT INTO versions (project_id, name, description, position) VALUES (?,?,?,?)",
        (data.project_id, data.name, data.description, pos)
    )
    await db.commit()
    row = await db.execute("SELECT * FROM versions WHERE id=?", (cursor.lastrowid,))
    return dict(await row.fetchone())

@router.put("/versions/{vid}")
async def update_version(vid: int, data: VersionUpdate, db: aiosqlite.Connection = Depends(get_db)):
    updates, params = [], []
    for field in ("name", "description", "status", "git_tag"):
        val = getattr(data, field)
        if val is not None:
            updates.append(f"{field}=?")
            params.append(val)
    if not updates:
        raise HTTPException(400, "nothing to update")
    updates.append("updated_at=datetime('now','localtime')")
    params.append(vid)
    await db.execute(f"UPDATE versions SET {','.join(updates)} WHERE id=?", params)
    await db.commit()
    row = await db.execute("SELECT * FROM versions WHERE id=?", (vid,))
    return dict(await row.fetchone())


@router.delete("/versions/{vid}")
async def delete_version(vid: int, db: aiosqlite.Connection = Depends(get_db)):
    req_cursor = await db.execute("SELECT id FROM requirements WHERE version_id=?", (vid,))
    req_ids = [r[0] for r in await req_cursor.fetchall()]
    if req_ids:
        placeholders = ",".join("?" * len(req_ids))
        # Clean up attachment files from disk
        att_cursor = await db.execute(
            f"SELECT filepath FROM attachments WHERE requirement_id IN ({placeholders})", req_ids
        )
        for row in await att_cursor.fetchall():
            if row[0] and os.path.exists(row[0]):
                os.remove(row[0])
        # Clean up agent_events (no FK CASCADE)
        await db.execute(
            f"DELETE FROM agent_events WHERE requirement_id IN ({placeholders})", req_ids
        )
    await db.execute("DELETE FROM requirements WHERE version_id=?", (vid,))
    await db.execute("DELETE FROM versions WHERE id=?", (vid,))
    await db.commit()
    return {"ok": True}

@router.get("/versions/{vid}/requirements")
async def list_requirements(vid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM requirements WHERE version_id=? AND archived=0 ORDER BY position",
        (vid,)
    )
    return [dict(row) for row in await cursor.fetchall()]

@router.post("/requirements")
async def create_requirement(data: ReqCreate, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    if not data.title or not data.title.strip():
        raise HTTPException(422, "title cannot be empty")
    agent_role = getattr(request.state, "agent_role", None) if hasattr(request, "state") else None
    if agent_role and not registry.check_permission(agent_role, "create", "requirements"):
        raise HTTPException(403, f"Role '{agent_role}' cannot create requirements")
    code = await next_code(db, data.version_id)
    if not code:
        raise HTTPException(404, "version not found")
    description = _normalize_newlines(data.description)
    notes = _normalize_newlines(data.notes)
    cursor = await db.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM requirements WHERE version_id=? AND archived=0",
        (data.version_id,)
    )
    pos = (await cursor.fetchone())[0]
    # 所有卡片强制从 organizing 或 research 入口进入，由 PM 决定流转
    init_status = "research" if data.type == "research" else "organizing"
    cursor = await db.execute(
        "INSERT INTO requirements (version_id,title,description,priority,type,status,assignee,deadline,estimated_hours,notes,queue_reason,agent_timeout,code,position) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (data.version_id, data.title, description, data.priority, data.type, init_status,
         data.assignee, data.deadline, data.estimated_hours, notes, data.queue_reason,
         data.agent_timeout, code, pos)
    )
    await db.commit()
    rid = cursor.lastrowid

    # CEO 的原始想法作为第一条评论
    if data.initial_comment and data.initial_comment.strip():
        await db.execute(
            "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
            (rid, "CEO", data.initial_comment.strip()),
        )
        await db.commit()

    # Emit event for async collaboration
    vcursor = await db.execute("SELECT project_id FROM versions WHERE id=?", (data.version_id,))
    vrow = await vcursor.fetchone()
    if vrow:
        import json
        from web.board_events import broadcast
        broadcast("card_created", {"id": rid, "status": init_status, "code": code})
        await db.execute(
            "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
            (vrow[0], "requirement_created", rid,
             json.dumps({"status": init_status, "priority": data.priority})),
        )
        await db.commit()

    row = await db.execute("SELECT * FROM requirements WHERE id=?", (rid,))
    return dict(await row.fetchone())

@router.get("/requirements/by-code")
@router.get("/requirements/by-code/{code}")
async def get_requirement_by_code(code: str = "", db: aiosqlite.Connection = Depends(get_db)):
    """Look up a requirement by its code (e.g. DO-001)."""
    if not code:
        raise HTTPException(400, "code parameter is required")
    row = await db.execute(
        "SELECT r.*, v.project_id FROM requirements r "
        "JOIN versions v ON r.version_id=v.id WHERE r.code=? AND r.archived=0",
        (code,),
    )
    req = await row.fetchone()
    if not req:
        raise HTTPException(404, f"requirement {code} not found")
    return dict(req)


@router.put("/requirements/{rid}")
async def update_requirement(rid: int, data: ReqUpdate, db: aiosqlite.Connection = Depends(get_db)):
    updates, params = [], []
    for field in ("title", "description", "priority", "type", "status", "assignee", "deadline", "estimated_hours", "actual_hours", "notes", "tags", "queue_reason", "agent_timeout"):
        val = getattr(data, field)
        if val is not None:
            if field in ("description", "notes"):
                val = _normalize_newlines(val)
            updates.append(f"{field}=?")
            params.append(val)
    if not updates:
        raise HTTPException(400, "nothing to update")
    updates.append("updated_at=datetime('now','localtime')")
    params.append(rid)
    await db.execute(f"UPDATE requirements SET {','.join(updates)} WHERE id=?", params)
    await db.commit()
    row = await db.execute("SELECT * FROM requirements WHERE id=?", (rid,))
    return dict(await row.fetchone())

@router.put("/requirements/{rid}/move")
async def move_requirement(rid: int, data: ReqMove, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    valid = ("research", "organizing", "dev", "testing", "done")
    if data.status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")

    # Permission check for agent roles
    agent_role = getattr(request.state, "agent_role", None) if hasattr(request, "state") else None
    cursor = await db.execute("SELECT status, type FROM requirements WHERE id=?", (rid,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, f"requirement {rid} not found")
    old_status = row["status"]
    req_type = row["type"]

    if agent_role:
        if not registry.check_move(agent_role, old_status, data.status):
            allowed = registry.get_permissions(agent_role).can_move if registry.get_permissions(agent_role) else []
            raise HTTPException(
                403,
                f"Role '{agent_role}' cannot move {old_status}->{data.status}, allowed: {allowed}"
            )

    # 进入对应列时自动设 assignee，前端据此判断活跃/排队中
    STATUS_ASSIGNEE_MAP = {
        "research": "Industry",
        "organizing": "PM",
        "dev": "Coach-Dev",
        "testing": "Coach-Review",
    }
    new_assignee = STATUS_ASSIGNEE_MAP.get(data.status, "")
    await db.execute(
        "UPDATE requirements SET status=?, assignee=?, position=?, queue_reason='', "
        "ceo_decision=NULL, updated_at=datetime('now','localtime'), progressed_at=datetime('now','localtime') WHERE id=?",
        (data.status, new_assignee, data.position, rid)
    )

    # Audit trail — identify actual caller
    caller_id = request.headers.get("X-Caller-ID", "").strip()
    actor = agent_role or caller_id or "human"
    await _audit_status_change(db, rid, old_status, data.status, actor, data.reason)

    # Emit status_changed event for async collaboration
    if old_status != data.status:
        import json
        from web.board_events import broadcast
        broadcast("card_moved", {"id": rid, "old_status": old_status, "new_status": data.status})
        vcursor = await db.execute(
            "SELECT v.project_id FROM requirements r JOIN versions v ON r.version_id=v.id WHERE r.id=?",
            (rid,),
        )
        vrow = await vcursor.fetchone()
        if vrow:
            await db.execute(
                "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                (vrow[0], "status_changed", rid,
                 json.dumps({"old_status": old_status, "new_status": data.status})),
            )

    await db.commit()
    row = await db.execute("SELECT * FROM requirements WHERE id=?", (rid,))
    return dict(await row.fetchone())

@router.get("/requirements/{rid}/comments")
async def list_comments(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM comments WHERE requirement_id=? ORDER BY created_at", (rid,)
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _audit_status_change(
    db: aiosqlite.Connection,
    rid: int,
    old_status: str,
    new_status: str,
    actor: str = "human",
    reason: str = "",
):
    """Insert audit comment when status changes."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    role_config = registry.get(actor)
    display = role_config.display_name if role_config else actor
    content = f"**[系统]** 状态变更 `{old_status}` → `{new_status}`\n\n- 操作者: {display}\n- 时间: {timestamp}\n"
    if reason:
        content += f"- 原因: {reason}\n"
    await db.execute(
        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
        (rid, "system", content),
    )

@router.get("/requirements/{rid}/commits")
async def list_commits(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM requirement_commits WHERE requirement_id=? ORDER BY created_at DESC", (rid,)
    )
    return [dict(row) for row in await cursor.fetchall()]


@router.get("/commits/{commit_hash}/diff")
async def get_commit_diff(commit_hash: str, project_id: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    import asyncio
    from core.workspace import get_project_repo_path

    if project_id:
        cursor = await db.execute("SELECT git_remote_url FROM projects WHERE id=?", (project_id,))
        row = await cursor.fetchone()
        git_remote_url = row["git_remote_url"] if row else ""
        repo_path = await get_project_repo_path(project_id, git_remote_url)
    else:
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    proc = await asyncio.create_subprocess_exec(
        "git", "show", "--stat", "--patch", commit_hash,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(404, f"commit not found: {stderr.decode().strip()}")
    return {"hash": commit_hash, "diff": stdout.decode(errors="replace")}

@router.post("/requirements/{rid}/comments")
async def add_comment(rid: int, data: CommentCreate, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
        (rid, data.author, data.content)
    )
    await db.commit()
    row = await db.execute("SELECT * FROM comments WHERE id=?", (cursor.lastrowid,))
    return dict(await row.fetchone())


def _classify_log_layer(msg: str) -> str:
    """Classify a log line by architecture layer: core|web|agent|sched|mcp."""
    # Module name from log format: "asctime [module.name] LEVEL: msg"
    m = re.match(r'^.*\[([a-z_]+(?:\.[a-z_]+)*)\]\s+(?:INFO|WARNING|ERROR|DEBUG|WARN):', msg)
    if m:
        mod = m.group(1)
        if mod.startswith('kh.'):
            seg = mod.split('.')[1] if '.' in mod[3:] else mod[3:]
            if seg in ('core', 'telemetry'):
                return 'core'
            if seg in ('web', 'startup'):
                return 'web'
            if seg in ('agent',):
                return 'agent'
            if seg in ('sched',):
                return 'sched'
            if seg in ('mcp',):
                return 'mcp'
        if mod.startswith('scheduler.'):
            return 'sched'
        if mod.startswith('agents.'):
            return 'agent'
        if mod.startswith('web.'):
            return 'web'
        if mod.startswith('core.'):
            return 'core'
    # Agent role tags
    if '[SCHED]' in msg:
        return 'sched'
    if '[PM]' in msg or '[CHAT]' in msg or '[hermes]' in msg:
        return 'web'
    if 'Coach-Dev' in msg or 'CommentAgent' in msg or '已加载 agent 角色' in msg:
        return 'agent'
    # MCP
    if 'MCP' in msg and ('server' in msg.lower() or 'client' in msg.lower()):
        return 'mcp'
    # DB migrations
    if '迁移' in msg or 'database' in msg.lower():
        return 'core'
    # Everything else (HTTP access, uvicorn lifecycle, startup, etc.) → web
    return 'web'


def _docker_unix_request(method: str, path: str) -> "http.client.HTTPResponse":
    """Send a request to Docker Engine via Unix socket."""
    import http.client, socket as _socket
    class _UnixConn(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect("/var/run/docker.sock")
    conn = _UnixConn("localhost")
    conn.request(method, path)
    return conn.getresponse()


def _get_self_container_id() -> str:
    """Detect own container ID via Docker API compose service label."""
    import json, urllib.parse
    try:
        filters = urllib.parse.quote('{"label":["com.docker.compose.service=web"]}')
        resp = _docker_unix_request("GET", f"/containers/json?filters={filters}")
        if resp.status == 200:
            containers = json.loads(resp.read())
            if containers:
                return containers[0]["Id"]
    except Exception:
        pass
    return ""


_SELF_CONTAINER_ID = _get_self_container_id()


def _fetch_docker_logs(lines: int = 300):
    """Fetch container logs via Docker Unix socket. Returns list of decoded lines."""
    if not _SELF_CONTAINER_ID:
        return ["(cannot detect container ID)"]
    resp = _docker_unix_request("GET", f"/containers/{_SELF_CONTAINER_ID}/logs?tail={lines}&stdout=true&stderr=true&timestamps=true")
    raw = resp.read()
    lines_out = []
    i = 0
    while i + 8 <= len(raw):
        msg_len = int.from_bytes(raw[i+4:i+8], 'big')
        offset = 8
        if offset + msg_len <= len(raw):
            lines_out.append(raw[i+offset:i+offset+msg_len].decode("utf-8", errors="replace").rstrip("\n\r"))
            i += offset + msg_len
        else:
            break
    return lines_out or ["(no log output)"]


@router.get("/dev/logs")
async def dev_logs(lines: int = 200):
    """Return logs from in-memory buffer (current process), fallback to Docker logs."""
    try:
        from main import LOG_BUFFER
        buf = LOG_BUFFER.get_all()
        if buf:
            return {"logs": buf[-lines:], "source": "memory"}
    except Exception:
        pass
    try:
        return {"logs": _fetch_docker_logs(lines), "source": "docker"}
    except Exception as e:
        return {"logs": [f"Error fetching logs: {e}"], "source": "error"}


@router.get("/dev/telemetry")
async def dev_telemetry():
    """LLM call statistics — token usage, latency, call counts by model."""
    from core.telemetry import get_stats
    stats = get_stats()
    if stats is None:
        return {"status": "not_initialized", "stats": {}}
    return {"status": "active", "stats": stats.snapshot()}


LAYER_META = {
    'core':  {'label': 'Core 核心层',   'icon': '⚙️', 'color': '#6366f1', 'desc': '数据库、配置、会话管理'},
    'web':   {'label': 'Web 服务层',    'icon': '🌐', 'color': '#06b6d4', 'desc': 'API、Chat、Hermes、中间件'},
    'agent': {'label': 'Agent 智能体层','icon': '🤖', 'color': '#f59e0b', 'desc': 'Coach-Dev、CommentAgent、Registry'},
    'sched': {'label': 'Scheduler 调度层','icon': '⏱', 'color': '#22c55e','desc': '定时任务、工作流引擎'},
    'mcp':   {'label': 'MCP 协议层',  'icon': '🔌', 'color': '#ec4899','desc': 'MCP Server、KH Client'},
}


@router.get("/dev/logs/layers")
async def dev_logs_layers(lines: int = 300):
    """Return logs grouped by architecture layer — from in-memory buffer or Docker."""
    raw_lines = []
    source = "unknown"
    try:
        from main import LOG_BUFFER
        buf = LOG_BUFFER.get_all()
        if buf:
            raw_lines = buf[-lines:]
            source = "memory"
        else:
            raw_lines = _fetch_docker_logs(lines)
            source = "docker"
    except Exception:
        try:
            raw_lines = _fetch_docker_logs(lines)
            source = "docker"
        except Exception as e:
            return {'layers': {}, 'total': 0, 'error': str(e), 'source': 'error'}

    layers = {}
    for raw in raw_lines:
        # Memory format: "2026-05-26 09:00:00 [name] LEVEL: message"
        # Docker format: "2026-05-26T09:00:00.123456Z [name] LEVEL: message"
        pm = re.match(r'^(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}:\d{2}', raw)
        ts = pm.group(1) + ' ' + raw[pm.end() - 8:pm.end()] if pm else ''
        msg = re.sub(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+Z)?\s+', '', raw)
        layer = _classify_log_layer(msg)
        if layer not in layers:
            layers[layer] = []
        layers[layer].append({'ts': ts, 'msg': msg, 'raw': raw})

    result = {}
    for key, meta in LAYER_META.items():
        lines_in_layer = layers.get(key, [])
        result[key] = {
            'meta': meta,
            'count': len(lines_in_layer),
            'lines': lines_in_layer,
        }
    return {'layers': result, 'total': len(raw_lines), 'source': source}


@router.delete("/projects/{pid}")
async def delete_project(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    # Clean up attachment files from disk
    att_cursor = await db.execute(
        "SELECT filepath FROM attachments WHERE requirement_id IN (SELECT id FROM requirements WHERE version_id IN (SELECT id FROM versions WHERE project_id=?))",
        (pid,),
    )
    for row in await att_cursor.fetchall():
        if row[0] and os.path.exists(row[0]):
            os.remove(row[0])
    await db.execute(
        "DELETE FROM comments WHERE requirement_id IN (SELECT id FROM requirements WHERE version_id IN (SELECT id FROM versions WHERE project_id=?))",
        (pid,),
    )
    await db.execute(
        "DELETE FROM requirement_commits WHERE requirement_id IN (SELECT id FROM requirements WHERE version_id IN (SELECT id FROM versions WHERE project_id=?))",
        (pid,),
    )
    await db.execute(
        "DELETE FROM agent_events WHERE project_id=?", (pid,),
    )
    await db.execute(
        "DELETE FROM agent_sessions WHERE project_id=?", (pid,),
    )
    await db.execute("DELETE FROM requirements WHERE version_id IN (SELECT id FROM versions WHERE project_id=?)", (pid,))
    await db.execute("DELETE FROM versions WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM project_architecture WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()
    return {"ok": True}


@router.delete("/requirements/{rid}")
async def delete_requirement(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    # Cancel running sessions for this card
    await db.execute(
        "UPDATE agent_sessions SET status='completed', output_summary='card_deleted', "
        "completed_at=datetime('now','localtime') "
        "WHERE status IN ('idle','running') AND requirement_id = ?",
        (rid,),
    )
    # Clean up attachment files from disk
    att_cursor = await db.execute("SELECT filepath FROM attachments WHERE requirement_id=?", (rid,))
    for row in await att_cursor.fetchall():
        if row[0] and os.path.exists(row[0]):
            os.remove(row[0])
    await db.execute("DELETE FROM agent_events WHERE requirement_id=?", (rid,))
    await db.execute("DELETE FROM comments WHERE requirement_id=?", (rid,))
    await db.execute("DELETE FROM requirement_commits WHERE requirement_id=?", (rid,))
    await db.execute("DELETE FROM requirements WHERE id=?", (rid,))
    await db.commit()
    return {"ok": True}


@router.put("/requirements/{rid}/archive")
async def archive_requirement(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    await db.execute("UPDATE requirements SET archived=1, updated_at=datetime('now','localtime') WHERE id=?", (rid,))
    await db.commit()
    return {"ok": True}


@router.delete("/comments/{cid}")
async def delete_comment(cid: int, db: aiosqlite.Connection = Depends(get_db)):
    await db.execute("DELETE FROM comments WHERE id=?", (cid,))
    await db.commit()
    return {"ok": True}


@router.get("/comments/{cid}/detail")
async def get_comment_detail(cid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT detail FROM comments WHERE id=?", (cid,))
    row = await cursor.fetchone()
    if not row:
        return {"error": "评论不存在"}
    return {"detail": row["detail"] or ""}


# ==================== 1b. Missing CRUD Endpoints ====================

@router.get("/requirements/by-code/{code}")
async def get_requirement_by_code(code: str, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT r.*, v.project_id FROM requirements r "
        "JOIN versions v ON r.version_id=v.id WHERE r.code=?", (code.upper(),)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, f"requirement {code} not found")
    return dict(row)


@router.get("/tags")
async def list_tags(project_id: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    if not project_id:
        return []
    cursor = await db.execute(
        "SELECT r.tags FROM requirements r JOIN versions v ON r.version_id=v.id "
        "WHERE v.project_id=? AND r.archived=0 AND r.tags != '[]' AND r.tags != ''",
        (project_id,),
    )
    rows = await cursor.fetchall()
    import json as _json
    tag_stats = {}
    for row in rows:
        try:
            tags = _json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        for t in tags:
            if t not in tag_stats:
                tag_stats[t] = {"tag": t, "total": 0, "research": 0, "organizing": 0, "dev": 0, "testing": 0, "done": 0, "description": ""}
            tag_stats[t]["total"] += 1

    cursor = await db.execute(
        "SELECT r.tags, r.status FROM requirements r JOIN versions v ON r.version_id=v.id "
        "WHERE v.project_id=? AND r.archived=0 AND r.tags != '[]' AND r.tags != ''",
        (project_id,),
    )
    for row in await cursor.fetchall():
        try:
            tags = _json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        status = row[1]
        for t in tags:
            if t in tag_stats and status in tag_stats[t]:
                tag_stats[t][status] += 1

    return list(tag_stats.values())


@router.get("/tags/{tag}/requirements")
async def get_tag_requirements(tag: str, project_id: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    import json as _json
    cursor = await db.execute(
        "SELECT r.*, v.name as version_name FROM requirements r "
        "JOIN versions v ON r.version_id=v.id "
        "WHERE v.project_id=? AND r.archived=0 AND r.tags LIKE ?",
        (project_id, f'%{tag}%'),
    )
    reqs = [dict(row) for row in await cursor.fetchall()]
    filtered = []
    for r in reqs:
        try:
            tags = _json.loads(r.get("tags", "[]"))
        except (ValueError, TypeError):
            tags = []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if tag in tags:
            filtered.append(r)

    grouped = {"research": [], "organizing": [], "dev": [], "testing": [], "done": []}
    for r in filtered:
        if r["status"] in grouped:
            grouped[r["status"]].append(r)

    done_count = len(grouped["done"])
    return {
        "tag": tag,
        "description": "",
        "summary": {"total": len(filtered), "done": done_count},
        "grouped": grouped,
    }


@router.get("/projects/{pid}/architecture")
async def get_architecture(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT content FROM project_architecture WHERE project_id=?", (pid,))
    row = await cursor.fetchone()
    return {"content": row[0] if row else ""}


@router.put("/projects/{pid}/architecture")
async def put_architecture(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    from fastapi import Body
    content = body.get("content", "")
    cursor = await db.execute("SELECT 1 FROM project_architecture WHERE project_id=?", (pid,))
    if await cursor.fetchone():
        await db.execute("UPDATE project_architecture SET content=?, updated_at=datetime('now','localtime') WHERE project_id=?", (content, pid))
    else:
        await db.execute("INSERT INTO project_architecture (project_id, content) VALUES (?,?)", (pid, content))
    await db.commit()
    return {"ok": True}


@router.get("/projects/{pid}/product-memory")
async def get_product_memory(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT product_memory FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    return {"content": row[0] if row else ""}


@router.get("/projects/{pid}/wiki")
async def get_wiki_pages(pid: int):
    from core.wiki import list_wiki_pages
    pages = list_wiki_pages(pid)
    return {"pages": pages}


@router.get("/projects/{pid}/wiki/{subdir}/{slug}")
async def get_wiki_page(pid: int, subdir: str, slug: str):
    from core.wiki import read_wiki_page
    page_path = f"{subdir}/{slug}"
    content = read_wiki_page(pid, page_path)
    if not content:
        raise HTTPException(404, f"Wiki page not found: {page_path}")
    return {"content": content, "page": page_path}


@router.put("/projects/{pid}/product-memory")
async def put_product_memory(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    content = body.get("content", "")
    await db.execute("UPDATE projects SET product_memory=?, updated_at=datetime('now','localtime') WHERE id=?", (content, pid))
    await db.commit()
    return {"ok": True}


@router.put("/projects/{pid}/product-memory/section")
async def put_product_memory_section(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    """Update a specific section of the product memory document.

    Body:
      section: "market_intelligence" | "direction_control"
      content: markdown content to insert/update in that section
      sub_section: optional, for market_intelligence: "open_source" | "commercial" | "signal_conflict"
    """
    section = body.get("section", "")
    content = body.get("content", "")
    sub_section = body.get("sub_section", "")

    if section not in ("market_intelligence", "direction_control"):
        raise HTTPException(400, "section must be 'market_intelligence' or 'direction_control'")

    cursor = await db.execute("SELECT product_memory FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    current = row[0] if row else ""

    updated = _update_memory_section(current, section, content, sub_section)
    await db.execute(
        "UPDATE projects SET product_memory=?, updated_at=datetime('now','localtime') WHERE id=?",
        (updated, pid),
    )
    await db.commit()

    # Audit log: insert a system comment in the project's product memory changes
    agent = body.get("agent", "system")
    logger.info("产品记忆已更新: project=%d section=%s sub_section=%s agent=%s", pid, section, sub_section, agent)

    return {"ok": True, "section": section, "sub_section": sub_section}


@router.post("/projects/{pid}/product-memory/decision")
async def append_decision(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    """Append a decision history entry to the direction_control section.

    Body:
      date: date string (defaults to today)
      decision: the decision made
      reason: why it was made
    """
    from datetime import date
    entry_date = body.get("date", date.today().isoformat())
    decision = body.get("decision", "")
    reason = body.get("reason", "")

    if not decision:
        raise HTTPException(400, "decision is required")

    cursor = await db.execute("SELECT product_memory FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    current = row[0] if row else ""

    entry = f"- {entry_date}：{decision}"
    if reason:
        entry += f"（{reason}）"

    updated = _append_to_section(current, "架构决策历史", entry)
    await db.execute(
        "UPDATE projects SET product_memory=?, updated_at=datetime('now','localtime') WHERE id=?",
        (updated, pid),
    )
    await db.commit()
    return {"ok": True}


@router.put("/projects/{pid}/product-memory/target")
async def set_productization_target(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    """Set the productization target level.

    Body:
      level: "L0" | "L1" | "L2" | "L3" | "L4"
    """
    level = body.get("level", "")
    if level not in ("L0", "L1", "L2", "L3", "L4"):
        raise HTTPException(400, "level must be L0-L4")

    cursor = await db.execute("SELECT product_memory FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    current = row[0] if row else ""

    import re
    if re.search(r'productization_target:\s*L\d', current):
        updated = re.sub(r'productization_target:\s*L\d', f'productization_target: {level}', current)
    else:
        updated = current + f"\nproductization_target: {level}\n"

    await db.execute(
        "UPDATE projects SET product_memory=?, updated_at=datetime('now','localtime') WHERE id=?",
        (updated, pid),
    )
    await db.commit()
    return {"ok": True, "level": level}


def _update_memory_section(current: str, section: str, content: str, sub_section: str = "") -> str:
    """Update a specific section of the product memory markdown document."""
    import re

    if section == "market_intelligence":
        section_header = "## 一、市场分析（Market Intelligence）"
        alt_header = "## 一、市场分析"
    else:
        section_header = "## 二、方向把控（Direction Control）"
        alt_header = "## 二、方向把控"

    # Find the target section boundaries
    section_pattern = re.compile(
        rf"({re.escape(section_header)}|{re.escape(alt_header)})"
        r"(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    match = section_pattern.search(current)

    if not match:
        # Section doesn't exist, append it
        if not current.endswith("\n"):
            current += "\n"
        current += f"\n{section_header}\n\n{content}\n"
        return current

    # Section exists, check for sub-section
    if sub_section:
        sub_map = {
            "open_source": "### 开源视角",
            "commercial": "### 商业视角",
            "signal_conflict": "### 信号冲突记录",
        }
        sub_header = sub_map.get(sub_section, "")
        if sub_header:
            sub_pattern = re.compile(
                rf"({re.escape(sub_header)})"
                r"(.*?)(?=\n### |\n## |\Z)",
                re.DOTALL,
            )
            sub_match = sub_pattern.search(match.group(0))
            if sub_match:
                # Replace sub-section content
                old = sub_match.group(0)
                new = f"{sub_header}\n\n{content}"
                full = match.group(0).replace(old, new)
            else:
                # Append sub-section
                full = match.group(0).rstrip() + f"\n\n{sub_header}\n\n{content}\n"
            current = current[:match.start()] + match.expand(rf"\1{full.split(match.group(1),1)[1]}") + current[match.end():]
            return current

    # No sub-section, replace entire section content
    # Keep the header, replace everything after it until the next section or end
    header = match.group(1)
    rest = match.group(2)
    new_section = f"{header}\n\n{content}\n"
    return current[:match.start()] + new_section + current[match.end():]


def _append_to_section(current: str, sub_header: str, entry: str) -> str:
    """Append an entry to a subsection within the product memory."""
    import re
    pattern = re.compile(
        rf"(### {re.escape(sub_header)}.*?)(?=\n### |\n## |\Z)",
        re.DOTALL,
    )
    match = pattern.search(current)
    if match:
        section_text = match.group(1)
        updated_section = section_text.rstrip() + f"\n{entry}\n"
        return current[:match.start()] + updated_section + current[match.end():]
    else:
        # No such subsection, append
        return current.rstrip() + f"\n\n### {sub_header}\n\n{entry}\n"


@router.get("/skill-template")
async def get_skill_template():
    template_path = os.path.join(os.path.dirname(__file__), "skill_template.md")
    if os.path.exists(template_path):
        with open(template_path, "r") as f:
            return {"content": f.read()}
    return {"content": "# 产品顾问 Skill 模板\n\n暂无模板内容，请联系管理员配置。"}


@router.post("/requirements/{rid}/attachments")
async def upload_attachment(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    from fastapi import UploadFile, File, Request
    return {"error": "attachment upload not yet implemented"}


@router.delete("/attachments/{aid}")
async def delete_attachment(aid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT filepath FROM attachments WHERE id=?", (aid,))
    row = await cursor.fetchone()
    if row and row[0] and os.path.exists(row[0]):
        os.remove(row[0])
    await db.execute("DELETE FROM attachments WHERE id=?", (aid,))
    await db.commit()
    return {"ok": True}


@router.get("/attachments/{aid}/preview")
async def preview_attachment(aid: int, db: aiosqlite.Connection = Depends(get_db)):
    from fastapi.responses import FileResponse
    cursor = await db.execute("SELECT filepath, filename, content_type FROM attachments WHERE id=?", (aid,))
    row = await cursor.fetchone()
    if not row or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(404, "attachment not found")
    return FileResponse(row[0], filename=row[1], media_type=row[2] or "application/octet-stream")


@router.get("/attachments/{aid}/download")
async def download_attachment(aid: int, db: aiosqlite.Connection = Depends(get_db)):
    from fastapi.responses import FileResponse
    cursor = await db.execute("SELECT filepath, filename, content_type FROM attachments WHERE id=?", (aid,))
    row = await cursor.fetchone()
    if not row or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(404, "attachment not found")
    return FileResponse(row[0], filename=row[1], media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{row[1]}"'})

# ==================== 2. Scheduler Control API ====================

def _get_scheduler():
    from main import scheduler
    return scheduler

@router.get("/scheduler/status")
async def scheduler_status():
    return _get_scheduler().status

@router.post("/scheduler/pause")
async def scheduler_pause():
    _get_scheduler().pause()
    return {"ok": True, "mode": "paused"}

@router.post("/scheduler/resume")
async def scheduler_resume():
    _get_scheduler().resume()
    return {"ok": True, "mode": "running"}

@router.post("/scheduler/trigger/{task_type}")
async def trigger_task(task_type: str):
    return {
        "triggered": task_type,
        "status": "accepted",
        "hint": "Manual trigger — will execute on next tick"
    }


# ==================== 2c. Card Lifecycle Logs ====================

@router.get("/requirements/{rid}/logs")
async def requirement_logs(rid: int, limit: int = 100, db: aiosqlite.Connection = Depends(get_db)):
    """返回卡片的生命周期日志（持久化，跨重启保留）。"""
    cursor = await db.execute(
        "SELECT id, level, source, message, created_at FROM requirement_logs "
        "WHERE requirement_id=? ORDER BY created_at ASC LIMIT ?",
        (rid, limit),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    return {"logs": rows, "total": len(rows)}


# ==================== 2b. Card Debug Endpoint (KH-107) ====================

@router.get("/cards/{code}/debug")
async def card_debug(code: str, db: aiosqlite.Connection = Depends(get_db)):
    """Aggregate all runtime info for a card by its code (e.g. KH-086)."""
    import json as _json
    from core.workspace import WORKSPACE_BASE

    cursor = await db.execute(
        "SELECT r.*, v.project_id FROM requirements r "
        "JOIN versions v ON r.version_id=v.id WHERE r.code=? AND r.archived=0",
        (code.upper(),),
    )
    card = await cursor.fetchone()
    if not card:
        raise HTTPException(404, f"card {code} not found")
    card = dict(card)
    req_id = card["id"]
    project_id = card["project_id"]

    workspace_path = os.path.join(WORKSPACE_BASE, f"project_{project_id}")

    cursor = await db.execute(
        "SELECT id, agent_role, status, trigger_type, error_message, retry_count, "
        "input_tokens, output_tokens, total_tokens, started_at, completed_at "
        "FROM agent_sessions WHERE requirement_id = ? ORDER BY created_at DESC LIMIT 20",
        (req_id,),
    )
    sessions = [dict(row) for row in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT author, content, created_at FROM comments "
        "WHERE requirement_id=? ORDER BY created_at DESC LIMIT 10",
        (req_id,),
    )
    recent_comments = [dict(row) for row in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT commit_hash, message, committed_at FROM requirement_commits "
        "WHERE requirement_id=? ORDER BY created_at DESC",
        (req_id,),
    )
    commits = [dict(row) for row in await cursor.fetchall()]

    total_attempts = len(sessions)
    last_error = ""
    for s in sessions:
        if s["error_message"]:
            last_error = s["error_message"]
            break

    return {
        "card": {
            "code": card["code"],
            "title": card["title"],
            "status": card["status"],
            "assignee": card["assignee"],
            "priority": card["priority"],
            "type": card.get("type", "dev"),
        },
        "workspace_path": workspace_path,
        "sessions": sessions,
        "recent_comments": recent_comments,
        "commits": commits,
        "stats": {
            "total_attempts": total_attempts,
            "last_error": last_error,
        },
    }


# ==================== 2c. Token Stats Endpoint (KH-108) ====================

@router.get("/stats/tokens")
async def token_stats(project_id: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    """Token consumption statistics — by role and time period."""
    base_where = "WHERE 1=1"
    params = []
    if project_id:
        base_where += " AND project_id=?"
        params.append(project_id)

    # By role
    cursor = await db.execute(
        f"SELECT agent_role, "
        f"SUM(input_tokens) as input_tokens, "
        f"SUM(output_tokens) as output_tokens, "
        f"SUM(total_tokens) as total_tokens, "
        f"COUNT(*) as session_count "
        f"FROM agent_sessions {base_where} AND status='completed' "
        f"GROUP BY agent_role",
        params,
    )
    by_role = [dict(row) for row in await cursor.fetchall()]

    # Today
    cursor = await db.execute(
        f"SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, "
        f"SUM(total_tokens) as total_tokens "
        f"FROM agent_sessions {base_where} AND status='completed' "
        f"AND started_at >= date('now', 'localtime')",
        params,
    )
    today = dict(await cursor.fetchone())

    # This week
    cursor = await db.execute(
        f"SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, "
        f"SUM(total_tokens) as total_tokens "
        f"FROM agent_sessions {base_where} AND status='completed' "
        f"AND started_at >= date('now', 'localtime', '-7 days')",
        params,
    )
    this_week = dict(await cursor.fetchone())

    # Total
    cursor = await db.execute(
        f"SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, "
        f"SUM(total_tokens) as total_tokens "
        f"FROM agent_sessions {base_where} AND status='completed'",
        params,
    )
    total = dict(await cursor.fetchone())

    # Chat token usage (from chat_tasks table)
    chat_where = "WHERE status='completed'"
    chat_params = []
    if project_id:
        chat_where += " AND project_id=?"
        chat_params.append(project_id)

    cursor = await db.execute(
        f"SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, "
        f"SUM(total_tokens) as total_tokens, COUNT(*) as session_count "
        f"FROM chat_tasks {chat_where}",
        chat_params,
    )
    chat_row = dict(await cursor.fetchone())
    if chat_row.get("session_count"):
        by_role.append({
            "agent_role": "chat",
            "input_tokens": chat_row["input_tokens"] or 0,
            "output_tokens": chat_row["output_tokens"] or 0,
            "total_tokens": chat_row["total_tokens"] or 0,
            "session_count": chat_row["session_count"],
        })

    # Add chat tokens to time-based totals
    for period, time_filter in [
        (today, "AND created_at >= date('now', 'localtime')"),
        (this_week, "AND created_at >= date('now', 'localtime', '-7 days')"),
        (total, ""),
    ]:
        cursor = await db.execute(
            f"SELECT COALESCE(SUM(input_tokens),0) as ci, COALESCE(SUM(output_tokens),0) as co, "
            f"COALESCE(SUM(total_tokens),0) as ct FROM chat_tasks {chat_where} {time_filter}",
            chat_params,
        )
        cr = dict(await cursor.fetchone())
        period["input_tokens"] = (period["input_tokens"] or 0) + cr["ci"]
        period["output_tokens"] = (period["output_tokens"] or 0) + cr["co"]
        period["total_tokens"] = (period["total_tokens"] or 0) + cr["ct"]

    return {
        "by_role": by_role,
        "today": today,
        "this_week": this_week,
        "total": total,
    }


@router.get("/scheduler/state")
async def scheduler_state(db: aiosqlite.Connection = Depends(get_db)):
    """Aggregated scheduler snapshot — one request for full runtime state."""
    import json as _json
    import time as _time
    from datetime import datetime
    from core.session_manager import DEFAULT_STALL_TIMEOUT

    scheduler = _get_scheduler()
    sched = scheduler.status
    heartbeats = scheduler.session_manager._heartbeats
    now_mono = _time.monotonic()

    # Running sessions
    cursor = await db.execute(
        "SELECT id, agent_role, input_context, started_at, retry_count "
        "FROM agent_sessions WHERE status='running' ORDER BY started_at"
    )
    running_rows = [dict(row) for row in await cursor.fetchall()]

    now = datetime.now()
    running = []
    stale = []
    for r in running_rows:
        card_code = ""
        try:
            ctx = _json.loads(r["input_context"] or "{}")
            card_code = ctx.get("code", "")
        except (ValueError, TypeError):
            pass
        elapsed = 0
        if r["started_at"]:
            try:
                started = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
                elapsed = int((now - started).total_seconds())
            except ValueError:
                pass

        # Heartbeat / stall info — only treat as truly running if heartbeat exists
        sid = r["id"]
        last_beat = heartbeats.get(sid)
        if last_beat is not None:
            silent_seconds = int(now_mono - last_beat)
            running.append({
                "session_id": sid,
                "card_code": card_code,
                "agent_role": r["agent_role"],
                "started_at": r["started_at"],
                "elapsed_seconds": elapsed,
                "silent_seconds": silent_seconds,
                "stall_timeout": DEFAULT_STALL_TIMEOUT,
                "retry_count": r["retry_count"],
            })
        else:
            # No heartbeat → stale session (process gone, DB not cleaned up)
            stale.append({
                "session_id": sid,
                "card_code": card_code,
                "agent_role": r["agent_role"],
                "started_at": r["started_at"],
                "elapsed_seconds": elapsed,
            })

    # Blocked sessions
    cursor = await db.execute(
        "SELECT id, agent_role, input_context, error_message, completed_at, retry_count "
        "FROM agent_sessions WHERE status='blocked' ORDER BY completed_at DESC LIMIT 10"
    )
    blocked_rows = [dict(row) for row in await cursor.fetchall()]

    blocked = []
    for r in blocked_rows:
        card_code = ""
        try:
            ctx = _json.loads(r["input_context"] or "{}")
            card_code = ctx.get("code", "")
        except (ValueError, TypeError):
            pass
        blocked.append({
            "session_id": r["id"],
            "card_code": card_code,
            "agent_role": r["agent_role"],
            "error": r["error_message"],
            "failed_at": r["completed_at"],
            "retry_count": r["retry_count"],
        })

    # Pending actionable cards (dev cards waiting for dispatch)
    cursor = await db.execute(
        "SELECT r.id, r.code, r.title, r.status, r.priority "
        "FROM requirements r "
        "JOIN versions v ON r.version_id = v.id "
        "WHERE r.status = 'dev' AND r.type = 'dev' AND r.archived = 0 "
        "ORDER BY r.priority, r.position"
    )
    pending_rows = [dict(row) for row in await cursor.fetchall()]

    pending = [{
        "card_id": r["id"],
        "card_code": r["code"],
        "title": r["title"],
        "status": r["status"],
        "priority": r["priority"],
    } for r in pending_rows]

    from core.telemetry import get_stats
    telemetry_stats = {}
    ts = get_stats()
    if ts:
        telemetry_stats = ts.snapshot()

    return {
        "generated_at": now.isoformat(),
        "scheduler": {
            "mode": sched["mode"],
            "tick_count": sched["tick_count"],
            "poll_interval": sched["poll_interval"],
            "started_at": sched["started_at"],
        },
        "counts": {
            "running": len(running),
            "stale": len(stale),
            "blocked": len(blocked),
            "pending": len(pending),
        },
        "running": running,
        "stale": stale,
        "blocked": blocked,
        "pending": pending,
        "telemetry": telemetry_stats,
    }


# ==================== 3. AI Activity API ====================

@router.get("/agents/sessions")
async def list_agent_sessions(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM agent_sessions ORDER BY created_at DESC LIMIT 50"
    )
    return [dict(row) for row in await cursor.fetchall()]

@router.get("/agents/status")
async def agents_status(db: aiosqlite.Connection = Depends(get_db)):
    ROLE_ORDER = ["industry", "pm", "coach_dev", "coach_review"]
    result = {}
    for role_name, role_config in registry.all_roles().items():
        cursor = await db.execute(
            "SELECT status, started_at, completed_at, output_summary FROM agent_sessions "
            "WHERE agent_role=? ORDER BY created_at DESC LIMIT 1",
            (role_name,),
        )
        row = await cursor.fetchone()

        # Count 24h activity
        cursor2 = await db.execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE agent_role=? AND status='completed' "
            "AND completed_at > datetime('now', '-24 hours', 'localtime')",
            (role_name,),
        )
        count_24h = (await cursor2.fetchone())[0]

        # Get latest comment by this role
        cursor3 = await db.execute(
            "SELECT content, created_at FROM comments WHERE author=? ORDER BY created_at DESC LIMIT 1",
            (role_config.display_name,),
        )
        last_comment = await cursor3.fetchone()

        session_data = dict(row) if row else {}
        result[role_name] = {
            "display_name": role_config.display_name,
            "icon": role_config.icon,
            "avatar": f"/static/avatars/{role_name}_avatar.png",
            "color": role_config.color,
            "description": role_config.description,
            "model": f"{role_config.model.provider}/{role_config.model.name}",
            "allowed_tools": role_config.allowed_tools,
            "triggers": [t.event for t in role_config.triggers],
            "permissions": {
                "can_move": role_config.permissions.can_move,
            },
            "status": session_data.get("status", "idle"),
            "last_run": session_data.get("started_at"),
            "completed_at": session_data.get("completed_at"),
            "activity_24h": count_24h,
            "last_comment": dict(last_comment) if last_comment else None,
        }
    ordered = {k: result[k] for k in ROLE_ORDER if k in result}
    for k in result:
        if k not in ordered:
            ordered[k] = result[k]
    return {"agents": ordered}


# ==================== CEO Decision Endpoints ====================

DECISION_MARKERS = ("[调研充分]", "[READY]", "[需要补充]", "[NEED_MORE]",
                    "推进开发", "进入开发", "移回调研", "退回调研", "补充调研", "继续调研")


@router.get("/decisions/pending")
async def list_pending_decisions(project_id: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    """List cards waiting for CEO decision — unified via ceo_decision field."""
    import json as _json

    query = """
        SELECT r.id, r.code, r.title, r.priority, r.status, r.description,
               r.updated_at, r.type, r.ceo_decision, v.project_id,
               p.name as project_name, p.prefix
        FROM requirements r
        JOIN versions v ON r.version_id = v.id
        JOIN projects p ON v.project_id = p.id
        WHERE r.ceo_decision IS NOT NULL AND r.archived = 0
    """
    params = []
    if project_id:
        query += " AND v.project_id = ?"
        params.append(project_id)
    query += " ORDER BY r.updated_at DESC"

    cursor = await db.execute(query, params)
    cards = [dict(row) for row in await cursor.fetchall()]

    results = []
    for card in cards:
        decision_data = {}
        try:
            decision_data = _json.loads(card["ceo_decision"])
        except (ValueError, TypeError):
            pass

        asking_role = decision_data.get("role", "pm")
        author_map = {
            "industry": ("行业顾问", "Industry"),
            "pm": ("产品经理", "PM"),
            "coach_review": ("Coach-Review", "Coach-QA"),
            "coach_dev": ("Coach-Dev",),
        }
        authors = author_map.get(asking_role, ("产品经理", "PM"))
        placeholders = ",".join("?" * len(authors))
        cursor2 = await db.execute(
            f"SELECT content, created_at FROM comments "
            f"WHERE requirement_id=? AND author IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT 1",
            (card["id"], *authors),
        )
        last_comment = await cursor2.fetchone()

        cursor3 = await db.execute(
            "SELECT COUNT(*) FROM comments WHERE requirement_id=? AND author='行业顾问'",
            (card["id"],),
        )
        research_count = (await cursor3.fetchone())[0]

        results.append({
            "id": card["id"],
            "code": card["code"],
            "title": card["title"],
            "priority": card["priority"],
            "status": card["status"],
            "type": card["type"],
            "project_id": card["project_id"],
            "project_name": card["project_name"],
            "asking_role": asking_role,
            "reason": decision_data.get("reason", ""),
            "message": decision_data.get("message", ""),
            "questions": decision_data.get("questions", [decision_data.get("message", "")] if decision_data.get("message") else []),
            "actions": decision_data.get("actions", []),
            "pm_summary": (
                decision_data.get("message", "")
                if decision_data.get("reason") == "agent_d"
                else (last_comment["content"][:500] if last_comment else decision_data.get("message", ""))
            ),
            "research_rounds": research_count,
            "waiting_since": decision_data.get("since", card["updated_at"]),
        })

    return {"decisions": results}

class CEODecisionInput(BaseModel):
    decision: str  # "reply_to_role" | "retry"
    comment: str = ""
    asking_role: str = ""


@router.post("/decisions/{rid}/submit")
async def submit_ceo_decision(rid: int, data: CEODecisionInput, db: aiosqlite.Connection = Depends(get_db)):
    """CEO submits a decision on a card awaiting CEO input."""
    import json

    cursor = await db.execute("SELECT status FROM requirements WHERE id=?", (rid,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "requirement not found")

    # Clear ceo_decision — CEO has responded
    await db.execute(
        "UPDATE requirements SET ceo_decision=NULL, queue_reason='', "
        "updated_at=datetime('now','localtime'), progressed_at=datetime('now','localtime') WHERE id=?",
        (rid,),
    )

    # CEO can only communicate, never move cards
    ALLOWED_DECISIONS = ("reply_to_role", "retry")
    if data.decision not in ALLOWED_DECISIONS:
        raise HTTPException(400, f"CEO 王权对话仅允许沟通操作: {ALLOWED_DECISIONS}")

    ROLE_WORK_STATUS = {
        "industry": "research",
        "pm": "organizing",
        "coach_dev": "dev",
        "coach_review": "testing",
    }
    if data.decision == "reply_to_role":
        new_status = ROLE_WORK_STATUS.get(data.asking_role, "organizing")
    else:
        new_status = ""

    # Record CEO comment
    comment_text = data.comment.strip() if data.comment else ""
    if not comment_text:
        if data.decision == "reply_to_role":
            comment_text = "已回复，请按反馈继续。"
        elif data.decision == "retry":
            comment_text = "请重新执行。"

    await db.execute(
        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
        (rid, "CEO", comment_text),
    )

    old_status = row["status"]

    # Archive is no longer available via CEO decision dialog
    # (use the card's archive button directly)

    # Move card only for reply_to_role when status differs (shouldn't happen, but safe)
    if new_status and new_status != old_status:
        await db.execute(
            "UPDATE requirements SET status=?, progressed_at=datetime('now','localtime'), "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (new_status, rid),
        )

    # Emit event: always for reply_to_role/retry (agent needs to re-engage),
    # or when status actually changed
    if new_status != old_status or data.decision in ("reply_to_role", "retry"):
        vcursor = await db.execute(
            "SELECT v.project_id FROM requirements r JOIN versions v ON r.version_id=v.id WHERE r.id=?",
            (rid,),
        )
        vrow = await vcursor.fetchone()
        if vrow:
            context = {"old_status": old_status, "new_status": new_status or old_status, "moved_by": "CEO"}
            if data.decision == "reply_to_role":
                context["decision"] = "reply_to_role"
                context["asking_role"] = data.asking_role or "pm"
            await db.execute(
                "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                (vrow[0], "status_changed" if data.decision != "reply_to_role" else "ceo_replied", rid,
                 json.dumps(context)),
            )

    await db.commit()

    from web.board_events import broadcast
    if new_status and new_status != old_status:
        broadcast("card_moved", {"id": rid, "old_status": old_status, "new_status": new_status})
    else:
        broadcast("card_updated", {"id": rid, "action": "ceo_decision"})

    vcursor = await db.execute(
        "SELECT v.id as version_id FROM requirements r JOIN versions v ON r.version_id=v.id WHERE r.id=?",
        (rid,),
    )
    vrow = await vcursor.fetchone()
    return {"ok": True, "new_status": new_status or old_status, "version_id": vrow["version_id"] if vrow else None}


# ==================== 4. Config Reload ====================


@router.post("/config/reload")
async def reload_config():
    """Re-read .env and re-sync hermes config without restarting the server."""
    from dotenv import load_dotenv
    # Re-read .env with override
    load_dotenv(override=True)
    logger.info("已重载 .env 文件")

    try:
        from web.hermes_chat import ensure_hermes_config
        await ensure_hermes_config()
        logger.info("已重新同步 hermes 配置")
    except Exception as e:
        logger.error("同步 hermes 配置失败: %s", e)
        return {"ok": True, "dotenv": "reloaded", "hermes_sync": f"failed: {e}"}

    return {"ok": True, "dotenv": "reloaded", "hermes_sync": "ok"}
