import unittest
from datetime import datetime, timezone, timedelta

from schedule_watch import (
    ScheduleFile,
    ScheduleSnapshot,
    build_snapshot_text,
    describe_changes,
    filter_lines_for_class,
    fingerprint_of,
    format_day_schedule,
    google_sheets_csv_url,
    parse_timetable_csv,
)

HOME_HTML = """
<html><head><title>МАОУ СОШ №1</title></head>
<body>
  <nav>Главная Контакты</nav>
  <a href="/about">О школе</a>
  <a href="/raspisanie">Расписание уроков</a>
  <a href="/files/zvonki.pdf">Расписание звонков</a>
  <p>Добро пожаловать</p>
</body></html>
"""

SCHEDULE_V1 = """
<html><head><title>Расписание</title></head>
<body>
  <h1>Расписание уроков</h1>
  <table>
    <tr><th>Урок</th><th>8А</th><th>8Б</th></tr>
    <tr><td>1</td><td>Математика</td><td>Русский</td></tr>
    <tr><td>2</td><td>История</td><td>Физика</td></tr>
  </table>
  <a href="/files/rasp.pdf">Скачать расписание</a>
</body></html>
"""

SCHEDULE_V2 = """
<html><head><title>Расписание</title></head>
<body>
  <h1>Расписание уроков</h1>
  <p>Изменения на четверг: у 8А математика перенесена</p>
  <table>
    <tr><th>Урок</th><th>8А</th><th>8Б</th></tr>
    <tr><td>1</td><td>Литература</td><td>Русский</td></tr>
    <tr><td>2</td><td>История</td><td>Физика</td></tr>
  </table>
  <a href="/files/rasp.pdf">Скачать расписание</a>
</body></html>
"""

SOSH_HTML = """
<html><head><title>Расписание</title></head>
<body>
<iframe src="https://docs.google.com/spreadsheets/d/e/2PACX-abc/pubhtml?gid=656166446&amp;single=true&amp;widget=true&amp;headers=false"></iframe>
</body></html>
"""

CSV_SAMPLE = """ПОНЕДЕЛЬНИК,,,,,,,,
,,5А,5Б,8А
Кабинет,,314,219,301
09.00-9.40,1,Разговоры о важном,Русский,История 322
09.50-10.30,2,Математика,,Химия
ЧЕТВЕРГ,,,,,,,,
,,5А,5Б,8А
09.00-9.40,1,Физкультура,Русский,Алгебра
"""


class ScheduleParseTests(unittest.TestCase):
    def test_finds_schedule_links_on_home(self):
        text, pages, files, iframes = build_snapshot_text("https://school.test/", HOME_HTML)
        page_urls = [u for u, _ in pages]
        file_urls = [u for u, _ in files]
        self.assertIn("https://school.test/raspisanie", page_urls)
        self.assertIn("https://school.test/files/zvonki.pdf", file_urls)
        self.assertEqual(iframes, [])

    def test_tables_extracted(self):
        text, _pages, files, _iframes = build_snapshot_text("https://school.test/raspisanie", SCHEDULE_V1)
        self.assertIn("Математика", text)
        self.assertIn("8А", text)
        self.assertTrue(any(u.endswith("rasp.pdf") for u, _ in files))

    def test_google_iframe(self):
        _text, _pages, _files, iframes = build_snapshot_text("https://sosh46.ru/raspisanie/", SOSH_HTML)
        self.assertEqual(len(iframes), 1)
        csv_url = google_sheets_csv_url(iframes[0][0])
        self.assertIsNotNone(csv_url)
        self.assertIn("gid=656166446", csv_url)
        self.assertIn("output=csv", csv_url)

    def test_csv_lessons(self):
        lessons = parse_timetable_csv(CSV_SAMPLE, sheet="5-11 классы")
        eight = [les for les in lessons if les.class_name == "8А"]
        self.assertTrue(any(les.subject.startswith("История") and les.day == "ПОНЕДЕЛЬНИК" for les in eight))
        self.assertTrue(any(les.subject == "Алгебра" and les.day == "ЧЕТВЕРГ" for les in eight))

    def test_format_thursday_for_class(self):
        lessons = parse_timetable_csv(CSV_SAMPLE, sheet="5-11 классы")
        text = "\n".join(les.line() for les in lessons)
        thursday = datetime(2026, 8, 27, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="8А", now=thursday)
        self.assertIn("8А", msg)
        self.assertIn("Алгебра", msg)
        self.assertNotIn("Физкультура", msg)

    def test_evening_post_is_tomorrow(self):
        lessons = parse_timetable_csv(CSV_SAMPLE, sheet="5-11 классы")
        text = "\n".join(les.line() for les in lessons)
        evening = datetime(2026, 8, 27, 18, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="8А", now=evening, days_ahead=1)
        self.assertIn("пятниц", msg.lower())
        self.assertIn("28.08.2026", msg)

    def test_diff_describes_lesson_change(self):
        t1, _, f1, _ = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
        t2, _, f2, _ = build_snapshot_text("https://school.test/r", SCHEDULE_V2)
        old = ScheduleSnapshot(source_url="u", text=t1, files=[ScheduleFile(u, n) for u, n in f1])
        new = ScheduleSnapshot(source_url="u", text=t2, files=[ScheduleFile(u, n) for u, n in f2])
        old.fingerprint = fingerprint_of(old)
        new.fingerprint = fingerprint_of(new)
        report = describe_changes(old, new)
        self.assertIsNotNone(report)
        self.assertIn("Литература", report)
        self.assertIn("Математика", report)

    def test_no_diff_when_same(self):
        t1, _, f1, _ = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
        snap = ScheduleSnapshot(source_url="u", text=t1, files=[ScheduleFile(u, n) for u, n in f1])
        snap.fingerprint = fingerprint_of(snap)
        self.assertIsNone(describe_changes(snap, snap))

    def test_class_filter_keeps_class_rows(self):
        text, _, _, _ = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
        filtered = filter_lines_for_class(text, "8А")
        self.assertTrue(any("8А" in line or "8а" in line.lower() for line in filtered.splitlines()))

    def test_file_hash_change(self):
        old = ScheduleSnapshot(
            source_url="u",
            text="same",
            files=[ScheduleFile("https://s/r.pdf", "Расписание", "aaa")],
        )
        new = ScheduleSnapshot(
            source_url="u",
            text="same",
            files=[ScheduleFile("https://s/r.pdf", "Расписание", "bbb")],
        )
        old.fingerprint = fingerprint_of(old)
        new.fingerprint = fingerprint_of(new)
        report = describe_changes(old, new)
        self.assertIsNotNone(report)
        self.assertIn("обновил", report.lower())


if __name__ == "__main__":
    unittest.main()
