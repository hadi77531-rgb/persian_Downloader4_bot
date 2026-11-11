from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.common import PendingStore, start_download, start_image_download
from core.config import get_settings
from core.security import validate_and_detect_url
from db.database import Database
from services import instagram
from workers.queue_manager import DownloadQueue

router = Router()
pending = PendingStore()          # video quality selections
pending_images = PendingStore()   # image/carousel confirmations

_VIDEO_FAIL_TEXT = (
    "اطلاعات این لینک به‌صورت ناشناس دریافت نشد. Instagram ممکن است دسترسی را محدود کرده باشد؛ "
    "این ربات از ورود، کوکی یا محتوای خصوصی استفاده نمی‌کند."
)


@router.message(F.text)
async def instagram_url(message: Message) -> None:
    try:
        parsed = validate_and_detect_url(message.text or "")
    except ValueError:
        raise SkipHandler
    if parsed.platform != "Instagram":
        raise SkipHandler
    status = await message.answer("در حال بررسی لینک عمومی Instagram…")

    video_info = None
    try:
        video_info = await instagram.inspect(parsed.url)
    except Exception:
        video_info = None

    if video_info is not None and video_info.choices:
        job_id = f"ig{uuid4().hex[:10]}"
        uid = message.from_user.id if message.from_user else 0
        pending.add(job_id, (uid, parsed.url, video_info.choices, video_info.title))
        rows = [[InlineKeyboardButton(text=choice.label, callback_data=f"ig:{job_id}:{choice.key}")]
                for choice in video_info.choices]
        await status.edit_text(
            f"ویدیو شناسایی شد: {escape(video_info.title)}\n"
            "کیفیت را انتخاب کنید. فقط محتوایی را دریافت کنید که اجازهٔ استفاده از آن را دارید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    # Video path found nothing (or failed) — try the image/carousel branch.
    try:
        post = await instagram.fetch_post(parsed.url)
    except ValueError as exc:
        message_text = str(exc) if "پشتیبانی نمی‌شود" in str(exc) else _VIDEO_FAIL_TEXT
        await status.edit_text(message_text + "\n\nاین ربات از ورود، کوکی یا محتوای خصوصی استفاده نمی‌کند.")
        return
    except Exception:
        await status.edit_text(_VIDEO_FAIL_TEXT)
        return

    if post.kind == "video":
        await status.edit_text(
            "این پست ویدیوست اما استخراج ناشناس آن ناموفق بود. Instagram ممکن است دسترسی را محدود کرده باشد؛ "
            "این ربات از ورود، کوکی یا محتوای خصوصی استفاده نمی‌کند."
        )
        return

    uid = message.from_user.id if message.from_user else 0
    job_id = f"igimg{uuid4().hex[:10]}"
    pending_images.add(job_id, (uid, parsed.url, post.title, post.count))
    label = "کاروسل تصویری" if post.kind == "carousel" else "تصویر"
    button_text = f"دریافت {label} ({post.count} مورد)"
    if post.kind == "carousel" and post.count >= 10:
        button_text = f"دریافت کاروسل (۱۰ مورد اول)"
    await status.edit_text(
        f"{label} شناسایی شد: {escape(post.title)}\n"
        "برای دریافت، دکمهٔ زیر را بزنید. فقط محتوایی را دریافت کنید که اجازهٔ استفاده از آن را دارید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, callback_data=f"igimg:{job_id}")
        ]]),
    )


@router.callback_query(F.data.startswith("ig:"))
async def instagram_quality(query: CallbackQuery, download_queue: DownloadQueue, db: Database) -> None:
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
        query, url=url, title=title, choice=choice, platform="Instagram",
        downloader=instagram.download, queue=download_queue, db=db,
        max_bytes=settings.max_file_mb * 1024 * 1024,
    )


@router.callback_query(F.data.startswith("igimg:"))
async def instagram_images(query: CallbackQuery, download_queue: DownloadQueue, db: Database) -> None:
    parts = (query.data or "").split(":", 1)
    entry = pending_images.get(parts[1]) if len(parts) == 2 and parts[1] else None
    if entry is None:
        await query.answer("این انتخاب منقضی شده است؛ لینک را دوباره ارسال کنید.", show_alert=True)
        return
    uid, url, title, count = entry
    if query.from_user.id != uid:
        await query.answer("این درخواست متعلق به کاربر دیگری است.", show_alert=True)
        return
    pending_images.pop(parts[1])
    await query.answer("درخواست ثبت شد.")
    settings = get_settings()
    await start_image_download(
        query, url=url, title=title, platform="Instagram",
        fetcher=instagram.fetch_images, queue=download_queue, db=db,
        max_total_bytes=settings.max_carousel_mb * 1024 * 1024,
    )
