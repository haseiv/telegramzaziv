"""Парсинг сайта СОШ №46 и сравнение снимков расписания."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

DEFAULT_SCHOOL_URL = "https://sosh46.ru/raspisanie/"
SOSH46_PAGES = (
    "https://sosh46.ru/raspisanie/",
    "https://sosh46.ru/raspisanie-1-4-klass/",
    "https://sosh46.ru/raspisanie-5-8-klass/",
    "https://sosh46.ru/raspisanie-zvonkov/",
)
MOSCOW = ZoneInfo("Europe/Moscow")

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

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "pages": self.pages,
            "text": self.text,
            "files": [
                {"url": f.url, "title": f.title, "sha256": f.sha256} for f in self.files
            ],
            "fingerprint": self.fingerprint,
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
        if cells and cells[0].lower().startswith("кабинет"):
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


def filter_lines_for_class(text: str, class_name: str) -> str:
    needle = _norm_space(class_name)
    if not needle:
        return text
    variants = {
        needle.lower(),
        needle.lower().replace("а", "a").replace("б", "b").replace("в", "b"),
        re.sub(r"[\s\-]", "", needle.lower()),
    }
    kept: list[str] = []
    for line in text.splitlines():
        compact = re.sub(r"[\s\-]", "", line.lower())
        if any(v and (v in compact or v in line.lower()) for v in variants if v):
            kept.append(line)
    return "\n".join(kept) if kept else text


def moscow_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(MOSCOW)
    if now.tzinfo is None:
        return now.replace(tzinfo=MOSCOW)
    return now.astimezone(MOSCOW)


def weekday_ru(now: datetime | None = None) -> str:
    return DAY_NAMES[moscow_now(now).weekday()].upper()


def format_day_schedule(
    text: str,
    *,
    class_filter: str = "",
    now: datetime | None = None,
    limit: int = 3500,
) -> str:
    day = weekday_ru(now)
    date = moscow_now(now).strftime("%d.%m.%Y")
    lines = text.splitlines()
    if class_filter:
        lines = filter_lines_for_class("\n".join(lines), class_filter).splitlines()
    today = [ln for ln in lines if f"| {day} |" in ln]
    header = f"📅 Расписание СОШ №46 на {day.lower()}, {date}"
    if class_filter:
        header += f" ({class_filter})"
    if not today:
        return (
            f"{header}\nПока в таблице нет уроков на этот день "
            f"(каникулы или ещё не выложили).\n"
            f"{DEFAULT_SCHOOL_URL}"
        )
    grouped: dict[str, list[str]] = {}
    for ln in today:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 4:
            continue
        sheet, _day, klass, lesson = parts[0], parts[1], parts[2], " | ".join(parts[3:])
        key = f"{klass} · {sheet}"
        grouped.setdefault(key, []).append(lesson)
    chunks = [header]
    for key, items in grouped.items():
        chunks.append(f"\n{key}")
        chunks.extend(items)
    body = "\n".join(chunks)
    return body + f"\n\nИсточник: {DEFAULT_SCHOOL_URL}"


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
    text = filter_lines_for_class(text, class_filter)
    text = "\n".join(_norm_space(line) for line in text.splitlines() if _norm_space(line))
    return text[:MAX_TEXT_CHARS], pages, files, iframes


def fingerprint_of(snapshot: ScheduleSnapshot) -> str:
    payload = snapshot.text + "\n" + "\n".join(
        sorted(f"{f.url}|{f.sha256}" for f in snapshot.files)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_changes(
    old: ScheduleSnapshot | None,
    new: ScheduleSnapshot,
) -> str | None:
    if old is None:
        return None
    if old.fingerprint == new.fingerprint:
        return None

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
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    async with session.get(url, timeout=timeout, headers=headers, ssl=ssl, allow_redirects=True) as resp:
        resp.raise_for_status()
        data = await resp.read()
        ctype = (resp.content_type or "").lower()
        return data, str(resp.url), ctype


def seed_pages(source_url: str) -> list[str]:
    pages = [source_url]
    if "sosh46.ru" in source_url:
        for extra in SOSH46_PAGES:
            if extra.rstrip("/") not in {p.rstrip("/") for p in pages}:
                pages.append(extra)
    return pages


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
    if own_session:
        session = aiohttp.ClientSession()
    try:
        snap = ScheduleSnapshot(source_url=source_url)
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
        if class_filter:
            lesson_text = filter_lines_for_class(lesson_text, class_filter)
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
