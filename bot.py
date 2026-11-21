import asyncio
import time
import sqlite3
import logging
import html
import os

from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from openai import AsyncOpenAI


# ==================================
# VEXORA AI — НАСТРОЙКИ
# ==================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

ADMIN_ID = 8939059429

AI_BASE_URL = "https://api.groq.com/openai/v1"
AI_MODEL = "openai/gpt-oss-120b"

PAYMENT_DETAILS = "Реквизиты для оплаты"

if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN!")

if not AI_API_KEY:
    raise ValueError("Не задан AI_API_KEY!")


# ==================================
# ЗАПУСК
# ==================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

ai = AsyncOpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL
)


# ==================================
# БАЗА ДАННЫХ
# ==================================

DB = sqlite3.connect("vexora.db")
DB.row_factory = sqlite3.Row

DB.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    trial_started INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    forever INTEGER NOT NULL DEFAULT 0
)
""")

DB.execute("""
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_key TEXT NOT NULL,
    photo_file_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
)
""")

# Обращения пользователей
DB.execute("""
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at INTEGER DEFAULT NULL
)
""")

# Сообщения поддержки
DB.execute("""
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL
)
""")

# Блокировки поддержки
DB.execute("""
CREATE TABLE IF NOT EXISTS support_bans (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    reason TEXT DEFAULT ''
)
""")

DB.commit()


# ==================================
# ТАРИФЫ
# ==================================

TRIAL_SECONDS = 24 * 60 * 60

PLANS = {
    "1": ("1 месяц", 50, 30),
    "2": ("2 месяца", 100, 60),
    "3": ("3 месяца", 150, 90),
    "4": ("4 месяца", 200, 120),
    "6": ("6 месяцев", 300, 180),
    "12": ("12 месяцев", 600, 365),
    "forever": ("Навсегда", 200, None),
}

chat_history = {}
selected_plan = {}
user_modes = {}

# Режим переписки поддержки
support_modes = {}

# Режим ответа администратора
admin_ticket_modes = {}


# ==================================
# МЕНЮ
# ==================================

def menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Общение с ИИ",
                    callback_data="chat"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧠 Ответ на оскорбление",
                    callback_data="smart"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Генерация фото",
                    callback_data="photo"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎬 Генерация видео",
                    callback_data="video"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Редактор фото/видео",
                    callback_data="editor"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎙 Генерация голоса",
                    callback_data="voice"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 Купить подписку",
                    callback_data="buy"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Мой профиль",
                    callback_data="profile"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Поддержка",
                    callback_data="support"
                )
            ]
        ]
    )


# ==================================
# ПОЛЬЗОВАТЕЛИ И ДОСТУП
# ==================================

def ensure_user(user_id):

    row = DB.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row is None:

        DB.execute(
            """
            INSERT INTO users(user_id, trial_started)
            VALUES(?, ?)
            """,
            (user_id, int(time.time()))
        )

        DB.commit()
        return True

    return False


def access_status(user_id):

    row = DB.execute(
        """
        SELECT trial_started, expires_at, forever
        FROM users WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not row:
        return False, "Нет доступа"

    now = int(time.time())

    if row["forever"]:
        return True, "♾ Подписка навсегда"

    if row["expires_at"] > now:
        return True, "💎 Платная подписка активна"

    if now < row["trial_started"] + TRIAL_SECONDS:
        return True, "🎁 Пробный период"

    return False, "🔒 Подписка не активна"


# ==================================
# СИСТЕМА ПОДДЕРЖКИ
# ==================================

def support_is_banned(user_id):

    row = DB.execute(
        "SELECT expires_at FROM support_bans WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        return False

    expires = row["expires_at"]

    # 0 означает бессрочную блокировку
    if expires == 0:
        return True

    if expires > int(time.time()):
        return True

    # Срок блокировки закончился
    DB.execute(
        "DELETE FROM support_bans WHERE user_id=?",
        (user_id,)
    )
    DB.commit()

    return False


def get_open_ticket(user_id):

    return DB.execute(
        """
        SELECT * FROM support_tickets
        WHERE user_id=? AND status='open'
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,)
    ).fetchone()


def ticket_number(ticket_id):

    return str(ticket_id).zfill(3)


def save_support_message(ticket_id, sender_id, sender_type, text):

    DB.execute(
        """
        INSERT INTO support_messages(
            ticket_id,
            sender_id,
            sender_type,
            message,
            created_at
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            sender_id,
            sender_type,
            text,
            int(time.time())
        )
    )

    DB.commit()


def support_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Создать обращение",
                    callback_data="ticket_create"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 Мои обращения",
                    callback_data="ticket_my"
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Главное меню",
                    callback_data="chat"
                )
            ]
        ]
    )


def user_ticket_keyboard(ticket_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Продолжить переписку",
                    callback_data=f"ticket_continue:{ticket_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"ticket_close:{ticket_id}"
                )
            ]
        ]
    )


def admin_ticket_keyboard(ticket_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Ответить",
                    callback_data=f"admin_reply:{ticket_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"ticket_close:{ticket_id}"
                )
            ]
        ]
    )


# ==================================
# ОТКРЫТИЕ ПОДДЕРЖКИ
# ==================================

@dp.message(Command("support"))
async def support_command(message: Message):

    uid = message.from_user.id

    if support_is_banned(uid):
        await message.answer(
            "🚫 Доступ к поддержке ограничен."
        )
        return

    await message.answer(
        "🆘 <b>Поддержка VEXORA AI</b>\n\n"
        "Здесь ты можешь обратиться к администратору.\n"
        "Создай обращение или посмотри свои предыдущие обращения.",
        reply_markup=support_menu()
    )


@dp.callback_query(F.data == "support")
async def support_callback(call: CallbackQuery):

    uid = call.from_user.id

    if support_is_banned(uid):
        await call.answer(
            "🚫 Доступ к поддержке ограничен.",
            show_alert=True
        )
        return

    await call.message.answer(
        "🆘 <b>Поддержка VEXORA AI</b>\n\n"
        "Выбери нужное действие:",
        reply_markup=support_menu()
    )

    await call.answer()


# ==================================
# СОЗДАНИЕ ОБРАЩЕНИЯ
# ==================================

@dp.callback_query(F.data == "ticket_create")
async def create_ticket(call: CallbackQuery):

    uid = call.from_user.id

    if support_is_banned(uid):
        await call.answer(
            "🚫 Доступ к поддержке ограничен.",
            show_alert=True
        )
        return

    existing = get_open_ticket(uid)

    if existing:

        ticket_id = existing["id"]

        await call.message.answer(
            f"У тебя уже есть открытое обращение "
            f"№{ticket_number(ticket_id)}.",
            reply_markup=user_ticket_keyboard(ticket_id)
        )

        await call.answer()
        return

    cur = DB.execute(
        """
        INSERT INTO support_tickets(user_id, status, created_at)
        VALUES(?, 'open', ?)
        """,
        (uid, int(time.time()))
    )

    DB.commit()

    ticket_id = cur.lastrowid

    support_modes[uid] = ticket_id

    await call.message.answer(
        f"✅ Обращение №{ticket_number(ticket_id)} создано!\n\n"
        "✍️ Напиши своё сообщение. "
        "Оно будет отправлено администратору.\n\n"
        "Чтобы закончить переписку, используй кнопку закрытия."
    )

    try:

        await bot.send_message(
            ADMIN_ID,
            f"🆘 <b>Новое обращение №{ticket_number(ticket_id)}</b>\n\n"
            f"👤 Пользователь: <code>{uid}</code>\n"
            f"🕒 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            reply_markup=admin_ticket_keyboard(ticket_id)
        )

    except Exception:

        logging.exception("Не удалось уведомить администратора")

        await call.message.answer(
            "⚠️ Обращение создано, но не удалось уведомить администратора. "
            "Попробуй позже."
        )

    await call.answer()


# ==================================
# МОИ ОБРАЩЕНИЯ
# ==================================

@dp.callback_query(F.data == "ticket_my")
async def my_tickets(call: CallbackQuery):

    uid = call.from_user.id

    rows = DB.execute(
        """
        SELECT * FROM support_tickets
        WHERE user_id=?
        ORDER BY id DESC LIMIT 10
        """,
        (uid,)
    ).fetchall()

    if not rows:

        await call.message.answer(
            "📂 У тебя пока нет обращений."
        )

        await call.answer()
        return

    for row in rows:

        ticket_id = row["id"]

        status = (
            "🟢 Открыто"
            if row["status"] == "open"
            else "🔒 Закрыто"
        )

        keyboard = None

        if row["status"] == "open":

            keyboard = user_ticket_keyboard(ticket_id)

        await call.message.answer(
            f"📂 <b>Обращение №{ticket_number(ticket_id)}</b>\n"
            f"Статус: {status}",
            reply_markup=keyboard
        )

    await call.answer()


# ==================================
# ПРОДОЛЖЕНИЕ ПЕРЕПИСКИ
# ==================================

@dp.callback_query(F.data.startswith("ticket_continue:"))
async def continue_ticket(call: CallbackQuery):

    ticket_id = int(call.data.split(":")[1])

    ticket = DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()

    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer("Обращение не найдено.", show_alert=True)
        return

    if ticket["status"] != "open":
        await call.answer("Обращение закрыто.", show_alert=True)
        return

    if support_is_banned(call.from_user.id):
        await call.answer("Доступ к поддержке ограничен.", show_alert=True)
        return

    support_modes[call.from_user.id] = ticket_id

    await call.message.answer(
        f"💬 Переписка по обращению №{ticket_number(ticket_id)} включена.\n"
        "Напиши сообщение."
    )

    await call.answer()


# ==================================
# ЗАКРЫТИЕ ОБРАЩЕНИЯ
# ==================================

@dp.callback_query(F.data.startswith("ticket_close:"))
async def close_ticket_callback(call: CallbackQuery):

    ticket_id = int(call.data.split(":")[1])

    ticket = DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()

    if not ticket:
        await call.answer("Обращение не найдено.", show_alert=True)
        return

    if (
        call.from_user.id != ADMIN_ID
        and call.from_user.id != ticket["user_id"]
    ):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    DB.execute(
        """
        UPDATE support_tickets
        SET status='closed', closed_at=?
        WHERE id=?
        """,
        (int(time.time()), ticket_id)
    )

    DB.commit()

    support_modes.pop(ticket["user_id"], None)
    admin_ticket_modes.pop(ADMIN_ID, None)

    await call.message.answer(
        f"🔒 Обращение №{ticket_number(ticket_id)} закрыто."
    )

    if call.from_user.id == ADMIN_ID:

        try:
            await bot.send_message(
                ticket["user_id"],
                f"🔒 Твоё обращение №{ticket_number(ticket_id)} закрыто администратором."
            )
        except Exception:
            pass

    else:

        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔒 Пользователь закрыл обращение №{ticket_number(ticket_id)}."
            )
        except Exception:
            pass

    await call.answer("Обращение закрыто.")


# ==================================
# ОТВЕТ АДМИНИСТРАТОРА
# ==================================

@dp.callback_query(F.data.startswith("admin_reply:"))
async def admin_reply_callback(call: CallbackQuery):

    if call.from_user.id != ADMIN_ID:
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    ticket_id = int(call.data.split(":")[1])

    ticket = DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()

    if not ticket or ticket["status"] != "open":
        await call.answer("Обращение закрыто или не найдено.", show_alert=True)
        return

    admin_ticket_modes[ADMIN_ID] = ticket_id

    await call.message.answer(
        f"✉️ Ответ на обращение №{ticket_number(ticket_id)}.\n\n"
        "Напиши сообщение пользователю.\n"
        "Для отмены используй /cancel."
    )

    await call.answer()


# ==================================
# КОМАНДЫ АДМИНИСТРАТОРА
# ==================================

@dp.message(Command("tickets"))
async def admin_tickets(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    rows = DB.execute(
        """
        SELECT * FROM support_tickets
        ORDER BY id DESC LIMIT 30
        """
    ).fetchall()

    if not rows:
        await message.answer("Обращений пока нет.")
        return

    for row in rows:

        status = "🟢 Открыто" if row["status"] == "open" else "🔒 Закрыто"

        await message.answer(
            f"📂 <b>Обращение №{ticket_number(row['id'])}</b>\n"
            f"Пользователь: <code>{row['user_id']}</code>\n"
            f"Статус: {status}",
            reply_markup=(
                admin_ticket_keyboard(row["id"])
                if row["status"] == "open"
                else None
            )
        )


@dp.message(Command("ticket"))
async def admin_ticket_info(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /ticket 001")
        return

    ticket_id = int(parts[1])

    ticket = DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()

    if not ticket:
        await message.answer("Обращение не найдено.")
        return

    rows = DB.execute(
        """
        SELECT * FROM support_messages
        WHERE ticket_id=?
        ORDER BY id DESC LIMIT 15
        """,
        (ticket_id,)
    ).fetchall()

    result = (
        f"📂 <b>Обращение №{ticket_number(ticket_id)}</b>\n"
        f"Пользователь: <code>{ticket['user_id']}</code>\n"
        f"Статус: {ticket['status']}\n\n"
        "<b>Последние сообщения:</b>\n"
    )

    for row in reversed(rows):

        sender = (
            "👤 Пользователь"
            if row["sender_type"] == "user"
            else "🛠 Администратор"
        )

        result += (
            f"\n{sender}:\n"
            f"{html.escape(row['message'][:700])}\n"
        )

    await message.answer(result)


@dp.message(Command("reply"))
async def admin_reply_command(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Использование: /reply 001 Текст ответа")
        return

    ticket_id = int(parts[1])
    text = parts[2]

    ticket = DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()

    if not ticket or ticket["status"] != "open":
        await message.answer("Обращение не найдено или закрыто.")
        return

    try:

        await bot.send_message(
            ticket["user_id"],
            f"🛠 <b>Ответ поддержки VEXORA AI</b>\n\n"
            f"{html.escape(text)}"
        )

        save_support_message(
            ticket_id,
            ADMIN_ID,
            "admin",
            text
        )

        await message.answer("✅ Ответ отправлен!")

    except Exception:

        logging.exception("Ошибка отправки ответа")

        await message.answer(
            "⚠️ Не удалось отправить сообщение пользователю."
        )


@dp.message(Command("close_ticket"))
async def admin_close_ticket(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /close_ticket 001")
        return

