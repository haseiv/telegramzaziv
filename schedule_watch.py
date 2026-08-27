"""Парсинг школьного сайта и сравнение снимков расписания."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

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


@dataclass
class ScheduleFile:
    url: str
    title: str
    sha256: str = ""

    def key(self) -> str:
        return self.url.rstrip("/")


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
    for enc in ("utf-8", "cp1251", "koi8-r"):
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
    # fallback if the page is almost empty after filtering
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
        # stay on the same host for HTML pages
        if parsed.netloc and origin.netloc and parsed.netloc != origin.netloc:
            continue
        if _looks_like_schedule(hay):
            pages.append((key, title))
    return pages, files


def filter_lines_for_class(text: str, class_name: str) -> str:
    needle = _norm_space(class_name)
    if not needle:
        return text
    variants = {needle.lower(), needle.lower().replace("а", "a").replace("б", "b").replace("в", "b")}
    variants.add(re.sub(r"[\s\-]", "", needle.lower()))
    kept: list[str] = []
    for line in text.splitlines():
        compact = re.sub(r"[\s\-]", "", line.lower())
        if any(v and (v in compact or v in line.lower()) for v in variants if v):
            kept.append(line)
    return "\n".join(kept) if kept else text


def build_snapshot_text(page_url: str, html: str, class_filter: str = "") -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    title = _norm_space(soup.title.get_text() if soup.title else "")
    tables = extract_tables(soup)
    body = visible_text(soup)
    page_is_schedule = _looks_like_schedule(f"{title} {page_url} {tables} {body[:3000]}")
    pages, files = collect_links(soup, page_url, keep_all_files=page_is_schedule)

    parts = [f"Страница: {title or page_url}", f"URL: {page_url}"]
    if tables:
        parts.append("Таблицы:")
        parts.append(tables)
    if body:
        parts.append("Текст:")
        parts.append(body)
    if files:
        parts.append("Файлы:")
        parts.extend(f"- {name}: {url}" for url, name in files)
    text = "\n".join(parts)
    text = filter_lines_for_class(text, class_filter)
    text = "\n".join(_norm_space(line) for line in text.splitlines() if _norm_space(line))
    return text[:MAX_TEXT_CHARS], pages, files


def fingerprint_of(snapshot: ScheduleSnapshot) -> str:
    payload = snapshot.text + "\n" + "\n".join(
        sorted(f"{f.url}|{f.sha256}" for f in snapshot.files)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_changes(
    old: ScheduleSnapshot | None,
    new: ScheduleSnapshot,
) -> str | None:
    """Человекочитаемый отчёт. None — изменений нет."""
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
    # drop noisy URL-only lines if the rest already explains the change
    added = [ln for ln in added if not ln.startswith("URL:")]
    removed = [ln for ln in removed if not ln.startswith("URL:")]

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
        raw, final_url, ctype = await _fetch(session, source_url, ssl)
        snap = ScheduleSnapshot(source_url=source_url, pages=[final_url])
        texts: list[str] = []
        file_queue: list[tuple[str, str]] = []

        if any(final_url.lower().split("?")[0].endswith(ext) for ext in FILE_EXT) or "pdf" in ctype:
            digest = hashlib.sha256(raw).hexdigest() if len(raw) <= MAX_FILE_BYTES else ""
            snap.files.append(ScheduleFile(url=final_url, title="Расписание", sha256=digest))
            texts.append(f"Файл расписания: {final_url}\nsha256: {digest}")
        else:
            html = decode_bytes(raw)
            text, page_links, file_links = build_snapshot_text(final_url, html, class_filter)
            texts.append(text)
            file_queue.extend(file_links)
            queued_pages = [url for url, _title in page_links if url.rstrip("/") != final_url.rstrip("/")]
            seen_pages = {final_url.rstrip("/")}
            for page_url in queued_pages[: MAX_PAGES - 1]:
                key = page_url.rstrip("/")
                if key in seen_pages:
                    continue
                seen_pages.add(key)
                try:
                    praw, pfinal, _ = await _fetch(session, page_url, ssl)
                except Exception:
                    continue
                snap.pages.append(pfinal)
                phtml = decode_bytes(praw)
                ptext, _more_pages, more_files = build_snapshot_text(pfinal, phtml, class_filter)
                texts.append(ptext)
                file_queue.extend(more_files)

        seen_files: set[str] = set()
        for furl, title in file_queue:
            key = furl.rstrip("/")
            if key in seen_files or len(snap.files) >= MAX_FILES:
                continue
            seen_files.add(key)
            digest = ""
            try:
                fraw, ffinal, _ = await _fetch(session, furl, ssl)
                key = ffinal.rstrip("/")
                if len(fraw) <= MAX_FILE_BYTES:
                    digest = hashlib.sha256(fraw).hexdigest()
                furl = ffinal
            except Exception:
                pass
            snap.files.append(ScheduleFile(url=furl, title=title or furl, sha256=digest))

        snap.text = "\n\n".join(t for t in texts if t)[:MAX_TEXT_CHARS]
        snap.fingerprint = fingerprint_of(snap)
        return snap
    finally:
        if own_session:
            await session.close()
