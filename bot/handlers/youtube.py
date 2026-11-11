from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.common import PendingStore, start_download
from core.config import get_settings
from core.security import validate_and_detect_url
from db.database import Database
from services.youtube import inspect, download
from workers.queue_manager import DownloadQueue

router = Router()
pending = PendingStore()


@router.message(F.text)
async def youtube_url(message: Message) -> None:
    # This router is included after core URL intake; handler only handles explicit YouTube links.
    try:
        parsed = validate_and_detect_url(message.text or "")
    except ValueError:
        raise SkipHandler
    if parsed.platform != "YouTube":
        raise SkipHandler
    status = await message.answer("در حال بررسی اطلاعات ویدیو…")
    try:
        info = await inspect(parsed.url)
    except Exception:
        await status.edit_text("اطلاعات این ویدیو دریافت نشد. ممکن است لینک عمومی نباشد یا YouTube دسترسی را محدود کرده باشد.")
        return
    if not info.choices:
        await status.edit_text("برای این لینک گزینهٔ رسانهٔ قابل‌پشتیبانی پیدا نشد.")
        return
    job_id = f"yt{uuid4().hex[:10]}"
    uid = message.from_user.id if message.from_user else 0
    pending.add(job_id, (uid, parsed.url, info.choices, info.title))
    duration = f"\nمدت: {info.duration} ثانیه" if info.duration else ""
    rows = [[InlineKeyboardButton(text=c.label, callback_data=f"yt:{job_id}:{c.key}")] for c in info.choices]
    await status.edit_text(
        f"عنوان: {escape(info.title)}{duration}\nکیفیت موردنظر را انتخاب کنید. فقط محتوایی را دریافت کنید که مجاز به استفاده از آن هستید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("yt:"))
async def choose_quality(query: CallbackQuery, download_queue: DownloadQueue, db: Database) -> None:
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return
    _, job_id, key = parts
    entry = pending.get(job_id)
    if entry is None:
        await query.answer("این انتخاب منقضی شده است؛ لینک را دوباره ارسال کنید.", show_alert=True)
        return
    uid, url, choices, title = entry
    if query.from_user.id != uid:
        await query.answer("این درخواست متعلق به کاربر دیگری است.", show_alert=True)
        return
    choice = next((item for item in choices if item.key == key), None)
    if choice is None:
        await query.answer("گزینهٔ کیفیت معتبر نیست.", show_alert=True)
        return
    pending.pop(job_id)
    await query.answer("دانلود آغاز شد.")
    settings = get_settings()
    await start_download(
        query, url=url, title=title, choice=choice, platform="YouTube",
        downloader=download, queue=download_queue, db=db,
        max_bytes=settings.max_file_mb * 1024 * 1024,
    )
