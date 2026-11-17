import asyncio
import os
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
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from openai import AsyncOpenAI


# ==================================
# НАСТРОЙКИ (из переменных окружения)
# ==================================
# На Bothost добавь в "Переменные окружения":
#   BOT_TOKEN   — токен от @BotFather
#   AI_API_KEY  — ключ Groq
#   ADMIN_ID    — твой Telegram ID (число)
# Необязательно: PAYMENT_DETAILS, AI_MODEL, AI_BASE_URL, DB_PATH

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("API_TOKEN", "")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
AI_MODEL = os.environ.get("AI_MODEL", "openai/gpt-oss-120b")
PAYMENT_DETAILS = os.environ.get("PAYMENT_DETAILS", "Реквизиты для оплаты")
DB_PATH = os.environ.get("DB_PATH", "vexora.db")

if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")
if not AI_API_KEY:
    raise ValueError("Не задана переменная окружения AI_API_KEY")
if not ADMIN_ID:
    raise ValueError("Не задана переменная окружения ADMIN_ID")


# ==================================
# ЗАПУСК
# ==================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

ai = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)


# ==================================
# БАЗА ДАННЫХ
# ==================================

DB = sqlite3.connect(DB_PATH)
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


# ==================================
# МЕНЮ
# ==================================

def menu():
    def btn(text, data):
        return [InlineKeyboardButton(text=text, callback_data=data)]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            btn("💬 Общение с ИИ", "chat"),
            btn("🧠 Ответ на оскорбление", "smart"),
            btn("🖼 Генерация фото", "photo"),
            btn("🎬 Генерация видео", "video"),
            btn("✨ Редактор фото/видео", "editor"),
            btn("🎙 Генерация голоса", "voice"),
            btn("💎 Купить подписку", "buy"),
            btn("👤 Мой профиль", "profile"),
        ]
    )


# ==================================
# ПОЛЬЗОВАТЕЛИ
# ==================================

def ensure_user(user_id):
    row = DB.execute(
        "SELECT user_id FROM users WHERE user_id=?", (user_id,)
    ).fetchone()

    if row is None:
        DB.execute(
            "INSERT INTO users(user_id, trial_started) VALUES(?, ?)",
            (user_id, int(time.time())),
        )
        DB.commit()
        return True

    return False


def access_status(user_id):
    row = DB.execute(
        "SELECT trial_started, expires_at, forever FROM users WHERE user_id=?",
        (user_id,),
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
# ИИ
# ==================================

async def ask_ai(user_id, user_text, system_prompt):
    history = chat_history.setdefault(user_id, [])

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    response = await ai.chat.completions.create(
        model=AI_MODEL,
        messages=messages,
        temperature=0.8,
        max_tokens=700,
    )

    answer = (
        response.choices[0].message.content
        or "Не получилось сформировать ответ."
    )

    history.extend([
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": answer},
    ])

    chat_history[user_id] = history[-20:]

    return answer


# ==================================
# СТАРТ / МЕНЮ
# ==================================

@dp.message(CommandStart())
async def start(message: Message):
    is_new = ensure_user(message.from_user.id)

    intro = (
        "🎁 Вам доступен пробный период на 24 часа."
        if is_new
        else "С возвращением!"
    )

    await message.answer(
        f"🟠 <b>VEXORA AI</b>\n\n{intro}\n\nВыберите функцию в меню.",
        reply_markup=menu(),
    )


@dp.message(Command("menu"))
async def show_menu(message: Message):
    await message.answer("Главное меню VEXORA AI:", reply_markup=menu())


# ==================================
# АДМИН: /activate
# ==================================
# /activate            — выдать себе подписку навсегда
# /activate 123456789  — выдать подписку навсегда пользователю с этим ID

@dp.message(Command("activate"))
async def activate(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split()

    try:
        target = int(parts[1]) if len(parts) > 1 else message.from_user.id
    except ValueError:
        await message.answer("Использование: /activate или /activate ID")
        return

    ensure_user(target)
    DB.execute("UPDATE users SET forever=1 WHERE user_id=?", (target,))
    DB.commit()

    await message.answer(
        f"✅ Подписка навсегда активирована для <code>{target}</code>."
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
                callback_data=f"plan:{key}",
            )
        ]
        for key, (name, price, days) in PLANS.items()
    ]

    await call.message.answer(
        "💎 <b>ТАРИФЫ VEXORA AI</b>\nВыберите подписку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )

    await call.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(call: CallbackQuery):
    key = call.data.split(":", 1)[1]

    if key not in PLANS:
        await call.answer("Неизвестный тариф.", show_alert=True)
        return

    selected_plan[call.from_user.id] = key

    name, price, days = PLANS[key]

    await call.message.answer(
        f"💳 <b>Оплата подписки</b>\n"
        f"Тариф: {name}\n"
        f"Сумма: {price} ₽\n\n"
        f"{PAYMENT_DETAILS}\n\n"
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
        await message.answer("Сначала выберите тариф в меню.")
        return

    name, price, days = PLANS[key]

    cur = DB.execute(
        """
        INSERT INTO payments(user_id, plan_key, photo_file_id, created_at)
        VALUES(?, ?, ?, ?)
        """,
        (uid, key, message.photo[-1].file_id, int(time.time())),
    )

    payment_id = cur.lastrowid
    DB.commit()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{payment_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{payment_id}",
                ),
            ]
        ]
    )

    await bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=(
            f"🧾 <b>Заявка #{payment_id}</b>\n"
            f"Пользователь: <code>{uid}</code>\n"
            f"Тариф: {name}\n"
            f"Сумма: {price} ₽"
        ),
        reply_markup=keyboard,
    )

    await message.answer("📨 Чек отправлен администратору!")


# ==================================
# ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ОПЛАТЫ
# ==================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_payment(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    payment_id = int(call.data.split(":")[1])

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?", (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    uid = payment["user_id"]
    key = payment["plan_key"]
    name, price, days = PLANS[key]

    ensure_user(uid)

    if key == "forever":
        DB.execute("UPDATE users SET forever=1 WHERE user_id=?", (uid,))
    else:
        row = DB.execute(
            "SELECT expires_at FROM users WHERE user_id=?", (uid,)
        ).fetchone()

        start_at = max(int(time.time()), row["expires_at"])

        DB.execute(
            "UPDATE users SET expires_at=? WHERE user_id=?",
            (start_at + days * 86400, uid),
        )

    DB.execute(
        "UPDATE payments SET status='approved' WHERE id=?", (payment_id,)
    )
    DB.commit()

    selected_plan.pop(uid, None)

    await bot.send_message(
        uid,
        f"🎉 Оплата подтверждена!\nПодписка «{name}» активирована.",
    )

    await call.message.edit_caption(
        caption=f"✅ Заявка #{payment_id} одобрена."
    )

    await call.answer("Подписка выдана.")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_payment(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    payment_id = int(call.data.split(":")[1])

    payment = DB.execute(
        "SELECT * FROM payments WHERE id=?", (payment_id,)
    ).fetchone()

    if not payment or payment["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    DB.execute(
        "UPDATE payments SET status='rejected' WHERE id=?", (payment_id,)
    )
    DB.commit()

    await bot.send_message(
        payment["user_id"],
        "❌ Заявка отклонена. Свяжитесь с администратором.",
    )

    await call.message.edit_caption(
        caption=f"❌ Заявка #{payment_id} отклонена."
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
        "SELECT trial_started, expires_at FROM users WHERE user_id=?",
        (call.from_user.id,),
    ).fetchone()

    remaining = max(
        0, row["trial_started"] + TRIAL_SECONDS - int(time.time())
    )

    if status == "🎁 Пробный период":
        status += f"\nОсталось примерно {remaining // 3600} ч."
    elif row["expires_at"] > int(time.time()):
        expiry = datetime.fromtimestamp(
            row["expires_at"], timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        status += f"\nДо: {expiry}"

    await call.message.answer(
        f"👤 <b>Профиль</b>\n"
        f"ID: <code>{call.from_user.id}</code>\n"
        f"Статус: {status}"
    )

    await call.answer()


# ==================================
# РЕЖИМЫ
# ==================================

@dp.callback_query(F.data == "chat")
async def enable_chat(call: CallbackQuery):
    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    if not allowed:
        await call.message.answer(
            "🔒 Пробный период закончился. Оформите подписку."
        )
    else:
        user_modes[call.from_user.id] = "chat"
        chat_history.pop(call.from_user.id, None)

        await call.message.answer(
            "💬 Режим общения включён!\n"
            "Просто напишите мне сообщение.\n"
            "Для очистки истории используйте /clear."
        )

    await call.answer()


@dp.callback_query(F.data == "smart")
async def smart_mode(call: CallbackQuery):
    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    if not allowed:
        await call.message.answer(
            "🔒 Оформите подписку, чтобы пользоваться ИИ."
        )
    else:
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


@dp.message(Command("clear"))
async def clear_history(message: Message):
    chat_history.pop(message.from_user.id, None)
    await message.answer("История общения очищена.")


# ==================================
# ОБРАБОТКА ТЕКСТА
# ==================================

@dp.message(F.text)
async def text_to_ai(message: Message):
    uid = message.from_user.id

    ensure_user(uid)

    allowed, status = access_status(uid)

    if not allowed:
        await message.answer(
            "🔒 Бесплатные 24 часа закончились.\n"
            "Нажмите «Купить подписку».",
            reply_markup=menu(),
        )
        return

    mode = user_modes.get(uid, "chat")

    if mode == "smart":
        system_prompt = (
            "Ты VEXORA AI. Помогаешь человеку отвечать "
            "на оскорбления без угроз и травли. "
            "Предложи три коротких варианта: "
            "спокойный, остроумный и уверенный."
        )
    else:
        system_prompt = (
            "Ты VEXORA AI — дружелюбный и полезный "
            "ИИ-помощник. Отвечай понятно на русском языке. "
            "Не выдумывай факты."
        )

    try:
        await bot.send_chat_action(message.chat.id, "typing")

        answer = await ask_ai(uid, message.text, system_prompt)

        for i in range(0, len(answer), 4000):
            await message.answer(html.escape(answer[i:i + 4000]))

    except Exception:
        logging.exception("AI request failed")

        await message.answer(
            "⚠️ Не удалось связаться с ИИ. Попробуйте позже."
        )


# ==================================
# ФУНКЦИИ В РАЗРАБОТКЕ
# ==================================

@dp.callback_query(F.data.in_({"photo", "video", "editor", "voice"}))
async def future_features(call: CallbackQuery):
    ensure_user(call.from_user.id)

    allowed, status = access_status(call.from_user.id)

    if not allowed:
        await call.message.answer(
            "🔒 Оформите подписку, чтобы пользоваться функциями."
        )
    else:
        labels = {
            "photo": "Генерация фото",
            "video": "Генерация видео",
            "editor": "Редактор фото/видео",
            "voice": "Генерация голоса",
        }

        await call.message.answer(
            f"🚧 «{labels[call.data]}» пока в разработке.\n"
            "Эта функция ещё не подключена."
        )

    await call.answer()


# ==================================
# ЗАПУСК БОТА
# ==================================

async def main():
    print("🟠 VEXORA AI запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await ai.close()


if __name__ == "__main__":
    asyncio.run(main())
