"""Shared download pipeline used by every platform handler.

Responsibilities consolidated here:
- TTL'd pending-selection store (process-local, bounded memory)
- queue submission with duplicate / busy / full feedback
- DB event recording: queued -> running -> succeeded | failed | cancelled
- progress message updates driven by the downloader hook
- ownership-checked cancel button for queued and running jobs
- one shared pipeline for single-file downloads (video/audio) and for
  image-set downloads (Instagram photo posts / carousels)

All user-controlled text (media titles) is HTML-escaped before being sent
with the bot's default HTML parse mode.
"""
import asyncio
from html import escape
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message, FSInputFile

from db.database import Database
from workers.queue_manager import (
    DownloadQueue,
    DuplicateJobError,
    QueueFullError,
    UserBusyError,
)

logger = logging.getLogger(__name__)

router = Router(name="download-common")

PENDING_TTL_SECONDS = 3600
PENDING_CAPACITY = 300
PROGRESS_EDIT_INTERVAL = 4.0
CAPTION_LIMIT = 1024
AUDIO_TITLE_LIMIT = 64

_FAIL_TEXTS = {
    "YouTube": "دانلود انجام نشد. ممکن است ویدیو در دسترس نباشد، محدودیت پلتفرم اعمال شده باشد یا حجم فایل بیش از حد مجاز باشد.",
    "Instagram": "دریافت ناموفق بود؛ لینک ممکن است محدود یا ناموجود باشد، یا دسترسی ناشناس به آن مسدود شده باشد.",
    "Pinterest": "دریافت ناموفق بود؛ لینک ممکن است محدود، ناموجود یا برای استخراج ناشناس پشتیبانی‌نشده باشد.",
}
_SEND_FAIL_TEXT = "فایل آماده شد، اما ارسال آن از طریق تلگرام ناموفق بود. حجم یا قالب رسانه ممکن است با محدودیت API سازگار نباشد."


class PendingStore:
    """Bounded, TTL-expiring store for pending quality selections."""

    def __init__(self, capacity: int = PENDING_CAPACITY, ttl: float = PENDING_TTL_SECONDS):
        self._capacity = capacity
        self._ttl = ttl
        self._data: dict[str, tuple[object, float]] = {}

    def add(self, job_id: str, value: object) -> None:
        now = time.monotonic()
        if len(self._data) >= self._capacity:
            for key in [k for k, (_, stamp) in self._data.items() if now - stamp > self._ttl]:
                self._data.pop(key, None)
            while len(self._data) >= self._capacity and self._data:
                self._data.pop(next(iter(self._data)))
        self._data[job_id] = (value, now)

    def get(self, job_id: str) -> object | None:
        entry = self._data.get(job_id)
        if entry is None:
            return None
        value, stamp = entry
        if time.monotonic() - stamp > self._ttl:
            self._data.pop(job_id, None)
            return None
        return value

    def pop(self, job_id: str) -> object | None:
        value = self.get(job_id)
        self._data.pop(job_id, None)
        return value

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class Progress:
    downloaded: int = 0
    total: int = 0
    speed: float = 0
    eta: int | None = None
    percent: float | None = None


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو دانلود", callback_data=f"dl:cancel:{job_id}")
    ]])


@router.callback_query(F.data.startswith("dl:cancel:"))
async def cancel_download(query: CallbackQuery, download_queue: DownloadQueue) -> None:
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return
    job_id = parts[2]
    job = download_queue.get(job_id)
    if job is None:
        await query.answer("این درخواست دیگر فعال نیست.", show_alert=True)
        return
    if job.user_id != query.from_user.id:
        await query.answer("این درخواست متعلق به کاربر دیگری است.", show_alert=True)
        return
    cancelled = await download_queue.cancel(job_id, query.from_user.id)
    if cancelled:
        await query.answer("درخواست لغو ثبت شد.")
    else:
        await query.answer("این مرحله قابل لغو نیست؛ شاید تمام شده باشد.", show_alert=True)


async def _safe_edit(message: Message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        pass  # e.g. "message is not modified" or an inaccessible message


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def _make_hook(loop: asyncio.AbstractEventLoop, updates: asyncio.Queue) -> Callable[[dict], None]:
    def hook(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        payload = {
            "downloaded": int(data.get("downloaded_bytes") or 0),
            "total": int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0),
            "speed": float(data.get("speed") or 0),
            "eta": data.get("eta"),
            "percent": data.get("percent"),
        }

        def push() -> None:
            if updates.full():
                try:
                    updates.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            updates.put_nowait(payload)

        loop.call_soon_threadsafe(push)

    return hook


async def _show_progress(task: asyncio.Task, status: Message, updates: asyncio.Queue, label: str) -> None:
    loop = asyncio.get_running_loop()
    progress = Progress()
    last_edit = 0.0
    while not task.done():
        try:
            values = await asyncio.wait_for(updates.get(), timeout=2)
            progress.downloaded = values.get("downloaded", progress.downloaded)
            progress.total = values.get("total", progress.total)
            progress.speed = values.get("speed", progress.speed)
            progress.eta = values.get("eta", progress.eta)
            progress.percent = values.get("percent", progress.percent)
        except asyncio.TimeoutError:
            pass
        now = loop.time()
        if now - last_edit >= PROGRESS_EDIT_INTERVAL:
            if progress.percent is not None:
                pct = min(100, int(progress.percent))
            elif progress.total:
                pct = min(100, int(progress.downloaded * 100 / progress.total))
            else:
                pct = None
            if pct is not None:
                speed = f"{progress.speed / 1024 / 1024:.1f} MB/s" if progress.speed else "—"
                eta = f"{progress.eta} ثانیه" if progress.eta is not None else "—"
                await _safe_edit(status, f"در حال دریافت {label}: {pct}%\nسرعت: {speed}\nزمان تقریبی: {eta}")
            last_edit = now


async def start_download(
    query: CallbackQuery,
    *,
    url: str,
    title: str,
    choice,
    platform: str,
    downloader,          # async (url, choice, directory, progress, max_bytes) -> DownloadedMedia
    queue: DownloadQueue,
    db: Database,
    max_bytes: int,
) -> None:
    """Record, enqueue and run one platform download with full user feedback."""
    if query.from_user is None or not query.message:
        return
    user_id = query.from_user.id
    job_id = uuid.uuid4().hex[:12]
    media_type = "audio" if choice.audio_only else "video"
    label = "صدا" if choice.audio_only else "ویدیو"

    try:
        await db.record_download(job_id, user_id, platform, "queued", media_type)
    except Exception:
        logger.exception("failed to record queued event")

    try:
        status = await query.message.answer("در صف انتظار پردازش…")
    except Exception:
        logger.exception("failed to send status message")
        return

    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[dict] = asyncio.Queue(maxsize=10)
    hook = _make_hook(loop, updates)
    message_target = query.message

    async def record(state: str) -> None:
        try:
            await db.record_download(job_id, user_id, platform, state, media_type)
        except Exception:
            logger.exception("failed to record %s event for job %s", state, job_id)

    async def work() -> None:
        await record("running")
        with tempfile.TemporaryDirectory(prefix="tgdl-") as tmp:
            task = asyncio.create_task(downloader(url, choice, tmp, hook, max_bytes))
            watcher = asyncio.create_task(_show_progress(task, status, updates, label))
            try:
                media = await task
            except asyncio.CancelledError:
                await record("cancelled")
                await _safe_edit(status, "دانلود لغو شد.")
                raise
            except Exception:
                logger.exception("download failed for job %s (%s)", job_id, platform)
                await record("failed")
                await _safe_edit(status, _FAIL_TEXTS.get(platform, _FAIL_TEXTS["YouTube"]))
                raise RuntimeError("download failed") from None
            finally:
                watcher.cancel()
            try:
                await _safe_edit(status, "در حال ارسال فایل به تلگرام…")
                upload = FSInputFile(media.path, filename=media.path.name)
                if choice.audio_only:
                    await message_target.answer_audio(upload, title=(title or platform)[:AUDIO_TITLE_LIMIT])
                else:
                    await message_target.answer_video(upload, caption=escape(title[:CAPTION_LIMIT]), supports_streaming=True)
                await _safe_delete(status)
            except asyncio.CancelledError:
                await record("cancelled")
                await _safe_edit(status, "دانلود لغو شد.")
                raise
            except Exception:
                logger.exception("telegram upload failed for job %s", job_id)
                await record("failed")
                await _safe_edit(status, _SEND_FAIL_TEXT)
                raise RuntimeError("telegram upload failed") from None
            await record("succeeded")

    try:
        job, position = await queue.submit(job_id, user_id, url, work, meta={"platform": platform, "media_type": media_type})
    except DuplicateJobError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "همین لینک هم‌اکنون در صف دانلود یا در حال پردازش است.")
        return
    except UserBusyError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "شما یک دانلود فعال دارید؛ پس از پایان آن، درخواست جدید بفرستید.")
        return
    except QueueFullError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "صف دانلود پر است؛ چند لحظه بعد دوباره تلاش کنید.")
        return

    position_text = f"\nموقعیت شما در صف: {position}" if position > 1 else ""
    await _safe_edit(
        status,
        f"درخواست ثبت شد.{position_text}\nبا شروع پردازش، پیشرفت همین‌جا نمایش داده می‌شود.",
        reply_markup=cancel_keyboard(job_id),
    )


async def _record_failure(db: Database, job_id: str, user_id: int, platform: str, media_type: str) -> None:
    """Record a rejection that happened before the job ever entered the queue."""
    try:
        await db.record_download(job_id, user_id, platform, "failed", media_type)
    except Exception:
        logger.exception("failed to record rejection event")


async def start_image_download(
    query: CallbackQuery,
    *,
    url: str,
    title: str,
    platform: str,
    fetcher,              # async (url, directory, max_total_bytes) -> object with .paths/.title/.total_size
    queue: DownloadQueue,
    db: Database,
    max_total_bytes: int,
) -> None:
    """Queue, run and deliver an image-set download (single photo or album)."""
    if query.from_user is None or not query.message:
        return
    user_id = query.from_user.id
    job_id = uuid.uuid4().hex[:12]
    media_type = "image"

    try:
        await db.record_download(job_id, user_id, platform, "queued", media_type)
    except Exception:
        logger.exception("failed to record queued event")

    try:
        status = await query.message.answer("در صف انتظار پردازش…")
    except Exception:
        logger.exception("failed to send status message")
        return

    message_target = query.message

    async def record(state: str) -> None:
        try:
            await db.record_download(job_id, user_id, platform, state, media_type)
        except Exception:
            logger.exception("failed to record %s event for job %s", state, job_id)

    async def work() -> None:
        await record("running")
        with tempfile.TemporaryDirectory(prefix="tgimg-") as tmp:
            try:
                images = await fetcher(url, tmp, max_total_bytes)
            except asyncio.CancelledError:
                await record("cancelled")
                await _safe_edit(status, "دانلود لغو شد.")
                raise
            except Exception:
                logger.exception("image fetch failed for job %s (%s)", job_id, platform)
                await record("failed")
                await _safe_edit(status, _FAIL_TEXTS.get(platform, _FAIL_TEXTS["YouTube"]))
                raise RuntimeError("image fetch failed") from None

            paths = [Path(p) for p in getattr(images, "paths", ())]
            caption = escape(str(getattr(images, "title", "") or title)[:CAPTION_LIMIT])
            try:
                await _safe_edit(status, "در حال ارسال تصاویر به تلگرام…")
                if not paths:
                    raise RuntimeError("empty image set")
                if len(paths) == 1:
                    await message_target.answer_photo(FSInputFile(paths[0], filename=paths[0].name),
                                                      caption=caption)
                else:
                    album = [InputMediaPhoto(media=FSInputFile(p, filename=p.name),
                                             caption=caption if i == 0 else None)
                             for i, p in enumerate(paths[:10])]
                    await message_target.answer_media_group(media=album)
                await _safe_delete(status)
            except asyncio.CancelledError:
                await record("cancelled")
                await _safe_edit(status, "دانلود لغو شد.")
                raise
            except Exception:
                logger.exception("telegram album upload failed for job %s", job_id)
                await record("failed")
                await _safe_edit(status, _SEND_FAIL_TEXT)
                raise RuntimeError("telegram album upload failed") from None
            await record("succeeded")

    try:
        await queue.submit(job_id, user_id, url, work, meta={"platform": platform, "media_type": media_type})
    except DuplicateJobError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "همین لینک هم‌اکنون در صف دانلود یا در حال پردازش است.")
        return
    except UserBusyError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "شما یک دانلود فعال دارید؛ پس از پایان آن، درخواست جدید بفرستید.")
        return
    except QueueFullError:
        await _record_failure(db, job_id, user_id, platform, media_type)
        await _safe_edit(status, "صف دانلود پر است؛ چند لحظه بعد دوباره تلاش کنید.")
        return

    await _safe_edit(
        status,
        "درخواست ثبت شد.\nبا شروع پردازش، همین پیام به‌روزرسانی می‌شود.",
        reply_markup=cancel_keyboard(job_id),
    )
