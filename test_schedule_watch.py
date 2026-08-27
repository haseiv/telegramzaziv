import unittest

from schedule_watch import (
    ScheduleFile,
    ScheduleSnapshot,
    build_snapshot_text,
    describe_changes,
    filter_lines_for_class,
    fingerprint_of,
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


class ScheduleParseTests(unittest.TestCase):
    def test_finds_schedule_links_on_home(self):
        text, pages, files = build_snapshot_text("https://school.test/", HOME_HTML)
        page_urls = [u for u, _ in pages]
        file_urls = [u for u, _ in files]
        self.assertIn("https://school.test/raspisanie", page_urls)
        self.assertIn("https://school.test/files/zvonki.pdf", file_urls)
        self.assertTrue(any("Расписание" in line or "расписан" in line.lower() for line in text.splitlines()) or pages)

    def test_tables_extracted(self):
        text, _pages, files = build_snapshot_text("https://school.test/raspisanie", SCHEDULE_V1)
        self.assertIn("Математика", text)
        self.assertIn("8А", text)
        self.assertTrue(any(u.endswith("rasp.pdf") for u, _ in files))

    def test_diff_describes_lesson_change(self):
        t1, _, f1 = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
        t2, _, f2 = build_snapshot_text("https://school.test/r", SCHEDULE_V2)
        old = ScheduleSnapshot(source_url="u", text=t1, files=[ScheduleFile(u, n) for u, n in f1])
        new = ScheduleSnapshot(source_url="u", text=t2, files=[ScheduleFile(u, n) for u, n in f2])
        old.fingerprint = fingerprint_of(old)
        new.fingerprint = fingerprint_of(new)
        report = describe_changes(old, new)
        self.assertIsNotNone(report)
        self.assertIn("Литература", report)
        self.assertIn("Математика", report)
        self.assertIn("Изменения на четверг", report)

    def test_no_diff_when_same(self):
        t1, _, f1 = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
        snap = ScheduleSnapshot(source_url="u", text=t1, files=[ScheduleFile(u, n) for u, n in f1])
        snap.fingerprint = fingerprint_of(snap)
        self.assertIsNone(describe_changes(snap, snap))

    def test_class_filter_keeps_class_rows(self):
        text, _, _ = build_snapshot_text("https://school.test/r", SCHEDULE_V1)
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
