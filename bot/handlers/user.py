from functools import lru_cache

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main import main_menu
from core.config import get_settings
from core.security import validate_and_detect_url
from db.database import Database

router = Router()


@lru_cache(maxsize=1)
def _max_mb() -> int:
    # Read lazily: importing this module must not require a configured .env.
    return get_settings().max_file_mb


def welcome_text() -> str:
    return (
        "سلام! به ربات دریافت رسانه خوش آمدید.\n\n"
        "لینک عمومی YouTube، Instagram یا Pinterest را بفرستید؛ برای ویدیو کیفیت را انتخاب می‌کنید و "
        "برای پست‌های تصویری Instagram، عکس یا کاروسل مستقیم دریافت می‌شود.\n\n"
        f"سقف حجم هر فایل: {_max_mb()} مگابایت · کاروسل تصویری Instagram پشتیبانی می‌شود\n\n"
        "فقط محتوایی را پردازش کنید که اجازهٔ دریافت و استفاده از آن را دارید. "
        "محتوای خصوصی، نیازمند ورود یا دارای محدودیت دسترسی پشتیبانی نمی‌شود."
    )


def help_text() -> str:
    return (
        "راهنما\n"
        "۱) لینک HTTPS عمومی از YouTube، Instagram یا Pinterest را ارسال کنید.\n"
        "۲) ربات پلتفرم را تشخیص می‌دهد: برای ویدیو کیفیت‌های موجود پیشنهاد می‌شود و برای پست تصویری "
        "Instagram دکمهٔ دریافت عکس/کاروسل نمایش داده می‌شود.\n"
        "۳) انتخاب را تأیید کنید؛ درخواست وارد صف دانلود می‌شود و پیشرفت آن نمایش داده می‌شود.\n"
        "۴) تا پیش از پایان پردازش می‌توانید با دکمهٔ «لغو دانلود» درخواست را لغو کنید.\n\n"
        f"محدودیت‌ها: هر فایل حداکثر {_max_mb()} مگابایت؛ هر کاربر یک دانلود فعال؛ حداکثر ۱۰ تصویر در هر کاروسل.\n"
        "از ارسال لینک‌هایی که اجازهٔ استفاده از آن‌ها را ندارید خودداری کنید."
    )


@router.message(CommandStart())
async def start(message: Message, db: Database) -> None:
    if message.from_user:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(welcome_text(), reply_markup=main_menu())


@router.message(F.text)
async def receive_text(message: Message, db: Database) -> None:
    # Platform routers run first for supported links and consume them; this
    # handler only sees non-link text and invalid links.
    if message.from_user:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = (message.text or "").strip()
    try:
        validate_and_detect_url(text)
    except ValueError as exc:
        await message.answer(str(exc) + "\n\nبرای راهنما /start را بزنید.")
        return
    await message.answer("لینک شناسایی شد اما پردازش آن آغاز نشد؛ لطفاً لینک را دوباره ارسال کنید.")


@router.callback_query(F.data == "help")
async def help_callback(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await query.message.answer(help_text())


@router.callback_query(F.data == "about")
async def about_callback(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await query.message.answer(
            "ربات فارسی دریافت ویدیوی عمومی از YouTube، Instagram و Pinterest، عکس و کاروسل تصویری Instagram، "
            "با انتخاب کیفیت، صف دانلود و امکان لغو. فقط محتوای مجاز؛ بدون ورود، کوکی یا محتوای خصوصی."
        )


@router.callback_query(F.data.startswith("platform:"))
async def platform_callback(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await query.message.answer("لینک عمومی را همین‌جا ارسال کنید تا خودکار شناسایی و پردازش شود.")
