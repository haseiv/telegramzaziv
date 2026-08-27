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
from aiogram.types import ChatMemberUpdated, KeyboardButton, Message, ReplyKeyboardMarkup

from schedule_watch import (
    DEFAULT_SCHOOL_URL,
    ScheduleSnapshot,
    collect_schedule,
    describe_changes,
    format_bells,
    format_day_schedule,
    last_due_parse_slot,
    next_clock_at,
    next_parse_at,
    school_now,
    split_class_filters,
)

# ─────────────────────────── настройки ───────────────────────────

TOKEN = os.getenv("BOT_TOKEN", "")
SCHOOL_URL = (os.getenv("SCHOOL_URL", "") or DEFAULT_SCHOOL_URL).strip()
SCHOOL_CLASS = os.getenv("SCHOOL_CLASS", "").strip()
SCHOOL_INSECURE_SSL = os.getenv("SCHOOL_INSECURE_SSL", "").strip() in {"1", "true", "yes"}
SCHEDULE_PARSE_START_HOUR = int(os.getenv("SCHEDULE_PARSE_START_HOUR", "8"))
SCHEDULE_PARSE_EVERY_HOURS = max(1, int(os.getenv("SCHEDULE_PARSE_EVERY_HOURS", "2")))
SCHEDULE_PARSE_END_HOUR = int(os.getenv("SCHEDULE_PARSE_END_HOUR", "22"))
DAILY_HOUR = int(os.getenv("SCHEDULE_DAILY_HOUR", "18"))
MORNING_HOUR = int(os.getenv("SCHEDULE_MORNING_HOUR", "7"))
MORNING_MINUTE = int(os.getenv("SCHEDULE_MORNING_MINUTE", "30"))

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

BTN_TODAY = "Сегодня"
BTN_TOMORROW = "Завтра"
BTN_WEEK = "Неделя"
BTN_SUBS = "Замены"
BTN_BELLS = "Звонки"


def schedule_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_SUBS)],
            [KeyboardButton(text=BTN_BELLS), KeyboardButton(text="калл")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="класс 10А  ·  Сегодня  ·  калл",
    )

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
    data[key].setdefault("chat_type", "")
    data[key].setdefault("schedule", {})
    sch = data[key]["schedule"]
    sch.setdefault("url", SCHOOL_URL)
    if not sch.get("url"):
        sch["url"] = SCHOOL_URL
    sch.setdefault("stopped", False)
    sch.setdefault("watch", not sch.get("stopped"))
    sch.setdefault("class_filter", SCHOOL_CLASS)
    sch.setdefault("snapshot", None)
    sch.setdefault("last_check", None)
    sch.setdefault("last_change", None)
    sch.setdefault("last_diff", "")
    sch.setdefault("last_error", "")
    sch.setdefault("last_daily", "")
    return data[key]


def meta_bucket() -> dict:
    bucket = data.setdefault("_meta", {})
    bucket.setdefault("last_daily", "")
    bucket.setdefault("last_parse_slot", "")
    bucket.setdefault("last_morning", "")
    return bucket


def watching(bucket: dict) -> bool:
    if bucket.get("schedule", {}).get("stopped"):
        return False
    chat_type = bucket.get("chat_type")
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP, "group", "supergroup"):
        return True
    return bool(bucket.get("schedule", {}).get("watch"))


def display_name(user) -> str:
    return user.full_name or user.first_name or "user"


def remember_user(chat_id: int, user, chat_type: str | None = None) -> None:
    if user is None or user.is_bot:
        return
    bucket = chat_bucket(chat_id)
    if chat_type:
        bucket["chat_type"] = chat_type
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


def format_schedule_message(
    snap: ScheduleSnapshot,
    class_filter: str = "",
    *,
    days_ahead: int = 0,
    week: bool | None = None,
) -> str:
    parts = []
    for klass in split_class_filters(class_filter):
        parts.append(
            format_day_schedule(
                snap.text,
                class_filter=klass,
                days_ahead=days_ahead,
                week=week,
            )
        )
    return "\n\n".join(parts)


def evening_days_ahead(now=None) -> int:
    """После 18:00 UTC+4 шлём уже завтрашнее расписание — его выкладывают вечером."""
    return 1 if school_now(now).hour >= DAILY_HOUR else 0


async def send_plain(chat_id: int, text: str, reply_markup=None) -> None:
    parts = _chunk_text(text)
    for i, part in enumerate(parts):
        kwargs = {}
        if reply_markup is not None and i == len(parts) - 1:
            kwargs["reply_markup"] = reply_markup
        await bot.send_message(chat_id, part, **kwargs)


async def refresh_schedule(chat_id: int, *, notify: bool) -> str:
    bucket = chat_bucket(chat_id)
    sch = bucket["schedule"]
    url = (sch.get("url") or SCHOOL_URL or "").strip()
    if not url:
        return "Сайт школы ещё не задан."
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
    schedule_msg = format_schedule_message(
        snap, sch.get("class_filter") or "", days_ahead=evening_days_ahead()
    )

    if old is None:
        save_data(data)
        if notify and watching(bucket):
            klass = sch.get("class_filter") or ""
            if klass:
                await send_plain(chat_id, schedule_msg, reply_markup=schedule_keyboard())
            else:
                await send_plain(
                    chat_id,
                    "Слежу за сайтом СОШ №46. Напишите <code>класс 10А</code> и жмите кнопки внизу: Сегодня, Завтра, Неделя.",
                    reply_markup=schedule_keyboard(),
                )
            return "Запомнил расписание. Дальше напишу сам, если появятся замены."
        return "Снял первый снимок обычного расписания. Замены пришлю, когда их выложат.\n" + schedule_msg

    if not diff:
        save_data(data)
        return "Проверил сайт: с прошлого раза расписание не менялось."

    sch["last_change"] = _now_iso()
    sch["last_diff"] = diff
    save_data(data)
    report = f"{SCHEDULE_CHANGE_HEADER}\n{diff}"
    if notify and watching(bucket):
        await ping_chat(chat_id, report)
        today = format_schedule_message(
            snap, sch.get("class_filter") or "", days_ahead=0, week=False
        )
        await send_plain(chat_id, today, reply_markup=schedule_keyboard())
        return "Нашёл изменения на сайте и написал в чат."
    return report + "\n\n" + schedule_msg


async def send_daily_if_needed() -> None:
    now = school_now()
    today = now.strftime("%Y-%m-%d")
    meta = meta_bucket()
    if now.hour < DAILY_HOUR:
        return
    if meta.get("last_daily") == today:
        return
    meta["last_daily"] = today
    save_data(data)
    for key, bucket in list(data.items()):
        if key == "_meta" or not isinstance(bucket, dict):
            continue
        if not watching(bucket):
            continue
        try:
            chat_id = int(key)
        except ValueError:
            continue
        sch = bucket.get("schedule") or {}
        snap = ScheduleSnapshot.from_dict(sch.get("snapshot"))
        if snap is None:
            try:
                await refresh_schedule(chat_id, notify=True)
            except Exception:
                log.exception("Вечерняя проверка расписания %s", key)
            continue
        try:
            await send_plain(
                chat_id,
                format_schedule_message(
                    snap, sch.get("class_filter") or "", days_ahead=1, week=False
                ),
                reply_markup=schedule_keyboard(),
            )
        except Exception:
            log.exception("Не отправил вечернее расписание в %s", key)


async def send_morning_if_needed() -> None:
    now = school_now()
    today = now.strftime("%Y-%m-%d")
    meta = meta_bucket()
    morning = now.replace(hour=MORNING_HOUR, minute=MORNING_MINUTE, second=0, microsecond=0)
    if now < morning:
        return
    if meta.get("last_morning") == today:
        return
    meta["last_morning"] = today
    save_data(data)
    for key, bucket in list(data.items()):
        if key == "_meta" or not isinstance(bucket, dict):
            continue
        if not watching(bucket):
            continue
        try:
            chat_id = int(key)
        except ValueError:
            continue
        sch = bucket.get("schedule") or {}
        snap = ScheduleSnapshot.from_dict(sch.get("snapshot"))
        if snap is None:
            continue
        try:
            await send_plain(
                chat_id,
                "☀️ Доброе утро. Расписание на сегодня:\n\n"
                + format_schedule_message(
                    snap, sch.get("class_filter") or "", days_ahead=0, week=False
                ),
                reply_markup=schedule_keyboard(),
            )
        except Exception:
            log.exception("Не отправил утреннее расписание в %s", key)


async def check_all_chats() -> None:
    for key, bucket in list(data.items()):
        if key == "_meta" or not isinstance(bucket, dict):
            continue
        if not watching(bucket):
            continue
        url = (bucket.get("schedule") or {}).get("url") or SCHOOL_URL
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


def _parse_slot_kwargs() -> dict:
    return {
        "start_hour": SCHEDULE_PARSE_START_HOUR,
        "every_hours": SCHEDULE_PARSE_EVERY_HOURS,
        "end_hour": SCHEDULE_PARSE_END_HOUR,
    }


async def schedule_loop() -> None:
    while True:
        try:
            now = school_now()
            due = last_due_parse_slot(now, **_parse_slot_kwargs())
            meta = meta_bucket()
            slot_id = due.strftime("%Y-%m-%dT%H") if due else ""
            if slot_id and meta.get("last_parse_slot") != slot_id:
                log.info("Проверяю сайт школы, слот %s", slot_id)
                await check_all_chats()
                meta["last_parse_slot"] = slot_id
                save_data(data)
            await send_morning_if_needed()
            await send_daily_if_needed()
            nxt = min(
                next_parse_at(school_now(), **_parse_slot_kwargs()),
                next_clock_at(MORNING_HOUR, MORNING_MINUTE),
                next_clock_at(DAILY_HOUR, 0),
            )
            delay = max(1.0, (nxt - school_now()).total_seconds() + 1)
            log.info("Следующая задача в %s (через %.0f мин)", nxt.isoformat(), delay / 60)
            await asyncio.sleep(delay)
        except Exception:
            log.exception("Цикл проверки расписания")
            await asyncio.sleep(60)


# ── /call [текст] ──
@dp.message(Command("call"))
async def cmd_call(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await do_call(message, command.args)


# ── текстовая команда: "калл", "калл ТЕКСТ" (call/кол/зов тоже) ──
_call_re = re.compile(r"^(калл|call|кол|зов)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

@dp.message(F.text.regexp(_call_re))
async def text_call(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    m = _call_re.match(message.text.strip())
    custom_text = m.group(2) if m else None
    await do_call(message, custom_text)


HELP_TEXT = (
    "<b>Зазывалкин</b>\n\n"
    "• <code>калл</code> — позвать всех\n"
    "• кнопки внизу: Сегодня, Завтра, Неделя, Замены, Звонки\n"
    "• <code>класс 10А</code> — обычная сетка этого класса (можно <code>10А, 10Б</code>)\n"
    "• утром в 7:30 (UTC+4) — расписание на сегодня\n"
    "• в 18:00 (UTC+4) — расписание на завтра\n"
    "• каждые 2 часа с 8:00 проверяет сайт и зовёт всех, только если появились замены\n\n"
    "<b>Ещё:</b>\n"
    "• <code>/emoji 🔥</code> — свой эмодзи\n"
    "• <code>/join</code> / <code>/leave</code> / <code>/who</code>\n"
    "• <code>/schoolstop</code> — выключить автосообщения\n\n"
    "⚠️ У @BotFather отключи Privacy Mode, иначе я не вижу сообщения в группе."
)


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await message.reply(HELP_TEXT, reply_markup=schedule_keyboard())


@dp.message(Command("join"))
async def cmd_join(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
    remember_user(message.chat.id, message.from_user, message.chat.type)
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
        "Дальше сам буду присылать расписание и изменения."
    )


async def apply_class_filter(message: Message, value: str) -> None:
    value = (value or "").strip()
    sch = chat_bucket(message.chat.id)["schedule"]
    if not value:
        current = sch.get("class_filter") or "не выбран"
        await message.reply(
            "Напишите <code>класс 10А</code> или <code>/schoolclass 10А</code>.\n"
            f"Сейчас: {escape(str(current))}"
        )
        return
    if value.lower() in {"все", "всё", "-", "нет"}:
        sch["class_filter"] = ""
        save_data(data)
        await message.reply("Фильтр класса снят.")
        return
    sch["class_filter"] = value
    save_data(data)
    await refresh_schedule(message.chat.id, notify=False)
    snap = ScheduleSnapshot.from_dict(
        chat_bucket(message.chat.id)["schedule"].get("snapshot")
    )
    if not snap:
        await message.reply("Не смог скачать расписание с сайта школы.")
        return
    await message.reply(
        f"Класс <b>{escape(value)}</b>. Это обычное расписание на неделю. "
        "Замены пришлю отдельно, когда их выложат."
    )
    for part in _chunk_text(format_schedule_message(snap, value, days_ahead=0)):
        await message.reply(part, reply_markup=schedule_keyboard())


@dp.message(Command("schoolclass", "class", "класс"))
async def cmd_schoolclass(message: Message, command: CommandObject):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await apply_class_filter(message, command.args or "")


@dp.message(Command("schoolwatch"))
async def cmd_schoolwatch(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    sch = chat_bucket(message.chat.id)["schedule"]
    url = (sch.get("url") or SCHOOL_URL or "").strip()
    if not url:
        await message.reply("Сначала задайте сайт: <code>/schoolurl https://…</code>")
        return
    sch["watch"] = True
    sch["stopped"] = False
    save_data(data)
    status = await refresh_schedule(message.chat.id, notify=True)
    await message.reply(
        f"Снова сам присылаю расписание СОШ №46 в этот чат.\n{status}"
    )


@dp.message(Command("schoolstop"))
async def cmd_schoolstop(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    chat_bucket(message.chat.id)["schedule"]["watch"] = False
    chat_bucket(message.chat.id)["schedule"]["stopped"] = True
    save_data(data)
    await message.reply("Больше сам не присылаю расписание в этот чат.")


@dp.message(Command("schoolcheck", "school"))
async def cmd_schoolcheck(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    if not await is_admin(message, message.from_user.id):
        await message.reply("Только для админов.")
        return
    status = await refresh_schedule(message.chat.id, notify=True)
    for part in _chunk_text(status):
        await message.reply(part)


async def ensure_snapshot(chat_id: int):
    sch = chat_bucket(chat_id)["schedule"]
    snap = ScheduleSnapshot.from_dict(sch.get("snapshot"))
    if snap is None:
        await refresh_schedule(chat_id, notify=False)
        sch = chat_bucket(chat_id)["schedule"]
        snap = ScheduleSnapshot.from_dict(sch.get("snapshot"))
    return snap, sch.get("class_filter") or ""


async def reply_schedule_view(message: Message, *, days_ahead: int = 0, week: bool = False) -> None:
    snap, klass = await ensure_snapshot(message.chat.id)
    if not snap:
        await message.reply("Пока нет снимка с сайта. Напишите <code>класс 10А</code>.")
        return
    text = format_schedule_message(snap, klass, days_ahead=days_ahead, week=week)
    for part in _chunk_text(text):
        await message.reply(part, reply_markup=schedule_keyboard())


@dp.message(Command("расписание", "raspisanie", "schedule", "today", "сегодня"))
async def cmd_raspisanie(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await reply_schedule_view(message, days_ahead=0, week=False)


@dp.message(Command("tomorrow", "завтра"))
async def cmd_tomorrow(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await reply_schedule_view(message, days_ahead=1, week=False)


@dp.message(Command("week", "неделя"))
async def cmd_week(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    await reply_schedule_view(message, days_ahead=0, week=True)


@dp.message(Command("bells", "звонки", "zvonki"))
async def cmd_bells(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    snap, _klass = await ensure_snapshot(message.chat.id)
    bells = snap.bells if snap else ""
    await message.reply(format_bells(bells), reply_markup=schedule_keyboard())


@dp.message(Command("изменения", "izmeneniya", "changes", "замены"))
async def cmd_izmeneniya(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    sch = chat_bucket(message.chat.id)["schedule"]
    diff = sch.get("last_diff") or ""
    if not diff:
        await message.reply(
            "Замен на сайте сейчас нет — действует обычное расписание.",
            reply_markup=schedule_keyboard(),
        )
        return
    when = sch.get("last_change") or ""
    text = f"{SCHEDULE_CHANGE_HEADER}\n{diff}"
    if when:
        text += f"\n\nЗафиксировано: {when}"
    for part in _chunk_text(text):
        await message.reply(part, reply_markup=schedule_keyboard())


_btn_re = re.compile(r"^(сегодня|завтра|неделя|замены|звонки)$", re.IGNORECASE)
_rasp_re = re.compile(r"^/?(расписание|изменения)(?:@\w+)?(?:\s+(\S.*))?$", re.IGNORECASE)
_class_re = re.compile(r"^/?(класс|class)(?:@\w+)?\s+(\S.*)$", re.IGNORECASE)

@dp.message(F.text.regexp(_btn_re))
async def text_buttons(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    key = message.text.strip().lower()
    if key == "сегодня":
        await reply_schedule_view(message, days_ahead=0, week=False)
    elif key == "завтра":
        await reply_schedule_view(message, days_ahead=1, week=False)
    elif key == "неделя":
        await reply_schedule_view(message, days_ahead=0, week=True)
    elif key == "замены":
        await cmd_izmeneniya(message)
    else:
        await cmd_bells(message)

@dp.message(F.text.regexp(_class_re))
async def text_class(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    m = _class_re.match(message.text.strip())
    args = m.group(2).strip() if m else ""
    await apply_class_filter(message, args)


@dp.message(F.text.regexp(_rasp_re))
async def text_schedule(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)
    m = _rasp_re.match(message.text.strip())
    word = (m.group(1) if m else "").lower()
    extra = (m.group(2) or "").strip() if m else ""
    if word == "изменения":
        await cmd_izmeneniya(message)
        return
    if extra:
        await apply_class_filter(message, extra)
        return
    await cmd_raspisanie(message)


# ловим любой текст, чтобы запоминать людей (идёт последним)
@dp.message(F.text)
async def remember_on_any_text(message: Message):
    remember_user(message.chat.id, message.from_user, message.chat.type)


@dp.my_chat_member()
async def bot_membership(event: ChatMemberUpdated):
    new_status = event.new_chat_member.status
    if new_status not in {"member", "administrator"}:
        return
    bucket = chat_bucket(event.chat.id)
    bucket["chat_type"] = event.chat.type
    sch = bucket["schedule"]
    sch["url"] = sch.get("url") or SCHOOL_URL
    sch["stopped"] = False
    sch["watch"] = True
    save_data(data)
    try:
        await bot.send_message(
            event.chat.id,
            "Я Зазывалкин. Напишите <code>класс 10А</code> и жмите кнопки внизу: Сегодня, Завтра, Неделя, Замены, Звонки.",
            reply_markup=schedule_keyboard(),
        )
        await refresh_schedule(event.chat.id, notify=False)
    except Exception:
        log.exception("Не смог отправить расписание после добавления в чат")


async def main():
    if not TOKEN:
        raise SystemExit("Укажи токен бота в переменной окружения BOT_TOKEN.")
    print("Бот запущен. Ctrl+C для остановки.")
    asyncio.create_task(schedule_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
