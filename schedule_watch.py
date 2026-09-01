"""Парсинг сайта СОШ №46 и сравнение снимков расписания."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

DEFAULT_SCHOOL_URL = "https://sosh46.ru/schedule"
SOSH46_API_URL = "https://sosh46.ru/api/schedule"
# Таблица, куда на сайте пишут сетку (тот же ID, что в админке sosh46.ru).
SOSH46_SHEET_ID = os.getenv(
    "SOSH46_SHEET_ID",
    "1uEvu2RU9JT67n4erJu6U0F2sS2MxZGeh1QmTd8qfZ-Q",
)
SOSH46_SHEET_TABS = (
    ("2114544105", "1-4 классы"),
    ("0", "5-11 классы"),
    ("656166446", "Изменения"),
)
SCHOOL_TZ = timezone(timedelta(hours=4))  # UTC+4

SCHEDULE_KEYWORDS = (
    "расписан",
    "замен",
    "звонк",
    "урок",
    "каникул",
    "изменен",
    "timetable",
    "schedule",
    "заняти",
    "klass",
)

FILE_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf", ".odt", ".ppt", ".pptx")
SKIP_TAGS = {"script", "style", "noscript", "svg", "form"}
MAX_PAGES = 8
MAX_FILES = 12
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 80_000

USER_AGENT = (
    "Mozilla/5.0 (compatible; ZazyvalkinBot/1.0; +https://t.me/) "
    "AppleWebKit/537.36 Chrome/120.0.0.0"
)

log = logging.getLogger("zazyvalkin.schedule")

DAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
DAY_INDEX = {name: i for i, name in enumerate(DAY_NAMES)}


@dataclass
class ScheduleFile:
    url: str
    title: str
    sha256: str = ""

    def key(self) -> str:
        return self.url.rstrip("/")


@dataclass
class Lesson:
    sheet: str
    day: str
    class_name: str
    num: str
    time: str
    subject: str

    def line(self) -> str:
        return f"{self.sheet} | {self.day} | {self.class_name} | {self.num}. {self.time} {self.subject}".strip()


@dataclass
class ScheduleSnapshot:
    source_url: str
    pages: list[str] = field(default_factory=list)
    text: str = ""
    files: list[ScheduleFile] = field(default_factory=list)
    fingerprint: str = ""
    bells: str = ""

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "pages": self.pages,
            "text": self.text,
            "files": [
                {"url": f.url, "title": f.title, "sha256": f.sha256} for f in self.files
            ],
            "fingerprint": self.fingerprint,
            "bells": self.bells,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ScheduleSnapshot | None":
        if not raw:
            return None
        files = [
            ScheduleFile(
                url=item.get("url", ""),
                title=item.get("title", ""),
                sha256=item.get("sha256", ""),
            )
            for item in raw.get("files") or []
            if item.get("url")
        ]
        snap = cls(
            source_url=raw.get("source_url", ""),
            pages=list(raw.get("pages") or []),
            text=raw.get("text") or "",
            files=files,
            fingerprint=raw.get("fingerprint") or "",
            bells=raw.get("bells") or "",
        )
        if not snap.fingerprint:
            snap.fingerprint = fingerprint_of(snap)
        return snap


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_schedule(text: str) -> bool:
    low = text.lower()
    return any(word in low for word in SCHEDULE_KEYWORDS)


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251", "koi8-r"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(list(SKIP_TAGS)):
        tag.decompose()
    chunks: list[str] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "div", "span"]):
        if not isinstance(node, Tag):
            continue
        if node.find(["p", "li", "td", "th", "h1", "h2", "h3", "h4", "div"]):
            continue
        text = _norm_space(node.get_text(" ", strip=True))
        if text:
            chunks.append(text)
    if len("\n".join(chunks)) < 40:
        chunks = [_norm_space(line) for line in soup.get_text("\n").splitlines()]
        chunks = [c for c in chunks if c]
    return "\n".join(chunks)


def extract_tables(soup: BeautifulSoup) -> str:
    rows: list[str] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [_norm_space(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
        rows.append("")
    return "\n".join(rows).strip()


def collect_links(
    soup: BeautifulSoup,
    base_url: str,
    *,
    keep_all_files: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    pages: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    seen: set[str] = set()
    origin = urlparse(base_url)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        title = _norm_space(a.get_text(" ", strip=True)) or key
        path_low = parsed.path.lower()
        hay = f"{title} {path_low} {parsed.query.lower()}"
        if any(path_low.endswith(ext) for ext in FILE_EXT):
            if keep_all_files or _looks_like_schedule(hay):
                files.append((key, title))
            continue
        if parsed.netloc and origin.netloc and parsed.netloc != origin.netloc:
            continue
        if _looks_like_schedule(hay):
            pages.append((key, title))
    return pages, files


def extract_iframes(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for iframe in soup.find_all("iframe", src=True):
        src = unescape(iframe["src"].strip())
        url = urljoin(base_url, src)
        if url in seen:
            continue
        seen.add(url)
        title = _norm_space(iframe.get("title") or "")
        if title.lower() in {"", "таблица", "spreadsheet", "google sheets"}:
            title = ""
        found.append((url, title))
    return found


def canonicalize_school_url(url: str) -> str:
    """Старые wordpress-страницы /raspisanie/ больше не существуют."""
    url = (url or "").strip()
    if not url:
        return DEFAULT_SCHOOL_URL
    host = urlparse(url).netloc.lower()
    if host.endswith("sosh46.ru"):
        return DEFAULT_SCHOOL_URL
    return url


def sosh46_schedule_api_url(source_url: str) -> str | None:
    host = urlparse(source_url).netloc.lower()
    path = urlparse(source_url).path.rstrip("/")
    if host.endswith("sosh46.ru"):
        return SOSH46_API_URL
    if path.endswith("/api/schedule"):
        return source_url
    return None


def _clean_api_text(value) -> str:
    return _norm_space(str(value or "").replace("\n—", "/").replace("\n", " "))


def lesson_from_api(item: dict, sheet: str) -> Lesson | None:
    class_name = _clean_api_text(item.get("className"))
    day = _clean_api_text(item.get("day")).upper().replace("Ё", "Е")
    subject = _clean_api_text(item.get("subject"))
    if not class_name or not day or not subject:
        return None
    teacher = _clean_api_text(item.get("teacher"))
    room = _clean_api_text(item.get("room"))
    note = _clean_api_text(item.get("note"))
    bits = [subject]
    extra = [part for part in (teacher, room) if part]
    if extra:
        bits.append("— " + ", ".join(extra))
    if note and "изменен" not in note.lower():
        bits.append(f"({note})")
    num = str(item.get("number") if item.get("number") not in (None, "") else "?").strip()
    return Lesson(
        sheet=sheet,
        day=day,
        class_name=class_name,
        num=num or "?",
        time=_clean_api_text(item.get("time")),
        subject=" ".join(bits),
    )


def bells_to_text(rows: Iterable[dict]) -> str:
    labels = {
        ("monday", 1): "Понедельник, 1 смена",
        ("monday", 2): "Понедельник, 2 смена",
        ("regular", 1): "Вторник–суббота, 1 смена",
        ("regular", 2): "Вторник–суббота, 2 смена",
    }
    groups: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    for row in rows:
        key = (row.get("dayGroup"), row.get("shift"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        n = row.get("lesson")
        start = row.get("start") or ""
        end = row.get("end") or ""
        brk = _clean_api_text(row.get("break"))
        line = f"{n} урок — {start} – {end}"
        if brk:
            line += f" (перемена {brk})"
        groups[key].append(line)
    chunks: list[str] = []
    for key in order:
        chunks.append(labels.get(key, f"{key[0]} · смена {key[1]}"))
        chunks.extend(groups[key])
        chunks.append("")
    return "\n".join(chunks).strip()


def parse_schedule_api(payload: dict) -> tuple[list[Lesson], str]:
    lessons: list[Lesson] = []
    for item in payload.get("lessons") or []:
        lesson = lesson_from_api(item, "Расписание")
        if lesson:
            lessons.append(lesson)
    for item in payload.get("changes") or []:
        lesson = lesson_from_api(item, "Изменения")
        if lesson:
            lessons.append(lesson)
    return lessons, bells_to_text(payload.get("bells") or [])


def parse_next_flight_payload(html: str) -> dict:
    """Достаёт уроки/замены/звонки из RSC-страницы, если /api/schedule недоступен."""
    blob_parts: list[str] = []
    for match in re.finditer(r"self\.__next_f\.push\(\[1,(\"(?:\\.|[^\"\\])*\")\]\)", html):
        try:
            blob_parts.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    blob = "".join(blob_parts) if blob_parts else html
    lessons: list[dict] = []
    changes: list[dict] = []
    bells: list[dict] = []
    seen: set[str] = set()
    decoder = json.JSONDecoder()
    for prefix in ('{"className":', '{"dayGroup":', '{"id":"change'):
        idx = 0
        while True:
            found = blob.find(prefix, idx)
            if found < 0:
                break
            brace = blob.rfind("{", 0, found + 1)
            try:
                obj, end = decoder.raw_decode(blob, brace)
            except json.JSONDecodeError:
                idx = found + 1
                continue
            idx = end
            if not isinstance(obj, dict):
                continue
            key = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if obj.get("dayGroup") is not None and obj.get("lesson") is not None:
                bells.append(obj)
            elif obj.get("className") and obj.get("day"):
                if str(obj.get("id") or "").startswith("change") or obj.get("note"):
                    changes.append(obj)
                else:
                    lessons.append(obj)
    return {"lessons": lessons, "changes": changes, "bells": bells}


def snapshot_from_payload(payload: dict, source_url: str, pages: list[str]) -> ScheduleSnapshot:
    lessons, bells = parse_schedule_api(payload)
    snap = ScheduleSnapshot(source_url=source_url, pages=pages, bells=bells)
    snap.text = lessons_to_text(lessons)[:MAX_TEXT_CHARS]
    if not snap.text:
        snap.text = "На сайте сейчас нет уроков."
    snap.fingerprint = fingerprint_of(snap)
    return snap


def has_lesson_lines(text: str) -> bool:
    return any(parse_lesson_line(line) for line in (text or "").splitlines())


def google_sheet_csv_export_url(spreadsheet_id: str, gid: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        f"?format=csv&gid={gid}"
    )


def google_sheets_csv_url(url: str) -> str | None:
    parsed = urlparse(unescape(url))
    if "docs.google.com" not in parsed.netloc or "/spreadsheets/d/e/" not in parsed.path:
        return None
    parts = parsed.path.split("/")
    try:
        idx = parts.index("e")
        doc_id = parts[idx + 1]
    except (ValueError, IndexError):
        return None
    gid = parse_qs(parsed.query).get("gid", ["0"])[0]
    return f"https://docs.google.com/spreadsheets/d/e/{doc_id}/pub?gid={gid}&single=true&output=csv"


def _is_day_row(cells: list[str]) -> str | None:
    first = _norm_space(cells[0] if cells else "").lower()
    first = first.replace("ё", "е")
    for name in DAY_NAMES:
        if first.startswith(name):
            return name.upper()
    return None


def _is_class_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    return bool(re.search(r"\d+\s*[а-яa-z]", joined)) and not re.match(r"^\d{1,2}[.:]", cells[0] if cells else "")


def parse_timetable_csv(csv_text: str, sheet: str = "Расписание") -> list[Lesson]:
    reader = csv.reader(io.StringIO(csv_text))
    day = ""
    classes: list[str] = []
    lessons: list[Lesson] = []
    for raw in reader:
        cells = [_norm_space(c) for c in raw]
        if not any(cells):
            continue
        maybe_day = _is_day_row(cells)
        if maybe_day:
            day = maybe_day
            classes = []
            continue
        if not day:
            continue
        if cells and cells[0].lower().startswith(("кабинет", "педагог")):
            continue
        if _is_class_header(cells) and (not cells[1] or not cells[1].isdigit()):
            # class names start after time/number columns
            start = 0
            if len(cells) >= 3 and not cells[0] and not cells[1]:
                start = 2
            elif len(cells) >= 2 and not cells[0]:
                start = 1
            classes = [c for c in cells[start:] if c]
            continue
        if not classes:
            continue
        time = cells[0] if cells else ""
        num = cells[1] if len(cells) > 1 else ""
        if not re.match(r"^\d+$", num or ""):
            if re.match(r"^\d{1,2}[.:]", time) and len(cells) > 2:
                num = ""
            else:
                continue
        subjects = cells[2:] if len(cells) > 2 else []
        for i, class_name in enumerate(classes):
            subject = subjects[i] if i < len(subjects) else ""
            if not subject:
                continue
            lessons.append(
                Lesson(
                    sheet=sheet,
                    day=day,
                    class_name=class_name,
                    num=num or "?",
                    time=time,
                    subject=subject,
                )
            )
    return lessons


def lessons_to_text(lessons: Iterable[Lesson]) -> str:
    return "\n".join(les.line() for les in lessons)


def infer_sheet_kind(lessons: list[Lesson]) -> str:
    """Вкладка «Изменения» часто содержит обычную сетку. Редкие ячейки — замены."""
    if not lessons:
        return "Расписание"
    per_class_day = Counter((normalize_class_name(item.class_name), item.day) for item in lessons)
    avg = sum(per_class_day.values()) / max(len(per_class_day), 1)
    return "Изменения" if avg <= 1.5 else "Расписание"


def lessons_with_sheet(lessons: Iterable[Lesson], sheet: str) -> list[Lesson]:
    return [
        Lesson(
            sheet=sheet,
            day=item.day,
            class_name=item.class_name,
            num=item.num,
            time=item.time,
            subject=item.subject,
        )
        for item in lessons
    ]


def lesson_slot_key(item: Lesson) -> tuple:
    return (
        normalize_class_name(item.class_name),
        item.day,
        str(item.num),
        is_changes_sheet(item.sheet),
    )


def merge_lessons(primary: list[Lesson], extra: list[Lesson]) -> list[Lesson]:
    index = {lesson_slot_key(item): i for i, item in enumerate(primary)}
    out = list(primary)
    for item in extra:
        key = lesson_slot_key(item)
        if key not in index:
            index[key] = len(out)
            out.append(item)
            continue
        i = index[key]
        if len(item.subject) > len(out[i].subject):
            out[i] = item
    return out


LAT_TO_CYR = str.maketrans({
    "a": "а",
    "b": "б",
    "v": "в",
    "g": "г",
    "d": "д",
    "e": "е",
    "k": "к",
    "m": "м",
    "h": "н",
    "o": "о",
    "p": "р",
    "c": "с",
    "t": "т",
    "y": "у",
    "x": "х",
})
DAY_PREPOS = {
    "понедельник": "понедельник",
    "вторник": "вторник",
    "среда": "среду",
    "четверг": "четверг",
    "пятница": "пятницу",
    "суббота": "субботу",
    "воскресенье": "воскресенье",
}


def normalize_class_name(name: str) -> str:
    s = _norm_space(name).lower().replace("ё", "е")
    s = re.sub(r"[\s.\-]", "", s)
    return s.translate(LAT_TO_CYR)


def class_matches(actual: str, needle: str) -> bool:
    if not needle:
        return True
    return normalize_class_name(actual) == normalize_class_name(needle)


def split_class_filters(raw: str) -> list[str]:
    if not _norm_space(raw):
        return [""]
    parts = re.split(r"[,;/]| и ", raw, flags=re.I)
    out: list[str] = []
    for part in parts:
        part = re.sub(r"^класс\s+", "", _norm_space(part), flags=re.I)
        if part:
            out.append(part)
    return out or [""]


def is_changes_sheet(sheet: str) -> bool:
    low = sheet.lower()
    return "изменен" in low or "замен" in low


def parse_lesson_line(line: str) -> tuple[str, str, str, str] | None:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None
    return parts[0], parts[1], parts[2], " | ".join(parts[3:])


def filter_lines_for_class(text: str, class_name: str) -> str:
    """Оставляет только строки уроков выбранного класса (точное совпадение 10А / 10а / 10A)."""
    needle = _norm_space(class_name)
    if not needle:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        parsed = parse_lesson_line(line)
        if parsed and class_matches(parsed[2], needle):
            kept.append(line)
    return "\n".join(kept)


def classes_in_snapshot(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        parsed = parse_lesson_line(line)
        if not parsed:
            continue
        klass = parsed[2]
        key = normalize_class_name(klass)
        if key in seen:
            continue
        seen.add(key)
        found.append(klass)

    def sort_key(name: str) -> tuple:
        match = re.match(r"^(\d+)(.*)$", normalize_class_name(name))
        if not match:
            return (99, name)
        return (int(match.group(1)), match.group(2))

    return sorted(found, key=sort_key)


def sibling_classes(needle: str, text: str) -> list[str]:
    grade_match = re.match(r"^(\d+)", normalize_class_name(needle))
    if not grade_match:
        return []
    grade = grade_match.group(1)
    return [
        klass
        for klass in classes_in_snapshot(text)
        if normalize_class_name(klass).startswith(grade)
        and not class_matches(klass, needle)
    ]


def format_day_schedule(
    text: str,
    *,
    class_filter: str = "",
    now: datetime | None = None,
    days_ahead: int = 0,
    limit: int = 3500,
    week: bool | None = None,
) -> str:
    """Стандартное расписание из таблиц 1–4 и 5–11. Замены — отдельным блоком, если они есть."""
    if week is None:
        week = bool(class_filter)
    when = school_now(now) + timedelta(days=days_ahead)
    want_days = [name.upper() for name in DAY_NAMES]
    if not week:
        want_days = [DAY_NAMES[when.weekday()].upper()]

    parsed_rows: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        parsed = parse_lesson_line(line)
        if not parsed:
            continue
        sheet, day, klass, lesson = parsed
        if class_filter and not class_matches(klass, class_filter):
            continue
        if day not in want_days:
            continue
        parsed_rows.append((sheet, day, klass, lesson))

    standard = [row for row in parsed_rows if not is_changes_sheet(row[0])]
    changes = [row for row in parsed_rows if is_changes_sheet(row[0])]

    if week and class_filter:
        header = f"📅 Расписание СОШ №46, класс {class_filter}"
    elif class_filter:
        day_name = DAY_NAMES[when.weekday()]
        header = (
            f"📅 Расписание СОШ №46 на {DAY_PREPOS[day_name]}, "
            f"{when.strftime('%d.%m.%Y')} ({class_filter})"
        )
    else:
        day_name = DAY_NAMES[when.weekday()]
        header = (
            f"📅 Расписание СОШ №46 на {DAY_PREPOS[day_name]}, "
            f"{when.strftime('%d.%m.%Y')}"
        )

    if not standard and not changes:
        other_days: list[str] = []
        if class_filter and not week:
            seen_days: set[str] = set()
            for line in text.splitlines():
                parsed = parse_lesson_line(line)
                if not parsed:
                    continue
                _sheet, day, klass, _lesson = parsed
                if class_matches(klass, class_filter) and day not in seen_days:
                    seen_days.add(day)
                    other_days.append(day)
            if other_days:
                shown = ", ".join(other_days)
                return (
                    f"{header}\nНа этот день в сетке пока пусто. "
                    f"На сайте сейчас есть: {shown}.\n"
                    f"Напишите «неделя», чтобы увидеть всё.\n"
                    f"{DEFAULT_SCHOOL_URL}"
                )
        if class_filter:
            siblings = sibling_classes(class_filter, text)
            extra = (
                f"Сейчас в параллели есть: {', '.join(siblings)}.\n"
                if siblings
                else "Проверьте написание (например 8А или 8Д).\n"
            )
            return (
                f"{header}\nВ таблице нет класса {class_filter}. {extra}"
                f"{DEFAULT_SCHOOL_URL}"
            )
        return (
            f"{header}\nВ стандартном расписании нет уроков. "
            f"Напишите класс, например <code>класс 8Д</code>.\n"
            f"{DEFAULT_SCHOOL_URL}"
        )

    chunks = [header]
    if standard:
        chunks.append("\nОсновное расписание:")
        chunks.extend(_format_grouped_lessons(standard, by_day=week))
    else:
        chunks.append("\nОсновное расписание на сайте сейчас пустое.")

    if changes:
        chunks.append("\n⚠ Изменения (замены), если выложили:")
        chunks.extend(_format_grouped_lessons(changes, by_day=True))
    elif class_filter:
        chunks.append("\nЗамен на сайте сейчас нет.")

    body = "\n".join(chunks)
    return body + f"\n\nИсточник: {DEFAULT_SCHOOL_URL}"


def format_changes_schedule(text: str, class_filter: str = "") -> str:
    rows: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        parsed = parse_lesson_line(line)
        if not parsed:
            continue
        sheet, day, klass, lesson = parsed
        if not is_changes_sheet(sheet):
            continue
        if class_filter and not class_matches(klass, class_filter):
            continue
        rows.append((sheet, day, klass, lesson))
    header = "⚠ Замены СОШ №46"
    if class_filter:
        header += f" ({class_filter})"
    if not rows:
        return (
            f"{header}\nЗамен на сайте сейчас нет — действует обычное расписание.\n\n"
            f"Источник: {DEFAULT_SCHOOL_URL}"
        )
    chunks = [header]
    chunks.extend(_format_grouped_lessons(rows, by_day=True))
    return "\n".join(chunks) + f"\n\nИсточник: {DEFAULT_SCHOOL_URL}"


def _format_grouped_lessons(
    rows: list[tuple[str, str, str, str]],
    *,
    by_day: bool,
) -> list[str]:
    day_order = {name.upper(): i for i, name in enumerate(DAY_NAMES)}
    grouped: dict[str, list[str]] = {}
    keys: list[str] = []
    for sheet, day, klass, lesson in rows:
        if by_day:
            key = f"{day} · {klass}"
        else:
            key = f"{klass} · {sheet}"
        if key not in grouped:
            grouped[key] = []
            keys.append(key)
        grouped[key].append(lesson)

    def sort_key(key: str) -> tuple:
        head = key.split(" · ", 1)[0]
        return (day_order.get(head, 99), key)

    out: list[str] = []
    for key in sorted(keys, key=sort_key):
        out.append(f"\n{key}")
        out.extend(grouped[key])
    return out


def school_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(SCHOOL_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=SCHOOL_TZ)
    return now.astimezone(SCHOOL_TZ)


def next_clock_at(hour: int, minute: int = 0, now: datetime | None = None) -> datetime:
    now = school_now(now)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def parse_hours(start_hour: int = 8, every_hours: int = 2, end_hour: int = 22) -> tuple[int, ...]:
    if every_hours < 1:
        every_hours = 1
    return tuple(range(start_hour, end_hour + 1, every_hours))


def last_due_parse_slot(
    now: datetime | None = None,
    *,
    start_hour: int = 8,
    every_hours: int = 2,
    end_hour: int = 22,
) -> datetime | None:
    """Последний слот проверки, который уже наступил сегодня (8:00, 10:00, …)."""
    now = school_now(now)
    hours = parse_hours(start_hour, every_hours, end_hour)
    due: datetime | None = None
    for hour in hours:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot <= now:
            due = slot
    return due


def next_parse_at(
    now: datetime | None = None,
    *,
    start_hour: int = 8,
    every_hours: int = 2,
    end_hour: int = 22,
) -> datetime:
    """Следующий слот строго в будущем."""
    now = school_now(now)
    hours = parse_hours(start_hour, every_hours, end_hour)
    for hour in hours:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=start_hour, minute=0, second=0, microsecond=0)


def weekday_ru(now: datetime | None = None) -> str:
    return DAY_NAMES[school_now(now).weekday()].upper()


def sheet_title_from_page(page_url: str, fallback: str = "Расписание") -> str:
    path = urlparse(page_url).path.lower()
    if "1-4" in path:
        return "1-4 классы"
    if "5-8" in path or "5-11" in path:
        return "5-11 классы"
    if "zvonk" in path:
        return "Звонки"
    if "raspisanie" in path:
        return "Изменения"
    return fallback


def extract_bells(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    raw_lines = [_norm_space(unescape(line)) for line in soup.get_text("\n").splitlines()]
    kept: list[str] = []
    started = False
    for line in raw_lines:
        if not line:
            continue
        low = line.lower()
        if "понедельник" in low and not started:
            started = True
        if not started:
            continue
        if kept and low in {"расписание", "изменения в расписании", "листать вверх"}:
            break
        if kept and line.startswith("©"):
            break
        if re.search(r"урок", line, re.I) or "понедельник" in low or "вторник" in low or "суббот" in low:
            kept.append(line.replace("&#8212;", "—").replace("—", "—"))
        if len(kept) >= 30:
            break
    return "\n".join(kept)


def format_bells(bells: str, now: datetime | None = None) -> str:
    header = "🔔 Расписание звонков СОШ №46"
    if not bells.strip():
        return (
            f"{header}\nНа сайте сетка звонков сейчас не разобралась.\n"
            f"{DEFAULT_SCHOOL_URL}"
        )
    weekday = school_now(now).weekday()
    label = "сегодня понедельник" if weekday == 0 else "сегодня вторник–суббота"
    if weekday == 6:
        label = "сегодня воскресенье"
    return f"{header} ({label})\n\n{bells}\n\n{DEFAULT_SCHOOL_URL}"


def build_snapshot_text(page_url: str, html: str, class_filter: str = "") -> tuple[str, list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    title = _norm_space(soup.title.get_text() if soup.title else "")
    tables = extract_tables(soup)
    body = visible_text(soup)
    page_is_schedule = _looks_like_schedule(f"{title} {page_url} {tables} {body[:3000]}")
    pages, files = collect_links(soup, page_url, keep_all_files=page_is_schedule)
    iframes = extract_iframes(html, page_url)

    parts = [f"Страница: {title or page_url}", f"URL: {page_url}"]
    if tables:
        parts.append("Таблицы:")
        parts.append(tables)
    if iframes:
        parts.append("Таблицы Google:")
        parts.extend(f"- {name}: {url}" for url, name in iframes)
    text = "\n".join(parts)
    text = "\n".join(_norm_space(line) for line in text.splitlines() if _norm_space(line))
    return text[:MAX_TEXT_CHARS], pages, files, iframes


def fingerprint_of(snapshot: ScheduleSnapshot) -> str:
    payload = snapshot.text + "\n" + "\n".join(
        sorted(f"{f.url}|{f.sha256}" for f in snapshot.files)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _page_blob(snap: ScheduleSnapshot) -> str:
    return " ".join([snap.source_url, *snap.pages]).lower()


def _is_legacy_sosh_snapshot(snap: ScheduleSnapshot) -> bool:
    blob = _page_blob(snap)
    return "sosh46.ru" in blob and "raspisanie" in blob


def _is_new_sosh_snapshot(snap: ScheduleSnapshot) -> bool:
    blob = _page_blob(snap)
    return "sosh46.ru" in blob and ("/schedule" in blob or "/api/schedule" in blob)


def describe_changes(
    old: ScheduleSnapshot | None,
    new: ScheduleSnapshot,
) -> str | None:
    if old is None:
        return None
    if old.fingerprint == new.fingerprint:
        return None

    if _is_legacy_sosh_snapshot(old) and _is_new_sosh_snapshot(new):
        return "Сайт СОШ №46 обновился. Дальше слежу за новым расписанием и заменами."

    lines: list[str] = []

    old_files = {f.key(): f for f in old.files}
    new_files = {f.key(): f for f in new.files}
    added_files = [f for k, f in new_files.items() if k not in old_files]
    removed_files = [f for k, f in old_files.items() if k not in new_files]
    updated_files = [
        new_files[k]
        for k in new_files
        if k in old_files and old_files[k].sha256 and new_files[k].sha256 and old_files[k].sha256 != new_files[k].sha256
    ]
    if added_files:
        lines.append("📄 Новые файлы:")
        for f in added_files:
            lines.append(f"• {f.title}\n  {f.url}")
    if removed_files:
        lines.append("🗑️ Файлы пропали:")
        for f in removed_files:
            lines.append(f"• {f.title}\n  {f.url}")
    if updated_files:
        lines.append("♻️ Файлы обновились (содержимое другое):")
        for f in updated_files:
            lines.append(f"• {f.title}\n  {f.url}")

    old_set = [ln for ln in old.text.splitlines() if ln]
    new_set = [ln for ln in new.text.splitlines() if ln]
    old_uniq = set(old_set)
    new_uniq = set(new_set)
    added = [ln for ln in new_set if ln not in old_uniq]
    removed = [ln for ln in old_set if ln not in new_uniq]
    added = [ln for ln in added if not ln.startswith("URL:") and not ln.startswith("Страница:")]
    removed = [ln for ln in removed if not ln.startswith("URL:") and not ln.startswith("Страница:")]

    def clip(items: Iterable[str], limit: int = 18) -> list[str]:
        items = list(items)
        extra = len(items) - limit
        shown = items[:limit]
        if extra > 0:
            shown.append(f"… ещё {extra}")
        return shown

    if added:
        lines.append("➕ Появилось в расписании:")
        lines.extend(f"• {ln}" for ln in clip(added))
    if removed:
        lines.append("➖ Пропало из расписания:")
        lines.extend(f"• {ln}" for ln in clip(removed))

    if not lines:
        return None
    return "\n".join(lines)


async def _fetch(session, url: str, ssl: bool | None) -> tuple[bytes, str, str]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=25)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "ru,en;q=0.8",
    }
    async with session.get(url, timeout=timeout, headers=headers, ssl=ssl, allow_redirects=True) as resp:
        resp.raise_for_status()
        data = await resp.read()
        ctype = (resp.content_type or "").lower()
        return data, str(resp.url), ctype


def seed_pages(source_url: str) -> list[str]:
    source_url = canonicalize_school_url(source_url)
    if "sosh46.ru" in source_url:
        return [DEFAULT_SCHOOL_URL]
    return [source_url]


async def collect_sosh46(session, ssl) -> ScheduleSnapshot | None:
    pages: list[str] = []
    sheet_lessons: list[Lesson] = []
    for gid, title in SOSH46_SHEET_TABS:
        url = google_sheet_csv_export_url(SOSH46_SHEET_ID, gid)
        try:
            raw, final_url, _ctype = await _fetch(session, url, ssl)
            pages.append(str(final_url))
            parsed = parse_timetable_csv(decode_bytes(raw), sheet=title)
            kind = infer_sheet_kind(parsed)
            sheet_lessons.extend(lessons_with_sheet(parsed, kind))
        except Exception:
            log.exception("Не удалось скачать вкладку расписания gid=%s", gid)

    api_lessons: list[Lesson] = []
    bells = ""
    try:
        raw, final_url, _ctype = await _fetch(session, SOSH46_API_URL, ssl)
        pages.append(str(final_url))
        payload = json.loads(decode_bytes(raw))
        if isinstance(payload, dict):
            api_lessons, bells = parse_schedule_api(payload)
    except Exception:
        log.exception("Не удалось прочитать %s", SOSH46_API_URL)

    lessons = merge_lessons(sheet_lessons, api_lessons)
    if not lessons and not bells:
        return None
    snap = ScheduleSnapshot(
        source_url=DEFAULT_SCHOOL_URL,
        pages=pages or [DEFAULT_SCHOOL_URL],
        bells=bells,
        text=lessons_to_text(lessons)[:MAX_TEXT_CHARS] or "На сайте сейчас нет уроков.",
    )
    snap.fingerprint = fingerprint_of(snap)
    return snap


async def collect_schedule(
    source_url: str,
    *,
    class_filter: str = "",
    insecure_ssl: bool = False,
    session=None,
) -> ScheduleSnapshot:
    import aiohttp

    ssl: bool | None = False if insecure_ssl else None
    own_session = session is None
    source_url = canonicalize_school_url(source_url)
    if own_session:
        session = aiohttp.ClientSession()
    try:
        snap = ScheduleSnapshot(source_url=source_url)
        if sosh46_schedule_api_url(source_url):
            sosh = await collect_sosh46(session, ssl)
            if sosh and has_lesson_lines(sosh.text):
                return sosh
            try:
                raw, final_url, _ctype = await _fetch(session, DEFAULT_SCHOOL_URL, ssl)
                payload = parse_next_flight_payload(decode_bytes(raw))
                snap = snapshot_from_payload(
                    payload, source_url, [DEFAULT_SCHOOL_URL, str(final_url)]
                )
                if has_lesson_lines(snap.text):
                    return snap
            except Exception:
                log.exception("Не удалось разобрать HTML %s", DEFAULT_SCHOOL_URL)

        texts: list[str] = []
        file_queue: list[tuple[str, str]] = []
        sheet_queue: list[tuple[str, str]] = []
        seen_pages: set[str] = set()

        start_pages = seed_pages(source_url)
        extra_pages: list[str] = []

        async def ingest_html(page_url: str) -> None:
            key = page_url.rstrip("/")
            if key in seen_pages:
                return
            seen_pages.add(key)
            raw, final_url, ctype = await _fetch(session, page_url, ssl)
            snap.pages.append(final_url)
            if any(final_url.lower().split("?")[0].endswith(ext) for ext in FILE_EXT) or "pdf" in ctype:
                digest = hashlib.sha256(raw).hexdigest() if len(raw) <= MAX_FILE_BYTES else ""
                snap.files.append(ScheduleFile(url=final_url, title="Расписание", sha256=digest))
                texts.append(f"Файл расписания: {final_url}\nsha256: {digest}")
                return
            html = decode_bytes(raw)
            if "zvonk" in final_url.lower() or "звонк" in html[:2000].lower():
                bells = extract_bells(html)
                if bells:
                    snap.bells = bells
            text, page_links, file_links, iframes = build_snapshot_text(final_url, html, class_filter)
            texts.append(text)
            file_queue.extend(file_links)
            for iframe_url, iframe_title in iframes:
                csv_url = google_sheets_csv_url(iframe_url)
                sheet_queue.append((csv_url or iframe_url, iframe_title or sheet_title_from_page(final_url)))
            for url, _title in page_links:
                if url.rstrip("/") not in seen_pages:
                    extra_pages.append(url)

        for page_url in start_pages:
            try:
                await ingest_html(page_url)
            except Exception:
                continue

        for page_url in extra_pages:
            if len(seen_pages) >= MAX_PAGES:
                break
            try:
                await ingest_html(page_url)
            except Exception:
                continue

        seen_sheets: set[str] = set()
        all_lessons: list[Lesson] = []
        for sheet_url, title in sheet_queue:
            key = sheet_url.rstrip("/")
            if key in seen_sheets:
                continue
            seen_sheets.add(key)
            try:
                raw, final, ctype = await _fetch(session, sheet_url, ssl)
            except Exception:
                continue
            body = decode_bytes(raw)
            if "csv" in ctype or sheet_url.endswith("csv") or "output=csv" in sheet_url:
                lessons = parse_timetable_csv(body, sheet=title or "Расписание")
                all_lessons.extend(lessons)
                if not lessons:
                    texts.append(f"{title}: таблица пустая или без уроков")
            else:
                texts.append(body[:4000])

        lesson_text = lessons_to_text(all_lessons)
        if lesson_text:
            texts.insert(0, lesson_text)

        seen_files: set[str] = set()
        for furl, title in file_queue:
            key = furl.rstrip("/")
            if key in seen_files or len(snap.files) >= MAX_FILES:
                continue
            seen_files.add(key)
            digest = ""
            try:
                fraw, ffinal, _ = await _fetch(session, furl, ssl)
                if len(fraw) <= MAX_FILE_BYTES:
                    digest = hashlib.sha256(fraw).hexdigest()
                furl = ffinal
            except Exception:
                pass
            snap.files.append(ScheduleFile(url=furl, title=title or furl, sha256=digest))

        snap.text = "\n".join(t for t in texts if t)[:MAX_TEXT_CHARS]
        snap.fingerprint = fingerprint_of(snap)
        return snap
    finally:
        if own_session:
            await session.close()


async def preview(class_filter: str = "") -> None:
    """Скачать сайт и показать, что бот отправил бы в чат. Для проверки без Telegram."""
    snap = await collect_schedule(DEFAULT_SCHOOL_URL, class_filter=class_filter)
    print("Страницы:")
    for page in snap.pages:
        print(" ", page)
    print("Снимок:", snap.fingerprint[:16], "символов:", len(snap.text))
    print()
    print("===== сегодня =====")
    print(format_day_schedule(snap.text, class_filter=class_filter, days_ahead=0, week=False))
    print()
    print("===== завтра =====")
    print(format_day_schedule(snap.text, class_filter=class_filter, days_ahead=1, week=False))
    print()
    print("===== неделя =====")
    print(format_day_schedule(snap.text, class_filter=class_filter, week=True))


if __name__ == "__main__":
    import asyncio
    import sys

    filt = " ".join(sys.argv[1:]).strip()
    asyncio.run(preview(filt))
