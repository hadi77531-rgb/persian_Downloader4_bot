"""Database-backed enforcement of user blocking across every router."""
import logging
import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class BlockCheckMiddleware(BaseMiddleware):
    """Reject updates from blocked users before any handler runs.

    Status lookups hit SQLite once per user per TTL window (default 60s) to
    keep the per-message overhead negligible. Admins are always exempt. If the
    database is temporarily unavailable the check fails open so the bot stays
    responsive; failures are logged.
    """
    def __init__(self, db, ttl_seconds: int = 60, admin_ids: frozenset[int] = frozenset()):
        self.db = db
        self.ttl = ttl_seconds
        self.admin_ids = admin_ids
        self._cache: dict[int, tuple[bool, float]] = {}

    def _cached(self, user_id: int, now: float) -> bool | None:
        entry = self._cache.get(user_id)
        if entry is None:
            return None
        blocked, expires = entry
        if expires <= now:
            self._cache.pop(user_id, None)
            return None
        return blocked

    def _prune_cache(self, now: float, limit: int = 5000) -> None:
        if len(self._cache) <= limit:
            return
        for uid in [uid for uid, (_, exp) in self._cache.items() if exp <= now]:
            self._cache.pop(uid, None)
        while len(self._cache) > limit:
            self._cache.pop(next(iter(self._cache)))

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is not None and user.id not in self.admin_ids:
            now = time.monotonic()
            blocked = self._cached(user.id, now)
            if blocked is None:
                try:
                    status = await self.db.get_user_status(user.id)
                except Exception:
                    logger.exception("block check failed; allowing update from %s", user.id)
                    status = None
                blocked = status == "blocked"
                self._cache[user.id] = (blocked, now + self.ttl)
                self._prune_cache(now)
            if blocked:
                if isinstance(event, Message):
                    await event.answer("دسترسی شما به ربات محدود شده است.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("دسترسی شما به ربات محدود شده است.", show_alert=True)
                return None
        return await handler(event, data)
