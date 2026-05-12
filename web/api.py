from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import aiosqlite

from core.database import get_db, next_code, generate_prefix
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

@router.get("/versions/{vid}/requirements")
async def list_requirements(vid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM requirements WHERE version_id=? AND archived=0 ORDER BY position",
        (vid,)
    )
    return [dict(row) for row in await cursor.fetchall()]

@router.post("/requirements")
async def create_requirement(data: ReqCreate, db: aiosqlite.Connection = Depends(get_db)):
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
    row = await db.execute("SELECT * FROM requirements WHERE id=?", (cursor.lastrowid,))
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
async def move_requirement(rid: int, data: ReqMove, db: aiosqlite.Connection = Depends(get_db)):
    valid = ("pending", "dev", "testing", "done")
    if data.status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    await db.execute(
        "UPDATE requirements SET status=?, position=?, updated_at=datetime('now','localtime') WHERE id=?",
        (data.status, data.position, rid)
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

@router.get("/requirements/{rid}/commits")
async def list_commits(rid: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM requirement_commits WHERE requirement_id=? ORDER BY created_at DESC", (rid,)
    )
    return [dict(row) for row in await cursor.fetchall()]


@router.get("/commits/{commit_hash}/diff")
async def get_commit_diff(commit_hash: str):
    import asyncio
    import os

    repo_path = os.getenv("KH_REPO_PATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    roles = ["industry", "pm", "coach_dev", "coach_review"]
    result = {}
    for role in roles:
        cursor = await db.execute(
            "SELECT status, started_at, completed_at FROM agent_sessions "
            "WHERE agent_role=? ORDER BY created_at DESC LIMIT 1",
            (role,),
        )
        row = await cursor.fetchone()
        if row:
            row = dict(row)
            result[role] = {"status": row["status"], "last_run": row["started_at"]}
        else:
            result[role] = {"status": "idle", "last_run": None}
    return {"agents": result}
