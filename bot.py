
import asyncio
import os
import time
import sqlite3
import logging
import html
from pathlib import Path
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from openai import AsyncOpenAI


# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
ADMIN_ID = 8939059429

AI_BASE_URL = "https://api.groq.com/openai/v1"
AI_MODEL = "openai/gpt-oss-120b"

PAYMENT_DETAILS = os.getenv(
    "PAYMENT_DETAILS",
    "Реквизиты для оплаты уточняйте у администратора."
)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

if not AI_API_KEY:
    raise ValueError("AI_API_KEY не задан")

DB_PATH = os.getenv("DB_PATH") or os.path.join(
    os.getenv("DATA_DIR", "."),
    "vexora.db"
)

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("VEXORA")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

ai = AsyncOpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL,
    timeout=60
)


# ================= DATABASE =================

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")

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

DB.execute("""
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at INTEGER DEFAULT NULL
)
""")

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

DB.execute("""
CREATE TABLE IF NOT EXISTS support_bans (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    reason TEXT DEFAULT ''
)
""")

DB.commit()


# ================= SETTINGS =================

TRIAL_SECONDS = 86400

PLANS = {
    "1": ("1 месяц", 50, 30),
    "2": ("2 месяца", 100, 60),
    "3": ("3 месяца", 150, 90),
    "4": ("4 месяца", 200, 120),
    "6": ("6 месяцев", 300, 180),
    "12": ("12 месяцев", 600, 365),
    "forever": ("Навсегда", 200, None)
}

chat_history = {}
user_modes = {}
support_modes = {}
admin_ticket_modes = {}
awaiting_receipt = {}


# ================= KEYBOARDS =================

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Общение с ИИ", callback_data="chat")],
        [InlineKeyboardButton(text="🧠 Умный ответ", callback_data="smart")],
        [InlineKeyboardButton(text="🖼 Генерация фото", callback_data="photo")],
        [InlineKeyboardButton(text="🎬 Генерация видео", callback_data="video")],
        [InlineKeyboardButton(text="✨ Редактор фото/видео", callback_data="editor")],
        [InlineKeyboardButton(text="🎙 Генерация голоса", callback_data="voice")],
        [InlineKeyboardButton(text="💎 Подписка", callback_data="buy")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ])


def plans_menu():
    buttons = []
    for key, (name, price, days) in PLANS.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"{name} — {price} ₽",
                callback_data=f"plan:{key}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="↩️ Назад", callback_data="back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def support_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать обращение", callback_data="ticket_create")],
        [InlineKeyboardButton(text="📂 Мои обращения", callback_data="ticket_my")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back")]
    ])


def ticket_number(ticket_id):
    return str(ticket_id).zfill(3)


def user_ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Продолжить переписку",
            callback_data=f"ticket_continue:{ticket_id}"
        )],
        [InlineKeyboardButton(
            text="🔒 Закрыть обращение",
            callback_data=f"ticket_close:{ticket_id}"
        )]
    ])


def admin_ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✉️ Ответить",
            callback_data=f"admin_reply:{ticket_id}"
        )],
        [InlineKeyboardButton(
            text="🔒 Закрыть обращение",
            callback_data=f"ticket_close:{ticket_id}"
        )]
    ])


def payment_keyboard(payment_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"payapprove:{payment_id}"
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"payreject:{payment_id}"
        )
    ]])


# ================= USERS =================

def ensure_user(user_id):
    row = DB.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row is None:
        DB.execute(
            "INSERT INTO users(user_id, trial_started) VALUES(?, ?)",
            (user_id, int(time.time()))
        )
        DB.commit()
        return True

    return False


def access_status(user_id):
    row = DB.execute(
        "SELECT trial_started, expires_at, forever FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        return False, "Нет доступа"

    now = int(time.time())

    if row["forever"]:
        return True, "♾ Подписка навсегда"

    if row["expires_at"] > now:
        return True, "💎 Платная подписка активна"

    trial_end = row["trial_started"] + TRIAL_SECONDS

    if now < trial_end:
        hours = (trial_end - now) // 3600
        return True, f"🎁 Пробный период, осталось {hours} ч."

    return False, "🔒 Подписка не активна"


# ================= SUPPORT HELPERS =================

def support_is_banned(user_id):
    row = DB.execute(
        "SELECT expires_at FROM support_bans WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        return False

    expires = row["expires_at"]

    if expires == 0 or expires > int(time.time()):
        return True

    DB.execute("DELETE FROM support_bans WHERE user_id=?", (user_id,))
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


def save_support_message(ticket_id, sender_id, sender_type, text):
    DB.execute(
        """
        INSERT INTO support_messages(
            ticket_id, sender_id, sender_type, message, created_at
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (ticket_id, sender_id, sender_type, text, int(time.time()))
    )
    DB.commit()


# ================= START =================

@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.from_user.id)
    await message.answer(
        "🚀 <b>Добро пожаловать в VEXORA AI!</b>\n\n"
        "Твой персональный искусственный интеллект.\n\n"
        "Выбери функцию:",
        reply_markup=menu()
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "🤖 <b>VEXORA AI</b>\n\n"
        "/start — главное меню\n"
        "/profile — профиль\n"
        "/support — поддержка\n"
        "/cancel — отменить режим\n"
        "/tickets — обращения администратора\n"
        "/ban — блокировка поддержки\n"
        "/unban — разблокировка поддержки"
    )


@dp.message(Command("cancel"))
async def cancel_command(message: Message):
    uid = message.from_user.id
    user_modes.pop(uid, None)
    support_modes.pop(uid, None)
    awaiting_receipt.pop(uid, None)

    if uid == ADMIN_ID:
        admin_ticket_modes.pop(uid, None)

    await message.answer(
        "✅ Текущий режим отменён.",
        reply_markup=menu()
    )


# ================= MENU =================

@dp.callback_query(F.data == "back")
async def back_callback(call: CallbackQuery):
    uid = call.from_user.id
    user_modes.pop(uid, None)
    support_modes.pop(uid, None)

    await call.message.answer(
        "Главное меню VEXORA AI:",
        reply_markup=menu()
    )
    await call.answer()


@dp.callback_query(F.data == "chat")
async def chat_callback(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)

    allowed, status = access_status(uid)
    if not allowed:
        await call.answer("Подписка не активна.", show_alert=True)
        return

    user_modes[uid] = "chat"
    support_modes.pop(uid, None)

    await call.message.answer(
        "💬 <b>Общение с VEXORA AI</b>\n\nНапиши свой вопрос."
    )
    await call.answer()


@dp.callback_query(F.data == "smart")
async def smart_callback(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)

    allowed, status = access_status(uid)
    if not allowed:
        await call.answer("Подписка не активна.", show_alert=True)
        return

    user_modes[uid] = "smart"
    support_modes.pop(uid, None)

    await call.message.answer(
        "🧠 <b>Умный ответ</b>\n\n"
        "Отправь сообщение, на которое нужно придумать ответ."
    )
    await call.answer()


@dp.callback_query(F.data.in_({"photo", "video", "editor", "voice"}))
async def unavailable_features(call: CallbackQuery):
    names = {
        "photo": "🖼 Генерация фото",
        "video": "🎬 Генерация видео",
        "editor": "✨ Редактор фото/видео",
        "voice": "🎙 Генерация голоса"
    }

    await call.answer()
    await call.message.answer(
        f"{names[call.data]}\n\n"
        "Эта функция пока находится в разработке."
    )


# ================= AI =================

async def ask_ai(user_id, prompt, mode):
    history = chat_history.setdefault(user_id, [])

    if mode == "smart":
        system_prompt = (
            "Ты помощник, который придумывает уверенные, остроумные "
            "и достойные ответы на сообщения. Не предлагай угрозы, "
            "травлю или унижение. Дай несколько вариантов ответа."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    else:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты VEXORA AI — дружелюбный, умный и полезный "
                    "ИИ-ассистент. Отвечай на русском, если пользователь "
                    "пишет по-русски."
                )
            }
        ] + history + [{"role": "user", "content": prompt}]

    response = await ai.chat.completions.create(
        model=AI_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1500
    )

    answer = response.choices[0].message.content or "Не удалось получить ответ."

    if mode == "chat":
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": answer})
        chat_history[user_id] = history[-20:]

    return answer


# ================= PROFILE =================

@dp.message(Command("profile"))
async def profile_command(message: Message):
    await show_profile(message, message.from_user.id)


@dp.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery):
    await show_profile(call.message, call.from_user.id)
    await call.answer()


async def show_profile(message, user_id):
    ensure_user(user_id)

    allowed, status = access_status(user_id)

    row = DB.execute(
        "SELECT expires_at, forever FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row["forever"]:
        expiry = "Бессрочно"
    elif row["expires_at"] > int(time.time()):
        expiry = datetime.fromtimestamp(
            row["expires_at"]
        ).strftime("%d.%m.%Y %H:%M")
    else:
        expiry = "Нет платной подписки"

    await message.answer(
        "👤 <b>Твой профиль VEXORA AI</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📊 Статус: {status}\n"
        f"📅 Окончание подписки: {expiry}\n"
        f"🔐 Доступ: {'Активен' if allowed else 'Не активен'}",
        reply_markup=menu()
    )


# ================= SUBSCRIPTIONS =================

@dp.callback_query(F.data == "buy")
async def buy_callback(call: CallbackQuery):
    await call.message.answer(
        "💎 <b>Тарифы VEXORA AI</b>\n\nВыбери подписку:",
        reply_markup=plans_menu()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(call: CallbackQuery):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]

    if key not in PLANS:
        await call.answer("Неизвестный тариф.", show_alert=True)
        return

    name, price, days = PLANS[key]
    awaiting_receipt[uid] = key

    await call.message.answer(
        f"💎 <b>Выбран тариф: {name}</b>\n\n"
        f"💰 Стоимость: {price} ₽\n\n"
        f"Реквизиты:\n<code>{html.escape(PAYMENT_DETAILS)}</code>\n\n"
        "После оплаты отправь фотографию чека."
    )
    await call.answer()


@dp.message(F.photo)
async def receipt_handler(message: Message):
    uid = message.from_user.id

    if uid not in awaiting_receipt:
        return

    key = awaiting_receipt[uid]
    if key not in PLANS:
        awaiting_receipt.pop(uid, None)
        return

    cur = DB.execute(
        """
        INSERT INTO payments(
            user_id, plan_key, photo_file_id, status, created_at
        )
        VALUES(?, ?, ?, 'pending', ?)
        """,
        (uid, key, message.photo[-1].file_id, int(time.time()))
    )
    DB.commit()

    payment_id = cur.lastrowid
    awaiting_receipt.pop(uid, None)

    name, price, days = PLANS[key]

    await message.answer(
        "✅ Чек отправлен администратору.\nОжидай проверки."
    )

    try:
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=(
                f"💳 <b>Новая заявка на оплату</b>\n\n"
                f"Номер: #{payment_id}\n"
                f"Пользователь: <code>{uid}</code>\n"
                f"Тариф: {html.escape(name)}\n"
                f"Сумма: {price} ₽"
            ),
            reply_markup=payment_keyboard(payment_id)
        )
    except Exception:
        logger.exception("Ошибка отправки чека администратору")


@dp.callback_query(F.data.startswith("payapprove:"))
async def approve_payment(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    payment_id = int(call.data.split(":")[1])

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    key = payment["plan_key"]
    if key not in PLANS:
        await call.answer("Неизвестный тариф.", show_alert=True)
        return

    uid = payment["user_id"]
    name, price, days = PLANS[key]
    ensure_user(uid)

    if key == "forever":
        DB.execute(
            "UPDATE users SET forever=1, expires_at=0 WHERE user_id=?",
            (uid,)
        )
    else:
        row = DB.execute(
            "SELECT expires_at FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        start = max(int(time.time()), row["expires_at"])

        DB.execute(
            """
            UPDATE users SET expires_at=?, forever=0
            WHERE user_id=?
            """,
            (start + days * 86400, uid)
        )

    DB.execute(
        "UPDATE payments SET status='approved' WHERE id=?",
        (payment_id,)
    )
    DB.commit()

    await call.message.edit_caption(
        caption=(
            f"✅ <b>Оплата подтверждена</b>\n"
            f"Заявка #{payment_id}\n"
            f"Пользователь: <code>{uid}</code>\n"
            f"Тариф: {html.escape(name)}"
        )
    )

    try:
        await bot.send_message(
            uid,
            f"🎉 <b>Оплата подтверждена!</b>\n"
            f"Тариф: {html.escape(name)}\n"
            "Подписка активирована."
        )
    except Exception:
        logger.exception("Ошибка уведомления пользователя")

    await call.answer("Подписка активирована!")


@dp.callback_query(F.data.startswith("payreject:"))
async def reject_payment(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    payment_id = int(call.data.split(":")[1])

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    DB.execute(
        "UPDATE payments SET status='rejected' WHERE id=?",
        (payment_id,)
    )
    DB.commit()

    await call.message.edit_caption(
        caption=f"❌ Заявка #{payment_id} отклонена."
    )

    try:
        await bot.send_message(
            payment["user_id"],
            "❌ Заявка на оплату отклонена. Обратись в поддержку."
        )
    except Exception:
        logger.exception("Ошибка уведомления пользователя")

    await call.answer("Заявка отклонена.")


# ================= SUPPORT MENU =================

@dp.message(Command("support"))
async def support_command(message: Message):
    uid = message.from_user.id

    if support_is_banned(uid):
        await message.answer("🚫 Доступ к поддержке ограничен.")
        return

    await message.answer(
        "🆘 <b>Поддержка VEXORA AI</b>",
        reply_markup=support_menu()
    )


@dp.callback_query(F.data == "support")
async def support_callback(call: CallbackQuery):
    if support_is_banned(call.from_user.id):
        await call.answer("Доступ ограничен.", show_alert=True)
        return

    await call.message.answer(
        "🆘 <b>Поддержка VEXORA AI</b>",
        reply_markup=support_menu()
    )
    await call.answer()


# ================= CREATE TICKET =================

@dp.callback_query(F.data == "ticket_create")
async def create_ticket(call: CallbackQuery):
    uid = call.from_user.id

    if support_is_banned(uid):
        await call.answer("Доступ ограничен.", show_alert=True)
        return

    existing = get_open_ticket(uid)

    if existing:
        ticket_id = existing["id"]
        await call.message.answer(
            f"У тебя уже есть обращение №{ticket_number(ticket_id)}.",
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

