import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject
from aiogram.dispatcher.middlewares.base import BaseMiddleware

from bot.handlers.user import router as user_router
from bot.handlers.common import router as download_common_router
from bot.handlers.youtube import router as youtube_router
from bot.handlers.instagram import router as instagram_router
from admin.handlers import router as admin_router
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.middlewares.block_check import BlockCheckMiddleware
from workers.queue_manager import DownloadQueue, Job
from bot.handlers.pinterest import router as pinterest_router
from core.config import get_settings
from db.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, db: Database):
        self.db = db

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["db"] = self.db
        return await handler(event, data)


async def _prune_loop(db: Database, retention_days: int) -> None:
    """Prune expired download records at startup and every 6 hours."""
    while True:
        try:
            deleted = await db.prune_old_records(retention_days)
            if deleted:
                logging.info("retention prune removed %s download records", deleted)
        except Exception:
            logging.exception("retention prune failed")
        await asyncio.sleep(_PRUNE_INTERVAL_SECONDS)


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    await db.initialize()
    bot = Bot(token=settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    queue = DownloadQueue(max_waiting=20, per_user_active=1)

    async def record_finished_job(job: Job) -> None:
        meta = job.meta or {}
        try:
            await db.record_download(job.job_id, job.user_id, meta.get("platform", "unknown"),
                                     job.state.value, meta.get("media_type"))
        except Exception:
            logging.exception("failed to record terminal state for job %s", job.job_id)

    queue.on_finish = record_finished_job
    await queue.start()
    prune_task = asyncio.create_task(_prune_loop(db, settings.history_retention_days), name="retention-prune")

    dp["download_queue"] = queue
    dp.update.middleware(DatabaseMiddleware(db))
    blocker = BlockCheckMiddleware(db, admin_ids=settings.admin_id_set)
    dp.message.middleware(blocker)
    dp.callback_query.middleware(blocker)
    limiter = RateLimitMiddleware(limit=6, window_seconds=60, admin_ids=settings.admin_id_set)
    dp.message.middleware(limiter)
    dp.callback_query.middleware(limiter)
    dp.include_router(admin_router)
    dp.include_router(download_common_router)
    dp.include_router(pinterest_router)
    dp.include_router(instagram_router)
    dp.include_router(youtube_router)
    dp.include_router(user_router)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        prune_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await prune_task
        await queue.close(drain=False)
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
