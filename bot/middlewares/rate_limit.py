import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class RateLimitMiddleware(BaseMiddleware):
    """In-memory sliding-window limiter. Process-local, so not multi-instance."""
    def __init__(self, limit: int = 6, window_seconds: int = 60, admin_ids: frozenset[int] = frozenset()):
        self.limit = limit
        self.window = window_seconds
        self.admin_ids = admin_ids
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def _prune_stale(self, now: float, limit: int = 5000) -> None:
        """Bound memory: drop empty/aged buckets once many distinct users appear."""
        if len(self._events) <= limit:
            return
        for uid in [uid for uid, ev in self._events.items() if not ev or ev[0] <= now - self.window]:
            self._events.pop(uid, None)
        while len(self._events) > limit:
            self._events.pop(next(iter(self._events)))

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is None or user.id in self.admin_ids:
            return await handler(event, data)
        now = time.monotonic()
        events = self._events[user.id]
        while events and events[0] <= now - self.window:
            events.popleft()
        if len(events) >= self.limit:
            if isinstance(event, Message):
                await event.answer("درخواست‌های شما زیاد است؛ کمی بعد دوباره تلاش کنید.")
            elif isinstance(event, CallbackQuery):
                await event.answer("درخواست‌های شما زیاد است؛ کمی بعد دوباره تلاش کنید.", show_alert=True)
            return None
        events.append(now)
        self._prune_stale(now)
        return await handler(event, data)
