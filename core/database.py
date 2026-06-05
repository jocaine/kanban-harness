import logging
import aiosqlite  # 异步 SQLite 驱动，整个项目用 async/await 操作数据库
import os
from dotenv import load_dotenv

logger = logging.getLogger("kh.core.database")

# 加载 .env，override=True 表示 .env 优先级高于已有环境变量
load_dotenv(override=True)

# 数据库文件路径，默认 data/kanban.db
DB_PATH = os.getenv("DB_PATH", "data/kanban.db")


# 异步数据库连接生成器，配合依赖注入或 async for 使用
# WAL 模式允许读写并发；row_factory=Row 使结果可按列名访问
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")  # SQLite 默认关闭外键，必须手动开启
    try:
        yield db
    finally:
        await db.close()


# 增量迁移：启动时自动检测 schema 并修补
# 策略：检测当前表结构是否缺少某特征 → 缺则执行变更
# SQLite 不支持 ALTER CHECK 约束，所以改枚举值时必须重建表（新建→导数据→删旧→改名）
async def _migrate_db(db: aiosqlite.Connection):
    """Run schema migrations for existing databases."""
    # Migration 1: 给 status 枚举加 'research' 值（需重建表）
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
                status TEXT DEFAULT 'organizing' CHECK(status IN ('research','organizing','dev','testing','done','blocked')),
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

    # Migration 2: 重命名 'pending' → 'organizing'（重建表 + CASE WHEN 转数据）
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='requirements'"
    )
    row = await cursor.fetchone()
    if row and "'organizing'" not in row[0]:
        logger.info("Migrating requirements table: pending→organizing status rename")
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
                status TEXT DEFAULT 'organizing' CHECK(status IN ('research','organizing','dev','testing','done','blocked')),
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
            INSERT INTO requirements_new
                SELECT id, version_id, title, description, priority, type,
                       CASE WHEN status='pending' THEN 'organizing' ELSE status END,
                       assignee, deadline, estimated_hours, actual_hours, tags, notes,
                       queue_reason, code, position, archived, created_at, updated_at
                FROM requirements;
            DROP TABLE requirements;
            ALTER TABLE requirements_new RENAME TO requirements;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_code ON requirements(code) WHERE code != '';
            PRAGMA foreign_keys=ON;
        """)
        await db.commit()

    # Migration 3: 加 queue_reason 列（需重建表因为同时要改 CHECK 约束）
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
                status TEXT DEFAULT 'organizing' CHECK(status IN ('research','organizing','dev','testing','done','blocked')),
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

    # Migration 4: 加 agent_timeout 列（简单 ALTER，不需重建）
    cursor = await db.execute("PRAGMA table_info(requirements)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "agent_timeout" not in columns:
        logger.info("Migrating requirements table: adding agent_timeout column")
        await db.execute(
            "ALTER TABLE requirements ADD COLUMN agent_timeout INTEGER DEFAULT NULL"
        )
    await db.commit()

    # Migration 5: comments 表加 detail 列（存评论的展开详情）
    cursor = await db.execute("PRAGMA table_info(comments)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "detail" not in columns:
        logger.info("Migrating comments table: adding detail column")
        await db.execute(
            "ALTER TABLE comments ADD COLUMN detail TEXT DEFAULT ''"
        )
    await db.commit()

    # Migration 6: agent_sessions 加 token 消耗统计（input/output/total）
    cursor = await db.execute("PRAGMA table_info(agent_sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "input_tokens" not in columns:
        logger.info("Migrating agent_sessions: adding token tracking columns")
        await db.execute("ALTER TABLE agent_sessions ADD COLUMN input_tokens INTEGER DEFAULT 0")
        await db.execute("ALTER TABLE agent_sessions ADD COLUMN output_tokens INTEGER DEFAULT 0")
        await db.execute("ALTER TABLE agent_sessions ADD COLUMN total_tokens INTEGER DEFAULT 0")
    await db.commit()

    # Migration 7: 王权问答重构 — 加 ceo_decision（CEO决策结果）和 progressed_at（最后推进时间）
    cursor = await db.execute("PRAGMA table_info(requirements)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "ceo_decision" not in columns:
        logger.info("Migrating requirements table: adding ceo_decision column")
        await db.execute(
            "ALTER TABLE requirements ADD COLUMN ceo_decision TEXT DEFAULT NULL"
        )
    if "progressed_at" not in columns:
        logger.info("Migrating requirements table: adding progressed_at column")
        await db.execute(
            "ALTER TABLE requirements ADD COLUMN progressed_at DATETIME DEFAULT NULL"
        )
        await db.execute(
            "UPDATE requirements SET progressed_at = updated_at WHERE progressed_at IS NULL"
        )
    await db.commit()

    # Migration 8: chat_tasks 加 token 统计（与 agent_sessions 对齐）
    cursor = await db.execute("PRAGMA table_info(chat_tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "input_tokens" not in columns:
        logger.info("Migrating chat_tasks: adding token tracking columns")
        await db.execute("ALTER TABLE chat_tasks ADD COLUMN input_tokens INTEGER DEFAULT 0")
        await db.execute("ALTER TABLE chat_tasks ADD COLUMN output_tokens INTEGER DEFAULT 0")
        await db.execute("ALTER TABLE chat_tasks ADD COLUMN total_tokens INTEGER DEFAULT 0")
    await db.commit()

    # Migration 9: agent_sessions 加 requirement_id（从 JSON 字段回填，方便直接查询）
    cursor = await db.execute("PRAGMA table_info(agent_sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "requirement_id" not in columns:
        logger.info("Migrating agent_sessions: adding requirement_id column")
        await db.execute("ALTER TABLE agent_sessions ADD COLUMN requirement_id INTEGER DEFAULT NULL")
        await db.execute(
            "UPDATE agent_sessions SET requirement_id = "
            "CAST(json_extract(input_context, '$.requirement_id') AS INTEGER) "
            "WHERE input_context != '' AND json_valid(input_context)"
        )
    await db.commit()


# 首次建库 + 后续迁移，应用启动时调用一次
# 阶段1: CREATE TABLE IF NOT EXISTS 建全部表
# 阶段2: 修复 prefix 唯一索引
# 阶段3: 跑增量迁移（_migrate_db）
async def init_db():
    # 确保数据库目录存在
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript("""
            -- ====== 核心看板（projects → versions → requirements） ======
            -- projects: 顶层项目容器
            -- versions: 项目下的版本/里程碑，FK → projects
            -- requirements: 需求卡片，FK → versions（级联删除）

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                color TEXT DEFAULT '#4f46e5',
                prefix TEXT DEFAULT '',
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
                status TEXT DEFAULT 'organizing' CHECK(status IN ('research','organizing','dev','testing','done','blocked')),
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

            -- ====== 需求附属（都 FK → requirements，级联删除） ======
            -- attachments: 文件附件
            -- comments: 评论/讨论
            -- requirement_commits: git commit 关联

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
                detail TEXT DEFAULT '',
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );

            -- ====== 项目附属（FK → projects） ======
            -- project_architecture: 架构文档，每项目一条

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

            -- ====== AI Agent 系统（FK → projects） ======
            -- agent_sessions: agent 运行记录（角色、状态、耗时）
            -- scheduled_tasks: 定时任务队列（cron 调度）
            -- agent_events: agent 异步事件（跨 agent 协作通信）

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

            -- ====== 对话系统（FK → projects） ======
            -- chat_messages: 对话历史（user/assistant/summary 三种角色）
            -- chat_tasks: 后台 AI 任务（与 SSE 推送解耦）

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

            -- Background chat tasks (v0.7: AI execution decoupled from SSE)
            CREATE TABLE IF NOT EXISTS chat_tasks (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL,
                status TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed','cancelled')),
                user_message TEXT NOT NULL,
                model TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                response_text TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                chunk_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now','localtime')),
                completed_at DATETIME,
                expires_at DATETIME DEFAULT (datetime('now','localtime','+1 hour')),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_tasks_project
                ON chat_tasks(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_tasks_status
                ON chat_tasks(status) WHERE status='running';
        """)
        await db.commit()

    # 修复 prefix 唯一索引：只对未归档项目做唯一约束（归档项目允许前缀重复）
    async with aiosqlite.connect(DB_PATH) as db_idx:
        await db_idx.executescript("""
            DROP INDEX IF EXISTS idx_projects_prefix;
            CREATE UNIQUE INDEX idx_projects_prefix ON projects(prefix) WHERE prefix != '' AND archived=0;
        """)
        await db_idx.commit()

    # 迁移：给 requirements 加 type 列（research/dev），需重建表
    # Migration 2: Add 'type' column to requirements
    async with aiosqlite.connect(DB_PATH) as db2:
        await db2.execute("PRAGMA foreign_keys=ON")
        cursor = await db2.execute("PRAGMA table_info(requirements)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "type" not in columns:
            logger.info("Migrating requirements table: adding type column")
            await db2.executescript("""
                PRAGMA foreign_keys=OFF;
                DROP TABLE IF EXISTS requirements_new;
                DELETE FROM requirements WHERE status NOT IN ('research','organizing','dev','testing','done','blocked');
                CREATE TABLE requirements_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    priority TEXT DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
                    type TEXT DEFAULT 'dev' CHECK(type IN ('research','dev')),
                    status TEXT DEFAULT 'organizing' CHECK(status IN ('research','organizing','dev','testing','done','blocked')),
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

    # 最后统一跑 _migrate_db 里的所有增量迁移
    async with aiosqlite.connect(DB_PATH) as db2:
        await db2.execute("PRAGMA foreign_keys=ON")
        await _migrate_db(db2)
        await db2.commit()


# 从项目名生成 2-3 字符前缀，用于需求编号（如 "Kanban Harness" → "KH"）
def generate_prefix(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return "".join(p[0] for p in parts[:3]).upper()  # 多词取首字母，最多3个
    return name[:2].upper()  # 单词取前两个字符


# 生成下一个需求编号（如 KH-004），跨版本全局递增，同项目下编号连续不重复
async def next_code(db: aiosqlite.Connection, version_id: int) -> str:
    # 1. 通过 version_id 找到项目前缀
    row = await db.execute(
        "SELECT p.prefix, p.id FROM projects p "
        "JOIN versions v ON v.project_id=p.id WHERE v.id=?", (version_id,)
    )
    proj = await row.fetchone()
    if not proj:
        return ""
    prefix = proj[0]
    # 2. 在该前缀下所有需求中找最大序号（SUBSTR 截取前缀后面的数字部分）
    cursor = await db.execute(
        "SELECT MAX(CAST(SUBSTR(r.code, LENGTH(p.prefix)+2) AS INTEGER)) "
        "FROM requirements r "
        "JOIN versions v ON r.version_id=v.id "
        "JOIN projects p ON v.project_id=p.id "
        "WHERE p.prefix=? AND p.archived=0 AND r.code != '' AND r.code IS NOT NULL",
        (prefix,)
    )
    max_seq = (await cursor.fetchone())[0] or 0
    return f"{prefix}-{max_seq + 1:03d}"  # 3位补零，如 KH-001
