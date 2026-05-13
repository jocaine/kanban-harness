from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import os
import aiosqlite

from core.database import get_db, next_code, generate_prefix
from agents.registry import registry
from web.chat import router as chat_router

router = APIRouter()
router.include_router(chat_router)

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
    status: str = "pending"
    assignee: str = ""
    deadline: str = ""
    estimated_hours: float = 0
    notes: str = ""

class ReqUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    deadline: Optional[str] = None
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    notes: Optional[str] = None

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
    cursor = await db.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM requirements WHERE version_id=? AND archived=0",
        (data.version_id,)
    )
    pos = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "INSERT INTO requirements (version_id,title,description,priority,status,assignee,deadline,estimated_hours,notes,code,position) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (data.version_id, data.title, data.description, data.priority, data.status,
         data.assignee, data.deadline, data.estimated_hours, data.notes, code, pos)
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
             json.dumps({"status": data.status, "priority": data.priority})),
        )
        await db.commit()

    row = await db.execute("SELECT * FROM requirements WHERE id=?", (rid,))
    return dict(await row.fetchone())

@router.put("/requirements/{rid}")
async def update_requirement(rid: int, data: ReqUpdate, db: aiosqlite.Connection = Depends(get_db)):
    updates, params = [], []
    for field in ("title", "description", "priority", "status", "assignee", "deadline", "estimated_hours", "actual_hours", "notes"):
        val = getattr(data, field)
        if val is not None:
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
    valid = ("pending", "dev", "testing", "done", "blocked")
    if data.status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")

    # Permission check for agent roles
    agent_role = getattr(request.state, "agent_role", None) if hasattr(request, "state") else None
    cursor = await db.execute("SELECT status FROM requirements WHERE id=?", (rid,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, f"requirement {rid} not found")
    old_status = row[0]

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

    await db.execute(
        "UPDATE requirements SET status=?, position=?, updated_at=datetime('now','localtime') WHERE id=?",
        (data.status, data.position, rid)
    )

    # Audit trail
    actor = agent_role or "human"
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
                tag_stats[t] = {"tag": t, "total": 0, "pending": 0, "dev": 0, "testing": 0, "done": 0, "description": ""}
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

    grouped = {"pending": [], "dev": [], "testing": [], "done": []}
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
            "avatar": f"/static/avatars/{role_name}_256.png",
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
