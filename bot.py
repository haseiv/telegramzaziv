"""
Telegram-бот «Калл» — зовёт всех участников чата по текстовой команде.

Главное:
    калл                 — тегнуть всех с заголовком по умолчанию
    калл ТЕКСТ           — тегнуть всех и показать этот ТЕКСТ
    (то же для /call и слова "call")

Возможности:
    • у каждого свой эмодзи-идентификатор рядом с упоминанием
    • /emoji 🔥                — задать себе эмодзи
    • /setemoji @user 🔥       — админ задаёт эмодзи другому (или ответом на сообщение)
    • /join / /leave           — добавить/убрать себя из списка
    • /who                     — кто в списке
    • /calltext ТЕКСТ          — админ меняет заголовок по умолчанию
    • /help                    — помощь

ВАЖНО про Telegram:
    Bot API не отдаёт полный список участников группы — это ограничение Telegram.
    Поэтому бот запоминает людей, когда они пишут в чат либо делают /join,
    и пингует их персональными упоминаниями tg://user?id=... (реальное уведомление).
    У @BotFather нужно отключить Privacy Mode (/setprivacy -> Disable).
"""

import asyncio
import json
import os
import random
import re
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiogram.types import Message

# ─────────────────────────── настройки ───────────────────────────

TOKEN = os.getenv("BOT_TOKEN", "")

# Куда писать данные. Многие хостинги дают персистентную папку через DATA_DIR
# (переживает передеплой). Если её нет — пишем рядом со скриптом.
_data_dir = os.getenv("DATA_DIR")
if _data_dir:
    Path(_data_dir).mkdir(parents=True, exist_ok=True)
    DATA_FILE = Path(_data_dir) / os.getenv("DATA_FILE", "call_bot_data.json")
else:
    DATA_FILE = Path(os.getenv("DATA_FILE", "call_bot_data.json"))

# слова-триггеры для текстовой команды (без слэша), в любом регистре
CALL_TRIGGERS = {"калл", "call", "кол", "зов"}

# эмодзи для новичков, если не выбрали свой
DEFAULT_EMOJI_POOL = [
    "🔥", "⚡", "🌟", "🎯", "🚀", "🐉", "🦊", "🐺", "🦈", "🐙",
    "🍀", "💎", "🌸", "🎸", "👾", "🥷", "🧊", "🌊", "🍕", "🦁",
]

DEFAULT_CALL_TEXT = "📣 Общий сбор! Все сюда:"

# сколько упоминаний в одном сообщении (Telegram не любит очень длинные)
MENTIONS_PER_MESSAGE = 30

# ─────────────────────────── хранилище ───────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


data = load_data()


def chat_bucket(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in data:
        data[key] = {"call_text": DEFAULT_CALL_TEXT, "users": {}}
    data[key].setdefault("call_text", DEFAULT_CALL_TEXT)
    data[key].setdefault("users", {})
    return data[key]


def display_name(user) -> str:
    return user.full_name or user.first_name or "user"


def remember_user(chat_id: int, user) -> None:
    if user is None or user.is_bot:
        return
    bucket = chat_bucket(chat_id)
    uid = str(user.id)
    if uid not in bucket["users"]:
        bucket["users"][uid] = {
            "name": display_name(user),
            "emoji": random.choice(DEFAULT_EMOJI_POOL),
        }
    else:
        bucket["users"][uid]["name"] = display_name(user)
    save_data(data)


def mention_html(uid: str, name: str, emoji: str) -> str:
    # кликабельной ссылкой-упоминанием выступает сам эмодзи, ник не показываем
    return f'<a href="tg://user?id={uid}">{emoji}</a>'


# ─────────────────────────── бот ───────────────────────────

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


async def is_admin(message: Message, user_id: int) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True
    member = await message.chat.get_member(user_id)
    return member.status in ("administrator", "creator")


async def do_call(message: Message, custom_text: str | None = None):
    """Тегает всех. Если передан custom_text — используем его как заголовок."""
    bucket = chat_bucket(message.chat.id)
    users = bucket["users"]

    if not users:
        await message.reply(
            "Мне некого звать 🤷 Пусть участники напишут что-нибудь в чат "
            "или сделают /join, чтобы я их запомнил."
        )
        return

    mentions = [
        mention_html(uid, u["name"], u.get("emoji", "•"))
        for uid, u in users.items()
    ]

    header = custom_text.strip() if custom_text and custom_text.strip() else bucket["call_text"]
    header = escape(header)  # текст от пользователя экранируем, чтобы не сломать HTML

    for i in range(0, len(mentions), MENTIONS_PER_MESSAGE):
        chunk = mentions[i : i + MENTIONS_PER_MESSAGE]
        prefix = header + "\n" if i == 0 else ""
        await message.answer(prefix + " ".join(chunk))


# ── /call [текст] ──
@dp.message(Command("call"))
async def cmd_call(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    await do_call(message, command.args)


# ── текстовая команда: "калл", "калл ТЕКСТ" (call/кол/зов тоже) ──
_call_re = re.compile(r"^(калл|call|кол|зов)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

@dp.message(F.text.regexp(_call_re))
async def text_call(message: Message):
    remember_user(message.chat.id, message.from_user)
    m = _call_re.match(message.text.strip())
    custom_text = m.group(2) if m else None
    await do_call(message, custom_text)


# ── помощь ──
@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    remember_user(message.chat.id, message.from_user)
    await message.reply(
        "<b>Бот-звалка «Калл»</b>\n\n"
        "• <code>калл</code> — позвать всех\n"
        "• <code>калл ТЕКСТ</code> — позвать всех и показать этот текст\n"
        "  (например: <code>калл собираемся на созвон через 5 минут</code>)\n\n"
        "<b>Эмодзи и настройки:</b>\n"
        "• <code>/emoji 🔥</code> — задать себе эмодзи\n"
        "• <code>/setemoji @user 🔥</code> — (админ) эмодзи другому\n"
        "• <code>/join</code> / <code>/leave</code> — вход/выход из списка\n"
        "• <code>/who</code> — кто в списке\n"
        "• <code>/calltext ТЕКСТ</code> — (админ) заголовок по умолчанию\n\n"
        "⚠️ У @BotFather отключи Privacy Mode, иначе я не вижу сообщения в группе.\n"
        "ℹ️ Я запоминаю людей, когда они пишут в чат — весь список участников "
        "Telegram боту не выдаёт."
    )


@dp.message(Command("join"))
async def cmd_join(message: Message):
    remember_user(message.chat.id, message.from_user)
    emoji = chat_bucket(message.chat.id)["users"][str(message.from_user.id)]["emoji"]
    await message.reply(f"Готово, добавил тебя в список {emoji}")


@dp.message(Command("leave"))
async def cmd_leave(message: Message):
    bucket = chat_bucket(message.chat.id)
    uid = str(message.from_user.id)
    if uid in bucket["users"]:
        del bucket["users"][uid]
        save_data(data)
        await message.reply("Убрал тебя из списка 👋")
    else:
        await message.reply("Тебя и так нет в списке.")


@dp.message(Command("emoji"))
async def cmd_emoji(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("Использование: <code>/emoji 🔥</code>")
        return
    emoji = arg.split()[0]
    chat_bucket(message.chat.id)["users"][str(message.from_user.id)]["emoji"] = emoji
    save_data(data)
    await message.reply(f"Твой эмодзи теперь {emoji}")


@dp.message(Command("setemoji"))
async def cmd_setemoji(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return

    bucket = chat_bucket(message.chat.id)
    target_id = None
    emoji = None

    if message.reply_to_message and command.args:
        target_id = str(message.reply_to_message.from_user.id)
        remember_user(message.chat.id, message.reply_to_message.from_user)
        emoji = command.args.strip().split()[0]
    elif message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                target_id = str(ent.user.id)
                remember_user(message.chat.id, ent.user)
        parts = (command.args or "").split()
        if parts:
            emoji = parts[-1]

    if not target_id or not emoji:
        await message.reply(
            "Ответь на сообщение человека и напиши <code>/setemoji 🔥</code>, "
            "либо <code>/setemoji @user 🔥</code> (упоминание должно быть кликабельным)."
        )
        return

    bucket["users"].setdefault(target_id, {"name": "user", "emoji": emoji})
    bucket["users"][target_id]["emoji"] = emoji
    save_data(data)
    await message.reply(f"Поставил {emoji} этому человеку.")


@dp.message(Command("calltext"))
async def cmd_calltext(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    text = (command.args or "").strip()
    if not text:
        await message.reply("Использование: <code>/calltext 📣 Все на созвон!</code>")
        return
    chat_bucket(message.chat.id)["call_text"] = text
    save_data(data)
    await message.reply("Заголовок по умолчанию обновлён ✅")


@dp.message(Command("who"))
async def cmd_who(message: Message):
    remember_user(message.chat.id, message.from_user)
    users = chat_bucket(message.chat.id)["users"]
    if not users:
        await message.reply("Пока никого не знаю. Пусть люди напишут /join.")
        return
    lines = [f"{u['emoji']} {escape(u['name'])}" for u in users.values()]
    await message.reply("Знаю этих людей:\n" + "\n".join(lines))


# ловим любой текст, чтобы запоминать людей (идёт последним)
@dp.message(F.text)
async def remember_on_any_text(message: Message):
    remember_user(message.chat.id, message.from_user)


async def main():
    if not TOKEN:
        raise SystemExit("Укажи токен бота в переменной окружения BOT_TOKEN.")
    print("Бот запущен. Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
