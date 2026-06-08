"""卡片生命周期日志 — 持久化到 requirement_logs 表，同时输出到标准 logger。"""

import logging
import aiosqlite

from core.database import DB_PATH

logger = logging.getLogger("kh.core.card_log")


async def card_log(
    requirement_id: int,
    message: str,
    level: str = "info",
    source: str = "",
):
    """写入一条卡片生命周期日志。"""
    getattr(logger, level, logger.info)("[card:%d] %s", requirement_id, message)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO requirement_logs (requirement_id, level, source, message) "
                "VALUES (?, ?, ?, ?)",
                (requirement_id, level, source, message),
            )
            await db.commit()
    except Exception as e:
        logger.warning("card_log 写入失败 (req=%d): %s", requirement_id, e)
