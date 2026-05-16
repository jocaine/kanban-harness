import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import os
import aiosqlite

logger = logging.getLogger(__name__)

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
    status: str = "pending"
    assignee: str = ""
    deadline: str = ""
    estimated_hours: float = 0
    notes: str = ""

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

class ReqMove(BaseModel):
    status: str
    position: int = 0
    reason: str = ""

class CommentCreate(BaseModel):
    author: str = ""
    content: str

# ==================== 1. Kanban Data API ====================

@router.get("/projects")
async def list_projects(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT id, name, description, color, prefix, archived, created_at, updated_at, "
        "git_repo_path, git_remote_url, "
        "(advisor_skill != '' AND advisor_skill IS NOT NULL) as has_advisor_skill, "
        "(SELECT COUNT(*) FROM versions WHERE project_id=p.id) as version_count, "
        "(SELECT COUNT(*) FROM requirements r JOIN versions v ON r.version_id=v.id "
        "WHERE v.project_id=p.id AND r.archived=0) as req_count "
        "FROM projects p WHERE p.archived=0 ORDER BY p.updated_at DESC"
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["has_advisor_skill"] = bool(row["has_advisor_skill"])
    return rows

@router.post("/projects")
async def create_project(data: ProjectCreate, db: aiosqlite.Connection = Depends(get_db)):
    prefix = data.prefix.strip().upper() if data.prefix else generate_prefix(data.name)
    check = await db.execute("SELECT 1 FROM projects WHERE prefix=?", (prefix,))
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
                check = await db.execute("SELECT 1 FROM projects WHERE prefix=? AND id!=?", (val, pid))
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
    # 调研类型卡片创建时自动设为 research 状态
    init_status = "research" if data.type == "research" else (data.status or "pending")
    cursor = await db.execute(
        "INSERT INTO requirements (version_id,title,description,priority,type,status,assignee,deadline,estimated_hours,notes,code,position) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (data.version_id, data.title, description, data.priority, data.type, init_status,
         data.assignee, data.deadline, data.estimated_hours, notes, code, pos)
    )
    await db.commit()
    rid = cursor.lastrowid

    # Emit event for async collaboration
    vcursor = await db.execute("SELECT project_id FROM versions WHERE id=?", (data.version_id,))
    vrow = await vcursor.fetchone()
    if vrow:
        import json
        await db.execute(
            "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
            (vrow[0], "requirement_created", rid,
             json.dumps({"status": init_status, "priority": data.priority})),
        )
        await db.commit()

    row = await db.execute("SELECT * FROM requirements WHERE id=?", (rid,))
    return dict(await row.fetchone())

@router.put("/requirements/{rid}")
async def update_requirement(rid: int, data: ReqUpdate, db: aiosqlite.Connection = Depends(get_db)):
    updates, params = [], []
    for field in ("title", "description", "priority", "type", "status", "assignee", "deadline", "estimated_hours", "actual_hours", "notes", "tags"):
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
    valid = ("research", "pending", "dev", "testing", "done", "blocked")
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

    # Type-based state machine rules
    if req_type == "dev" and old_status == "pending" and data.status == "done":
        raise HTTPException(400, "开发需求不能直接从 pending 移到 done，须经过 dev→testing→done 流程")
    if req_type == "research" and data.status in ("dev", "testing"):
        raise HTTPException(400, "调研需求不能移到 dev/testing 列，审计通过后直接到 done")

    if agent_role:
        if not registry.check_move(agent_role, old_status, data.status):
            allowed = registry.get_permissions(agent_role).can_move if registry.get_permissions(agent_role) else []
            raise HTTPException(
                403,
                f"Role '{agent_role}' cannot move {old_status}->{data.status}, allowed: {allowed}"
            )
        # Dev moving to pending/blocked MUST provide a reason
        if agent_role == "coach_dev" and data.status in ("pending", "blocked") and not data.reason:
            raise HTTPException(
                400,
                f"Role 'coach_dev' must provide a reason when moving to '{data.status}'"
            )
    else:
        # 人类操作必须提供移动原因
        if not data.reason.strip():
            raise HTTPException(400, "移动卡片必须提供原因说明")

    await db.execute(
        "UPDATE requirements SET status=?, position=?, updated_at=datetime('now','localtime') WHERE id=?",
        (data.status, data.position, rid)
    )

    # Audit trail — identify actual caller
    caller_id = request.headers.get("X-Caller-ID", "").strip()
    actor = agent_role or caller_id or "human"
    await _audit_status_change(db, rid, old_status, data.status, actor, data.reason)

    # Emit status_changed event for async collaboration
    if old_status != data.status:
        import json
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
    from core.config import get_project_repo_path

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


@router.delete("/projects/{pid}")
async def delete_project(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    await db.execute("UPDATE projects SET archived=1, updated_at=datetime('now','localtime') WHERE id=?", (pid,))
    await db.commit()
    return {"ok": True}


@router.delete("/requirements/{rid}")
async def delete_requirement(rid: int, db: aiosqlite.Connection = Depends(get_db)):
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
                tag_stats[t] = {"tag": t, "total": 0, "research": 0, "pending": 0, "dev": 0, "testing": 0, "done": 0, "description": ""}
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

    grouped = {"research": [], "pending": [], "dev": [], "testing": [], "done": []}
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


@router.get("/projects/{pid}/advisor-skill")
async def get_advisor_skill(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT advisor_skill FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    return {"content": row[0] if row else ""}


@router.put("/projects/{pid}/advisor-skill")
async def put_advisor_skill(pid: int, db: aiosqlite.Connection = Depends(get_db), body: dict = {}):
    content = body.get("content", "")
    await db.execute("UPDATE projects SET advisor_skill=?, updated_at=datetime('now','localtime') WHERE id=?", (content, pid))
    await db.commit()
    return {"ok": True}


@router.get("/projects/{pid}/product-memory")
async def get_product_memory(pid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT product_memory FROM projects WHERE id=?", (pid,))
    row = await cursor.fetchone()
    return {"content": row[0] if row else ""}


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
    logger.info("Product memory updated: project=%d section=%s sub_section=%s agent=%s", pid, section, sub_section, agent)

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
    """List cards waiting for CEO/human decision.

    Includes:
    - Cards in pending queue (from any role, e.g. [转给PM])
    - Research cards where Industry marked [需要补充] (stay in research, CEO decides)
    """
    import json

    query = """
        SELECT r.id, r.code, r.title, r.priority, r.status, r.description,
               r.updated_at, r.type, v.project_id, p.name as project_name, p.prefix
        FROM requirements r
        JOIN versions v ON r.version_id = v.id
        JOIN projects p ON v.project_id = p.id
        WHERE r.archived = 0
        AND (
            r.status = 'pending'
            OR (r.status = 'research' AND r.id IN (
                SELECT c.requirement_id FROM comments c
                WHERE c.author IN ('行业顾问', 'Industry')
                AND c.content LIKE '%[需要补充]%'
                AND c.id = (
                    SELECT MAX(c2.id) FROM comments c2
                    WHERE c2.requirement_id = c.requirement_id
                )
            ))
        )
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
        # Get the last agent comment — whichever role put it in pending
        cursor2 = await db.execute(
            "SELECT content, author, created_at FROM comments "
            "WHERE requirement_id=? AND author IN ('产品经理', 'PM', '行业顾问', 'Industry', 'Coach-Dev', 'Coach-QA') "
            "ORDER BY created_at DESC LIMIT 1",
            (card["id"],),
        )
        last_comment = await cursor2.fetchone()
        if not last_comment:
            continue

        comment_text = last_comment["content"] or ""

        # Map author to asking_role
        author = last_comment["author"]
        ROLE_MAP = {
            "产品经理": "pm", "PM": "pm",
            "行业顾问": "industry", "Industry": "industry",
            "Coach-Dev": "coach_dev", "Coach-QA": "coach_review",
        }
        asking_role = ROLE_MAP.get(author, "pm")

        # Count research rounds
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
            "pm_summary": comment_text[:500],
            "research_rounds": research_count,
            "waiting_since": last_comment["created_at"],
        })

    return {"decisions": results}


class CEODecisionInput(BaseModel):
    decision: str  # "approve_dev" | "request_more_research" | "reply_to_role" | "custom"
    comment: str = ""
    asking_role: str = ""


@router.post("/decisions/{rid}/submit")
async def submit_ceo_decision(rid: int, data: CEODecisionInput, db: aiosqlite.Connection = Depends(get_db)):
    """CEO submits a decision on a pending card."""
    import json

    cursor = await db.execute("SELECT status, type FROM requirements WHERE id=?", (rid,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "requirement not found")
    if row["status"] not in ("pending", "research"):
        raise HTTPException(400, "card must be in pending or research status")
    req_type = row["type"]

    # Determine target status
    ROLE_WORK_STATUS = {
        "industry": "research",
        "pm": "pending",
        "coach_dev": "dev",
        "coach_review": "testing",
    }
    if data.decision == "approve_dev":
        new_status = "done" if req_type == "research" else "dev"
    elif data.decision == "request_more_research":
        new_status = "research"
    elif data.decision == "reply_to_role":
        # Return card to the asking role's working column
        new_status = ROLE_WORK_STATUS.get(data.asking_role, "pending")
    else:
        new_status = ""

    # Record CEO comment
    comment_text = data.comment.strip() if data.comment else ""
    if not comment_text:
        if data.decision == "approve_dev":
            comment_text = "调研结果已确认，完成。" if req_type == "research" else "批准进入开发阶段。"
        elif data.decision == "request_more_research":
            comment_text = "需要补充更多调研材料。"
        elif data.decision == "reply_to_role":
            comment_text = "已回复，请按反馈继续。"

    await db.execute(
        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
        (rid, "CEO", comment_text),
    )

    old_status = row["status"]

    # Move card if decision implies a status change
    if new_status:
        await db.execute(
            "UPDATE requirements SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
            (new_status, rid),
        )

    # Emit event: always for reply_to_role (agent needs to see CEO's reply),
    # or when status actually changed
    if new_status != old_status or data.decision == "reply_to_role":
        vcursor = await db.execute(
            "SELECT v.project_id FROM requirements r JOIN versions v ON r.version_id=v.id WHERE r.id=?",
            (rid,),
        )
        vrow = await vcursor.fetchone()
        if vrow:
            context = {"old_status": old_status, "new_status": new_status or old_status, "moved_by": "CEO"}
            if data.decision == "reply_to_role":
                context["decision"] = "reply_to_role"
            await db.execute(
                "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                (vrow[0], "status_changed" if data.decision != "reply_to_role" else "ceo_replied", rid,
                 json.dumps(context)),
            )

    await db.commit()
    return {"ok": True, "new_status": new_status or "pending"}
