"""
Telegram-бот «Зазывалкин» — зовёт участников чата и следит за школьным расписанием.

Главное:
    калл                 — тегнуть всех с заголовком по умолчанию
    калл ТЕКСТ           — тегнуть всех и показать этот ТЕКСТ
    (то же для /call и слова "call")

Расписание школы:
    /расписание          — что сейчас на сайте
    /изменения           — последние найденные изменения
    /schoolurl URL       — (админ) адрес школьного сайта
    /schoolwatch         — (админ) слать в этот чат, когда расписание меняется
    /schoolcheck         — (админ) проверить сайт прямо сейчас

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
import logging
import os
import random
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiogram.types import Message

from schedule_watch import (
    ScheduleSnapshot,
    collect_schedule,
    describe_changes,
)

# ─────────────────────────── настройки ───────────────────────────

TOKEN = os.getenv("BOT_TOKEN", "")
SCHOOL_URL = os.getenv("SCHOOL_URL", "").strip()
SCHOOL_CLASS = os.getenv("SCHOOL_CLASS", "").strip()
SCHOOL_INSECURE_SSL = os.getenv("SCHOOL_INSECURE_SSL", "").strip() in {"1", "true", "yes"}
SCHEDULE_POLL_SECONDS = max(60, int(os.getenv("SCHEDULE_POLL_SECONDS", "900")))

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
SCHEDULE_CHANGE_HEADER = "📢 Изменения в расписании школы:"

# сколько упоминаний в одном сообщении (Telegram не любит очень длинные)
MENTIONS_PER_MESSAGE = 30
TELEGRAM_TEXT_LIMIT = 3500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zazyvalkin")

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
    data[key].setdefault("schedule", {})
    sch = data[key]["schedule"]
    sch.setdefault("url", SCHOOL_URL)
    sch.setdefault("watch", False)
    sch.setdefault("class_filter", SCHOOL_CLASS)
    sch.setdefault("snapshot", None)
    sch.setdefault("last_check", None)
    sch.setdefault("last_change", None)
    sch.setdefault("last_diff", "")
    sch.setdefault("last_error", "")
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        add = len(line) + 1
        if buf and size + add > limit:
            parts.append("\n".join(buf))
            buf = [line]
            size = add
        else:
            buf.append(line)
            size += add
    if buf:
        parts.append("\n".join(buf))
    return parts or [text[:limit]]


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


async def ping_chat(chat_id: int, header: str) -> None:
    bucket = chat_bucket(chat_id)
    users = bucket["users"]
    mentions = [
        mention_html(uid, u["name"], u.get("emoji", "•"))
        for uid, u in users.items()
    ]
    safe_header = escape(header)
    if not mentions:
        for part in _chunk_text(safe_header):
            await bot.send_message(chat_id, part)
        return
    first = True
    for i in range(0, len(mentions), MENTIONS_PER_MESSAGE):
        chunk = mentions[i : i + MENTIONS_PER_MESSAGE]
        if first:
            parts = _chunk_text(safe_header)
            for j, part in enumerate(parts):
                suffix = "\n" + " ".join(chunk) if j == len(parts) - 1 else ""
                await bot.send_message(chat_id, part + suffix)
            first = False
        else:
            await bot.send_message(chat_id, " ".join(chunk))


def format_snapshot_preview(snap: ScheduleSnapshot, limit: int = 2500) -> str:
    files = ""
    if snap.files:
        files = "\n\nФайлы:\n" + "\n".join(f"• {escape(f.title)}\n  {escape(f.url)}" for f in snap.files[:8])
    pages = ""
    if snap.pages:
        pages = "\nСтраницы: " + ", ".join(escape(p) for p in snap.pages[:6])
    body = escape(snap.text)
    if len(body) > limit:
        body = body[:limit] + "\n…"
    return f"{pages}{files}\n\n<pre>{body}</pre>"


async def refresh_schedule(chat_id: int, *, notify: bool) -> str:
    bucket = chat_bucket(chat_id)
    sch = bucket["schedule"]
    url = (sch.get("url") or SCHOOL_URL or "").strip()
    if not url:
        return (
            "Сайт школы ещё не задан. Админ: пришлите "
            "<code>/schoolurl https://сайт-школы.ru</code> "
            "(лучше сразу страницу «Расписание»)."
        )
    try:
        snap = await collect_schedule(
            url,
            class_filter=sch.get("class_filter") or "",
            insecure_ssl=SCHOOL_INSECURE_SSL,
        )
    except Exception as exc:
        sch["last_check"] = _now_iso()
        sch["last_error"] = str(exc)
        save_data(data)
        log.exception("Не удалось скачать расписание %s", url)
        return f"Не смог открыть сайт школы: {escape(str(exc))}"

    old = ScheduleSnapshot.from_dict(sch.get("snapshot"))
    diff = describe_changes(old, snap)
    sch["last_check"] = _now_iso()
    sch["last_error"] = ""
    sch["snapshot"] = snap.to_dict()

    if old is None:
        save_data(data)
        return (
            "Снял первый снимок расписания. Дальше буду сравнивать с ним и "
            "писать, что изменилось."
            + format_snapshot_preview(snap)
        )

    if not diff:
        save_data(data)
        return "Проверил сайт: с прошлого раза расписание не менялось."

    sch["last_change"] = _now_iso()
    sch["last_diff"] = diff
    save_data(data)
    report = f"{SCHEDULE_CHANGE_HEADER}\n{diff}\n\nИсточник: {url}"
    if notify and sch.get("watch"):
        await ping_chat(chat_id, report)
        return "Нашёл изменения и написал в чат."
    return report


async def schedule_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            for key, bucket in list(data.items()):
                if not isinstance(bucket, dict):
                    continue
                sch = bucket.get("schedule") or {}
                if not sch.get("watch"):
                    continue
                url = (sch.get("url") or SCHOOL_URL or "").strip()
                if not url:
                    continue
                try:
                    chat_id = int(key)
                except ValueError:
                    continue
                try:
                    await refresh_schedule(chat_id, notify=True)
                except Exception:
                    log.exception("Проверка расписания для чата %s упала", key)
        except Exception:
            log.exception("Цикл проверки расписания")
        await asyncio.sleep(SCHEDULE_POLL_SECONDS)


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


HELP_TEXT = (
    "<b>Зазывалкин</b>\n\n"
    "• <code>калл</code> — позвать всех\n"
    "• <code>калл ТЕКСТ</code> — позвать всех и показать этот текст\n"
    "  (например: <code>калл собираемся на созвон через 5 минут</code>)\n\n"
    "<b>Расписание школы:</b>\n"
    "• <code>/расписание</code> — что сейчас на сайте\n"
    "• <code>/изменения</code> — последние найденные изменения\n"
    "• <code>/schoolurl https://…</code> — (админ) адрес сайта или страницы «Расписание»\n"
    "• <code>/schoolclass 8А</code> — (админ) следить только за этим классом, если он есть на странице\n"
    "• <code>/schoolwatch</code> — (админ) писать в чат, когда расписание меняется, и звать всех\n"
    "• <code>/schoolstop</code> — (админ) перестать следить\n"
    "• <code>/schoolcheck</code> — (админ) проверить сайт прямо сейчас\n\n"
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


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    remember_user(message.chat.id, message.from_user)
    await message.reply(HELP_TEXT)


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


def _http_url(raw: str) -> str | None:
    url = raw.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not re.match(r"^https?://[^.\s]+\.[^\s]+", url, re.I):
        return None
    return url


@dp.message(Command("schoolurl"))
async def cmd_schoolurl(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    url = _http_url(command.args or "")
    if not url:
        current = chat_bucket(message.chat.id)["schedule"].get("url") or SCHOOL_URL or "не задан"
        await message.reply(
            "Использование: <code>/schoolurl https://ваш-сайт-школы.ru/raspisanie</code>\n"
            f"Сейчас: {escape(str(current))}"
        )
        return
    sch = chat_bucket(message.chat.id)["schedule"]
    sch["url"] = url
    sch["snapshot"] = None
    sch["last_diff"] = ""
    save_data(data)
    await message.reply(
        f"Сайт школы: {escape(url)}\n"
        "Сниму первый снимок при следующей проверке. "
        "Включите слежение: <code>/schoolwatch</code>."
    )


@dp.message(Command("schoolclass"))
async def cmd_schoolclass(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    value = (command.args or "").strip()
    sch = chat_bucket(message.chat.id)["schedule"]
    sch["class_filter"] = value
    sch["snapshot"] = None
    save_data(data)
    if value:
        await message.reply(f"Буду выделять строки про класс <b>{escape(value)}</b>.")
    else:
        await message.reply("Фильтр класса снят — смотрю всё расписание на странице.")


@dp.message(Command("schoolwatch"))
async def cmd_schoolwatch(message: Message):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    sch = chat_bucket(message.chat.id)["schedule"]
    url = (sch.get("url") or SCHOOL_URL or "").strip()
    if not url:
        await message.reply("Сначала задайте сайт: <code>/schoolurl https://…</code>")
        return
    sch["watch"] = True
    save_data(data)
    status = await refresh_schedule(message.chat.id, notify=False)
    await message.reply(
        f"Слежу за {escape(url)} и буду звать всех, когда расписание изменится.\n{status}"
    )


@dp.message(Command("schoolstop"))
async def cmd_schoolstop(message: Message):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    chat_bucket(message.chat.id)["schedule"]["watch"] = False
    save_data(data)
    await message.reply("Больше не слежу за сайтом в этом чате.")


@dp.message(Command("schoolcheck", "school"))
async def cmd_schoolcheck(message: Message):
    remember_user(message.chat.id, message.from_user)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    status = await refresh_schedule(message.chat.id, notify=True)
    for part in _chunk_text(status):
        await message.reply(part)


@dp.message(Command("расписание", "raspisanie", "schedule"))
async def cmd_raspisanie(message: Message):
    remember_user(message.chat.id, message.from_user)
    bucket = chat_bucket(message.chat.id)
    sch = bucket["schedule"]
    snap = ScheduleSnapshot.from_dict(sch.get("snapshot"))
    url = (sch.get("url") or SCHOOL_URL or "").strip()
    if snap is None:
        if not url:
            await message.reply(
                "Сайт школы не задан. Админ: <code>/schoolurl https://сайт-школы.ru</code>"
            )
            return
        status = await refresh_schedule(message.chat.id, notify=False)
        for part in _chunk_text(status):
            await message.reply(part)
        return
    when = sch.get("last_check") or "неизвестно"
    header = (
        f"📅 Расписание с сайта школы\n"
        f"Проверено: {escape(str(when))}\n"
        f"Источник: {escape(url)}"
    )
    await message.reply(header + format_snapshot_preview(snap))


@dp.message(Command("изменения", "izmeneniya", "changes"))
async def cmd_izmeneniya(message: Message):
    remember_user(message.chat.id, message.from_user)
    sch = chat_bucket(message.chat.id)["schedule"]
    diff = sch.get("last_diff") or ""
    if not diff:
        await message.reply("Пока не видел изменений. Напишите /расписание или /schoolcheck.")
        return
    when = sch.get("last_change") or ""
    text = f"{SCHEDULE_CHANGE_HEADER}\n{diff}"
    if when:
        text += f"\n\nЗафиксировано: {when}"
    for part in _chunk_text(text):
        await message.reply(part)


_rasp_re = re.compile(r"^/?(расписание|изменения)(?:@\w+)?\s*$", re.IGNORECASE)

@dp.message(F.text.regexp(_rasp_re))
async def text_schedule(message: Message):
    remember_user(message.chat.id, message.from_user)
    word = message.text.strip().lower()
    if word == "изменения":
        await cmd_izmeneniya(message)
    else:
        await cmd_raspisanie(message)


# ловим любой текст, чтобы запоминать людей (идёт последним)
@dp.message(F.text)
async def remember_on_any_text(message: Message):
    remember_user(message.chat.id, message.from_user)


async def main():
    if not TOKEN:
        raise SystemExit("Укажи токен бота в переменной окружения BOT_TOKEN.")
    print("Бот запущен. Ctrl+C для остановки.")
    asyncio.create_task(schedule_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
