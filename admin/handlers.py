import shutil
from collections import Counter

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.config import get_settings
from db.database import Database
from workers.queue_manager import DownloadQueue

router = Router()


def _admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_set


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="آمار کلی", callback_data="adm:stats"), InlineKeyboardButton(text="کاربران", callback_data="adm:users")],
        [InlineKeyboardButton(text="سلامت سامانه", callback_data="adm:health")],
    ])


@router.message(Command("admin"))
async def admin_home(message: Message) -> None:
    if not message.from_user or not _admin(message.from_user.id):
        await message.answer("دسترسی مجاز نیست.")
        return
    await message.answer("پنل مدیریت", reply_markup=_menu())


@router.callback_query(F.data.startswith("adm:"))
async def admin_actions(query: CallbackQuery, db: Database, download_queue: DownloadQueue) -> None:
    if not _admin(query.from_user.id):
        await query.answer("دسترسی مجاز نیست.", show_alert=True)
        return
    action = (query.data or "").split(":", 1)[1]
    await query.answer()
    if action == "stats":
        data = await db.totals()
        today = await db.platform_breakdown(days=1)
        text = (f"کاربران: {data['users']}\nکاربران فعال: {data['active_users']}\n"
                f"کل درخواست‌های ثبت‌شده: {data['downloads']}\nموفق: {data['success']}\nناموفق: {data['failed']}\n")
        if today:
            text += "۲۴ ساعت گذشته: " + " · ".join(f"{name}: {count}" for name, count in sorted(today.items()))
        else:
            text += "۲۴ ساعت گذشته: دانلودی ثبت نشده است."
        if query.message:
            await query.message.answer(text, reply_markup=_menu())
    elif action == "users":
        users = await db.list_users(limit=15)
        rows = [[InlineKeyboardButton(text=f"{u.telegram_id} · {u.status}", callback_data=f"adm:user:{u.telegram_id}")] for u in users]
        rows.append([InlineKeyboardButton(text="بازگشت", callback_data="adm:home")])
        if query.message:
            await query.message.answer("کاربران اخیر:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif action.startswith("user:"):
        try:
            target_id = int(action.split(":", 1)[1])
        except ValueError:
            return
        users = await db.list_users(limit=1000)
        target = next((u for u in users if u.telegram_id == target_id), None)
        if target and target_id not in get_settings().admin_id_set:
            next_status = "active" if target.status == "blocked" else "blocked"
            label = "رفع مسدودی" if target.status == "blocked" else "مسدود کردن"
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=label, callback_data=f"adm:set:{target_id}:{next_status}")
            ]])
            if query.message:
                await query.message.answer(f"کاربر {target_id} — وضعیت: {target.status}", reply_markup=markup)
    elif action.startswith("set:"):
        try:
            _, raw_id, status = action.split(":", 2)
            target_id = int(raw_id)
            if target_id in get_settings().admin_id_set:
                return
            changed = await db.set_user_status(target_id, status)
        except (ValueError, TypeError):
            return
        if query.message:
            await query.message.answer("وضعیت کاربر به‌روزرسانی شد." if changed else "کاربر پیدا نشد.")
    elif action == "health":
        disk = shutil.disk_usage(".")
        snap = download_queue.snapshot()
        counts = Counter(u.status for u in await db.list_users(limit=1000))
        await query.message.answer(
            f"سلامت فرایند: فعال\n"
            f"فضای دیسک آزاد: {disk.free // (1024**2)} MB\n"
            f"صف دانلود: در انتظار {snap['queued']} · فعال {snap['active']} · ثبت‌شده {snap['total']}\n"
            f"کاربران (۱۰۰۰ اخیر): فعال {counts.get('active', 0)} · مسدود {counts.get('blocked', 0)}"
        )
    elif action == "home" and query.message:
        await query.message.answer("پنل مدیریت", reply_markup=_menu())
