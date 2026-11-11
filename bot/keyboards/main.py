from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="YouTube", callback_data="platform:youtube"),
         InlineKeyboardButton(text="Instagram", callback_data="platform:instagram")],
        [InlineKeyboardButton(text="Pinterest", callback_data="platform:pinterest")],
        [InlineKeyboardButton(text="راهنما", callback_data="help"),
         InlineKeyboardButton(text="دربارهٔ ربات", callback_data="about")],
    ])
