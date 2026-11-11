from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.common import PendingStore, start_download
from core.config import get_settings
from core.security import validate_and_detect_url
from db.database import Database
from services import pinterest
from workers.queue_manager import DownloadQueue

router = Router()
pending = PendingStore()


@router.message(F.text)
async def pinterest_url(message: Message) -> None:
    try:
        parsed = validate_and_detect_url(message.text or "")
    except ValueError:
        raise SkipHandler
    if parsed.platform != "Pinterest":
        raise SkipHandler
    status = await message.answer("در حال بررسی Pin عمومی…")
    try:
        info = await pinterest.inspect(parsed.url)
    except Exception:
        await status.edit_text("اطلاعات این Pin به‌صورت ناشناس دریافت نشد؛ ممکن است استخراج Pinterest موقتاً کار نکند یا لینک در دسترس نباشد.")
        return
    if not info.choices:
        await status.edit_text("ویدیوی Pin قابل‌پشتیبانی پیدا نشد. در این نسخه دریافت Pinهای تصویری پشتیبانی نمی‌شود.")
        return
    job_id = f"pin{uuid4().hex[:10]}"
    uid = message.from_user.id if message.from_user else 0
    pending.add(job_id, (uid, parsed.url, info.choices, info.title))
    rows = [[InlineKeyboardButton(text=choice.label, callback_data=f"pin:{job_id}:{choice.key}")] for choice in info.choices]
    await status.edit_text(
        f"ویدیوی Pin شناسایی شد: {escape(info.title)}\nکیفیت را انتخاب کنید. فقط محتوایی را دریافت کنید که اجازهٔ استفاده از آن را دارید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("pin:"))
async def pinterest_quality(query: CallbackQuery, download_queue: DownloadQueue, db: Database) -> None:
    parts = (query.data or "").split(":", 2)
    entry = pending.get(parts[1]) if len(parts) == 3 else None
    if entry is None:
        await query.answer("این انتخاب منقضی شده است؛ لینک را دوباره ارسال کنید.", show_alert=True)
        return
    uid, url, choices, title = entry
    if query.from_user.id != uid:
        await query.answer("این درخواست متعلق به کاربر دیگری است.", show_alert=True)
        return
    choice = next((item for item in choices if item.key == parts[2]), None)
    if choice is None or choice.audio_only:
        await query.answer("گزینهٔ انتخاب‌شده معتبر نیست.", show_alert=True)
        return
    pending.pop(parts[1])
    await query.answer("دانلود آغاز شد.")
    settings = get_settings()
    await start_download(
        query, url=url, title=title, choice=choice, platform="Pinterest",
        downloader=pinterest.download, queue=download_queue, db=db,
        max_bytes=settings.max_file_mb * 1024 * 1024,
    )
