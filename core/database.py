import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "data/kanban.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                color TEXT DEFAULT '#4f46e5',
                prefix TEXT DEFAULT '',
                advisor_skill TEXT DEFAULT '',
                product_memory TEXT DEFAULT '',
                git_repo_path TEXT DEFAULT '',
                git_remote_url TEXT DEFAULT '',
                git_last_synced_at TEXT DEFAULT '',
                archived INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                updated_at DATETIME DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'planning' CHECK(status IN ('planning','active','testing','released')),
                git_tag TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                updated_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority TEXT DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending','dev','testing','done')),
                assignee TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                estimated_hours REAL DEFAULT 0,
                actual_hours REAL DEFAULT 0,
                tags TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                code TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                updated_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                filesize INTEGER DEFAULT 0,
                uploaded_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL,
                author TEXT DEFAULT '',
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS project_architecture (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL UNIQUE,
                content TEXT DEFAULT '',
                updated_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS requirement_commits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL,
                commit_hash TEXT NOT NULL,
                repo_path TEXT DEFAULT '',
                message TEXT DEFAULT '',
                committed_at TEXT DEFAULT '',
                files_changed TEXT DEFAULT '[]',
                total_additions INTEGER DEFAULT 0,
                total_deletions INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE,
                UNIQUE(requirement_id, commit_hash, repo_path)
            );

            -- KH-specific: Agent session tracking
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                agent_role TEXT NOT NULL CHECK(agent_role IN ('industry','pm','coach_dev','coach_review')),
                status TEXT DEFAULT 'idle' CHECK(status IN ('idle','running','completed','failed')),
                trigger_type TEXT DEFAULT '',
                input_context TEXT DEFAULT '',
                output_summary TEXT DEFAULT '',
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- KH-specific: Scheduler task queue
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                cron_expr TEXT DEFAULT '',
                next_run_at DATETIME,
                last_run_at DATETIME,
                enabled INTEGER DEFAULT 1,
                config TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- Indexes
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_prefix ON projects(prefix) WHERE prefix != '';
        """)
        await db.commit()


def generate_prefix(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return "".join(p[0] for p in parts[:3]).upper()
    return name[:2].upper()


async def next_code(db: aiosqlite.Connection, version_id: int) -> str:
    row = await db.execute(
        "SELECT p.prefix, p.id FROM projects p "
        "JOIN versions v ON v.project_id=p.id WHERE v.id=?", (version_id,)
    )
    proj = await row.fetchone()
    if not proj:
        return ""
    prefix, project_id = proj[0], proj[1]
    cursor = await db.execute(
        "SELECT MAX(CAST(SUBSTR(r.code, LENGTH(p.prefix)+2) AS INTEGER)) "
        "FROM requirements r "
        "JOIN versions v ON r.version_id=v.id "
        "JOIN projects p ON v.project_id=p.id "
        "WHERE v.project_id=? AND r.code != '' AND r.code IS NOT NULL",
        (project_id,)
    )
    max_seq = (await cursor.fetchone())[0] or 0
    return f"{prefix}-{max_seq + 1:03d}"
