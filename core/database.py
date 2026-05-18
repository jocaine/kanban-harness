import logging
import aiosqlite
import os
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(override=True)

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


async def _migrate_db(db: aiosqlite.Connection):
    """Run schema migrations for existing databases."""
    # Migration 1: Add 'research' to requirements status CHECK constraint
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='requirements'"
    )
    row = await cursor.fetchone()
    if row and "'research'" not in row[0]:
        # Old constraint doesn't allow 'research' — recreate table
        await db.executescript("""
            PRAGMA foreign_keys=OFF;
            DROP TABLE IF EXISTS requirements_new;
            CREATE TABLE requirements_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority TEXT DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
                type TEXT DEFAULT 'dev' CHECK(type IN ('research','dev')),
                status TEXT DEFAULT 'pending' CHECK(status IN ('research','pending','dev','testing','done','blocked')),
                assignee TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                estimated_hours REAL DEFAULT 0,
                actual_hours REAL DEFAULT 0,
                tags TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                queue_reason TEXT DEFAULT '',
                code TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                updated_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
            );
            INSERT INTO requirements_new (id, version_id, title, description, priority, type, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at) SELECT id, version_id, title, description, priority, type, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at FROM requirements;
            DROP TABLE requirements;
            ALTER TABLE requirements_new RENAME TO requirements;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
            PRAGMA foreign_keys=ON;
        """)
        await db.commit()

    # Migration: Add queue_reason column to requirements
    cursor = await db.execute("PRAGMA table_info(requirements)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "queue_reason" not in columns:
        logger.info("Migrating requirements table: adding queue_reason column")
        await db.executescript("""
            PRAGMA foreign_keys=OFF;
            DROP TABLE IF EXISTS requirements_new;
            CREATE TABLE requirements_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority TEXT DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
                type TEXT DEFAULT 'dev' CHECK(type IN ('research','dev')),
                status TEXT DEFAULT 'pending' CHECK(status IN ('research','pending','dev','testing','done','blocked')),
                assignee TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                estimated_hours REAL DEFAULT 0,
                actual_hours REAL DEFAULT 0,
                tags TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                queue_reason TEXT DEFAULT '',
                code TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                updated_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
            );
            INSERT INTO requirements_new (id, version_id, title, description, priority, type, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at) SELECT id, version_id, title, description, priority, type, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at FROM requirements;
            DROP TABLE requirements;
            ALTER TABLE requirements_new RENAME TO requirements;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
            PRAGMA foreign_keys=ON;
        """)
    await db.commit()


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
                type TEXT DEFAULT 'dev' CHECK(type IN ('research','dev')),
                status TEXT DEFAULT 'pending' CHECK(status IN ('research','pending','dev','testing','done','blocked')),
                assignee TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                estimated_hours REAL DEFAULT 0,
                actual_hours REAL DEFAULT 0,
                tags TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                queue_reason TEXT DEFAULT '',
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
                status TEXT DEFAULT 'idle' CHECK(status IN ('idle','running','completed','failed','blocked')),
                trigger_type TEXT DEFAULT '',
                input_context TEXT DEFAULT '',
                output_summary TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0,
                parent_session_id INTEGER,
                timeout_seconds INTEGER DEFAULT 600,
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

            -- KH-specific: Agent event queue for async collaboration
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                requirement_id INTEGER,
                context TEXT DEFAULT '{}',
                processed INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime'))
            );

            -- Indexes
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_prefix ON projects(prefix) WHERE prefix != '' AND archived=0;

            -- Chat conversation history
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','summary')),
                content TEXT NOT NULL,
                agent_role TEXT DEFAULT '',
                token_estimate INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_project
                ON chat_messages(project_id, created_at DESC);
        """)
        await db.commit()

    # Migration 2: Add 'type' column to requirements
    async with aiosqlite.connect(DB_PATH) as db2:
        await db2.execute("PRAGMA foreign_keys=ON")
        cursor = await db2.execute("PRAGMA table_info(requirements)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "type" not in columns:
            logger.info("Migrating requirements table: adding type column")
            await db2.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE requirements_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    priority TEXT DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
                    type TEXT DEFAULT 'dev' CHECK(type IN ('research','dev')),
                    status TEXT DEFAULT 'pending' CHECK(status IN ('research','pending','dev','testing','done','blocked')),
                    assignee TEXT DEFAULT '',
                    deadline TEXT DEFAULT '',
                    estimated_hours REAL DEFAULT 0,
                    actual_hours REAL DEFAULT 0,
                    tags TEXT DEFAULT '[]',
                    notes TEXT DEFAULT '',
                    queue_reason TEXT DEFAULT '',
                    code TEXT DEFAULT '',
                    position INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT (datetime('now','localtime')),
                    updated_at DATETIME DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
                );
                INSERT INTO requirements_new (id, version_id, title, description, priority, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at) SELECT id, version_id, title, description, priority, status, assignee, deadline, estimated_hours, actual_hours, tags, notes, code, position, archived, created_at, updated_at FROM requirements;
                DROP TABLE requirements;
                ALTER TABLE requirements_new RENAME TO requirements;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
                PRAGMA foreign_keys=ON;
            """)
        await db2.commit()

    async with aiosqlite.connect(DB_PATH) as db2:
        await db2.execute("PRAGMA foreign_keys=ON")
        await _migrate_db(db2)
        await db2.commit()


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
    prefix = proj[0]
    cursor = await db.execute(
        "SELECT MAX(CAST(SUBSTR(r.code, LENGTH(p.prefix)+2) AS INTEGER)) "
        "FROM requirements r "
        "JOIN versions v ON r.version_id=v.id "
        "JOIN projects p ON v.project_id=p.id "
        "WHERE p.prefix=? AND p.archived=0 AND r.code != '' AND r.code IS NOT NULL",
        (prefix,)
    )
    max_seq = (await cursor.fetchone())[0] or 0
    return f"{prefix}-{max_seq + 1:03d}"
