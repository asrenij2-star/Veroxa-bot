
import asyncio
import time
import sqlite3
import logging
import html

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
ADMIN_ID = 8939059429

AI_BASE_URL = "https://api.groq.com/openai/v1"
AI_MODEL = "openai/gpt-oss-120b"

PAYMENT_DETAILS = "Реквизиты для оплаты"

TRIAL_SECONDS = 24 * 60 * 60


if BOT_TOKEN.startswith("ВСТАВЬ_"):
    raise ValueError("Вставь Telegram-токен!")

if AI_API_KEY.startswith("ВСТАВЬ_"):
    raise ValueError("Вставь Groq API-ключ!")


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

# Тикеты поддержки

DB.execute("""
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at INTEGER DEFAULT NULL
)
""")

# Сообщения внутри тикетов

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

# Баны поддержки

DB.execute("""
CREATE TABLE IF NOT EXISTS support_bans (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    reason TEXT DEFAULT ''
)
""")

DB.commit()


# ==================================
# ТАРИФЫ И СОСТОЯНИЯ
# ==================================

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

# Режимы переписки с поддержкой
support_modes = {}


# ==================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================

def is_admin(user_id):
    return user_id == ADMIN_ID


def ticket_number(ticket_id):
    return f"{ticket_id:03d}"


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
        FROM users
        WHERE user_id=?
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


def get_ticket(ticket_id):

    return DB.execute(
        "SELECT * FROM support_tickets WHERE id=?",
        (ticket_id,)
    ).fetchone()


def get_open_ticket(user_id):

    return DB.execute(
        """
        SELECT * FROM support_tickets
        WHERE user_id=? AND status='open'
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,)
    ).fetchone()


def support_ban_status(user_id):

    row = DB.execute(
        "SELECT expires_at FROM support_bans WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        return False, 0

    expires = row["expires_at"]

    if expires == 0:
        return True, 0

    if expires > int(time.time()):
        return True, expires

    DB.execute(
        "DELETE FROM support_bans WHERE user_id=?",
        (user_id,)
    )
    DB.commit()

    return False, 0


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


def support_keyboard(ticket_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"ticket_close:{ticket_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📨 Мои обращения",
                    callback_data="my_tickets"
                )
            ]
        ]
    )


# ==================================
# ГЛАВНОЕ МЕНЮ
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
# ИСКУССТВЕННЫЙ ИНТЕЛЛЕКТ
# ==================================

async def ask_ai(user_id, user_text, system_prompt):

    history = chat_history.setdefault(user_id, [])

    messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]

    messages.extend(history[-10:])

    messages.append({
        "role": "user",
        "content": user_text
    })

    response = await ai.chat.completions.create(
        model=AI_MODEL,
        messages=messages,
        temperature=0.8,
        max_tokens=700
    )

    answer = (
        response.choices[0].message.content
        or "Не получилось сформировать ответ."
    )

    history.extend([
        {
            "role": "user",
            "content": user_text
        },
        {
            "role": "assistant",
            "content": answer
        }
    ])

    chat_history[user_id] = history[-20:]

    return answer


# ==================================
# СТАРТ И МЕНЮ
# ==================================

@dp.message(CommandStart())
async def start(message: Message):

    is_new = ensure_user(message.from_user.id)

    intro = (
        "🎁 Вам доступен пробный период на 24 часа."
        if is_new else
        "С возвращением!"
    )

    await message.answer(
        f"🟠 <b>VEXORA AI</b>\n\n"
        f"{intro}\n\n"
        "Выберите функцию в меню.",
        reply_markup=menu()
    )


@dp.message(Command("menu"))
async def show_menu(message: Message):

    support_modes.pop(message.from_user.id, None)

    await message.answer(
        "Главное меню VEXORA AI:",
        reply_markup=menu()
    )


# ==================================
# ПОКУПКА ПОДПИСКИ
# ==================================

@dp.callback_query(F.data == "buy")
async def buy(call: CallbackQuery):

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{name} — {price} ₽",
                callback_data=f"plan:{key}"
            )
        ]
        for key, (name, price, days) in PLANS.items()
    ]

    await call.message.answer(
        "💎 <b>ТАРИФЫ VEXORA AI</b>\n"
        "Выберите подписку:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await call.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(call: CallbackQuery):

    key = call.data.split(":", 1)[1]

    if key not in PLANS:
        await call.answer(
            "Неизвестный тариф.",
            show_alert=True
        )
        return

    selected_plan[call.from_user.id] = key

    name, price, days = PLANS[key]

    await call.message.answer(
        f"💳 <b>Оплата подписки</b>\n"
        f"Тариф: {name}\n"
        f"Сумма: {price} ₽\n\n"
        f"{html.escape(PAYMENT_DETAILS)}\n\n"
        "После оплаты отправьте фото чека. "
        "Администратор проверит его вручную."
    )

    await call.answer()


# ==================================
# ПОЛУЧЕНИЕ ЧЕКА
# ==================================

@dp.message(F.photo)
async def payment_photo(message: Message):

    uid = message.from_user.id

    key = selected_plan.get(uid)

    if not key:
        await message.answer(
            "Сначала выберите тариф в меню."
        )
        return

    name, price, days = PLANS[key]

    cur = DB.execute(
        """
        INSERT INTO payments(
            user_id,
            plan_key,
            photo_file_id,
            created_at
        )
        VALUES(?, ?, ?, ?)
        """,
        (
            uid,
            key,
            message.photo[-1].file_id,
            int(time.time())
        )
    )

    payment_id = cur.lastrowid
    DB.commit()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{payment_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{payment_id}"
                )
            ]
        ]
    )

    try:
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=(
                f"🧾 <b>Заявка #{payment_id}</b>\n"
                f"Пользователь: <code>{uid}</code>\n"
                f"Тариф: {html.escape(name)}\n"
                f"Сумма: {price} ₽"
            ),
            reply_markup=keyboard
        )

        await message.answer(
            "📨 Чек отправлен администратору!"
        )

    except Exception:
        logging.exception("Не удалось отправить чек администратору")
        await message.answer(
            "Не удалось отправить чек. Попробуйте позже."
        )


# ==================================
# ОДОБРЕНИЕ ОПЛАТЫ
# ==================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_payment(call: CallbackQuery):

    if not is_admin(call.from_user.id):
        await call.answer(
            "Недостаточно прав.",
            show_alert=True
        )
        return

    try:
        payment_id = int(call.data.split(":")[1])
    except ValueError:
        await call.answer("Неверный номер заявки.", show_alert=True)
        return

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer(
            "Заявка уже обработана.",
            show_alert=True
        )
        return

    uid = payment["user_id"]
    key = payment["plan_key"]

    if key not in PLANS:
        await call.answer("Неизвестный тариф.", show_alert=True)
        return

    name, price, days = PLANS[key]

    ensure_user(uid)

    if key == "forever":

        DB.execute(
            "UPDATE users SET forever=1 WHERE user_id=?",
            (uid,)
        )

    else:

        row = DB.execute(
            "SELECT expires_at FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        start_at = max(
            int(time.time()),
            row["expires_at"]
        )

        DB.execute(
            "UPDATE users SET expires_at=? WHERE user_id=?",
            (
                start_at + days * 86400,
                uid
            )
        )

    DB.execute(
        "UPDATE payments SET status='approved' WHERE id=?",
        (payment_id,)
    )

    DB.commit()

    selected_plan.pop(uid, None)

    try:
        await bot.send_message(
            uid,
            f"🎉 Оплата подтверждена!\n"
            f"Подписка «{html.escape(name)}» активирована."
        )
    except Exception:
        logging.exception("Не удалось уведомить пользователя")

    await call.message.edit_caption(
        f"✅ Заявка #{payment_id} одобрена."
    )

    await call.answer("Подписка выдана.")


# ==================================
# ОТКЛОНЕНИЕ ОПЛАТЫ
# ==================================

@dp.callback_query(F.data.startswith("reject:"))
async def reject_payment(call: CallbackQuery):

    if not is_admin(call.from_user.id):
        await call.answer(
            "Недостаточно прав.",
            show_alert=True
        )
        return

    try:
        payment_id = int(call.data.split(":")[1])
    except ValueError:
        await call.answer("Неверный номер заявки.", show_alert=True)
        return

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer(
            "Заявка уже обработана.",
            show_alert=True
        )
        return

    DB.execute(
        "UPDATE payments SET status='rejected' WHERE id=?",
        (payment_id,)
    )

    DB.commit()

    try:
        await bot.send_message(
            payment["user_id"],
            "❌ Заявка отклонена. "
            "Свяжитесь с администратором."
        )
    except Exception:
        logging.exception("Не удалось уведомить пользователя")

    await call.message.edit_caption(
        f"❌ Заявка #{payment_id} отклонена."
    )

    await call.answer("Заявка отклонена.")


# ==================================
# ПРОФИЛЬ
# ==================================

@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):

    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    row = DB.execute(
        """
        SELECT trial_started, expires_at
        FROM users
        WHERE user_id=?
        """,
        (call.from_user.id,)
    ).fetchone()

    remaining = max(
        0,
        row["trial_started"] + TRIAL_SECONDS - int(time.time())
    )

    if status == "🎁 Пробный период":
        status += f"\nОсталось примерно {remaining // 3600} ч."

    elif row["expires_at"] > int(time.time()):

        expiry = datetime.fromtimestamp(
            row["expires_at"],
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        status += f"\nДо: {expiry}"

    await call.message.answer(
        f"👤 <b>Профиль</b>\n"
        f"ID: <code>{call.from_user.id}</code>\n"
        f"Статус: {status}"
    )

    await call.answer()


# ==================================
# СВОБОДНОЕ ОБЩЕНИЕ
# ==================================

@dp.callback_query(F.data == "chat")
async def enable_chat(call: CallbackQuery):

    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    if not allowed:

        await call.message.answer(
            "🔒 Пробный период закончился. "
            "Оформите подписку."
        )

    else:

        support_modes.pop(call.from_user.id, None)
        user_modes[call.from_user.id] = "chat"

        chat_history.pop(call.from_user.id, None)

        await call.message.answer(
            "💬 Режим общения включён!\n"
            "Просто напишите мне сообщение.\n"
            "Для очистки истории используйте /clear."
        )

    await call.answer()


# ==================================
# УМНЫЙ ОТВЕТ НА ОСКОРБЛЕНИЯ
# ==================================

@dp.callback_query(F.data == "smart")
async def smart_mode(call: CallbackQuery):

    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    if not allowed:

        await call.message.answer(
            "🔒 Оформите подписку, чтобы пользоваться ИИ."
        )

    else:

        support_modes.pop(call.from_user.id, None)
        user_modes[call.from_user.id] = "smart"

        chat_history.pop(call.from_user.id, None)

        await call.message.answer(
            "🧠 Пришлите оскорбление или неприятное сообщение.\n"
            "Я предложу три варианта ответа:\n"
            "1. Спокойный\n"
            "2. Остроумный\n"
            "3. Уверенный"
        )

    await call.answer()


# ==================================
# ОЧИСТКА ИСТОРИИ
# ==================================

@dp.message(Command("clear"))
async def clear_history(message: Message):

    chat_history.pop(message.from_user.id, None)

    await message.answer("История общения очищена.")


# ==================================
# ПОДДЕРЖКА — МЕНЮ
# ==================================

@dp.message(Command("support"))
async def support_command(message: Message):

    await show_support_menu(message)


async def show_support_menu(message: Message):

    uid = message.from_user.id

    banned, expires = support_ban_status(uid)

    if banned:

        if expires == 0:
            text = "🚫 Вы навсегда заблокированы в поддержке."
        else:
            remaining = max(0, expires - int(time.time()))
            text = (
                "🚫 Вы временно заблокированы в поддержке.\n"
                f"Осталось секунд: {remaining}"
            )

        await message.answer(text)
        return

    ticket = get_open_ticket(uid)

    if ticket:

        support_modes[uid] = ticket["id"]

        await message.answer(
            f"🆘 У вас открыто обращение "
            f"№{ticket_numb
