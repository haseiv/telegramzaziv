import unittest
from datetime import datetime, timezone, timedelta

from schedule_watch import (
    ScheduleFile,
    ScheduleSnapshot,
    bells_to_text,
    build_snapshot_text,
    canonicalize_school_url,
    describe_changes,
    extract_bells,
    filter_lines_for_class,
    fingerprint_of,
    format_bells,
    format_changes_schedule,
    format_day_schedule,
    google_sheets_csv_url,
    infer_sheet_kind,
    last_due_parse_slot,
    lessons_to_text,
    next_clock_at,
    next_parse_at,
    parse_next_flight_payload,
    parse_schedule_api,
    parse_timetable_csv,
    parse_hours,
    split_class_filters,
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
ПЯТНИЦА,,,,,,,,
,,5А,5Б,8А
09.00-9.40,1,Русский,Математика,Геометрия
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
        msg = format_day_schedule(
            text, class_filter="8А", now=evening, days_ahead=1, week=False
        )
        self.assertIn("пятниц", msg.lower())
        self.assertIn("28.08.2026", msg)
        self.assertIn("Геометрия", msg)

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

    def test_class_filter_exact_10a_not_times(self):
        text = "\n".join([
            "5-11 классы | ЧЕТВЕРГ | 10А | 2. 8.50–9.30 Химия 314",
            "5-11 классы | ЧЕТВЕРГ | 1а | 2. 09.50-10.30 Русский язык",
            "1-4 классы | ЧЕТВЕРГ | 4д | 8. 14.30-15.10 Английский язык",
            "5-11 классы | ЧЕТВЕРГ | 7А | 7. 13.30-14.10 Алгебра",
        ])
        filtered = filter_lines_for_class(text, "10А")
        self.assertIn("10А", filtered)
        self.assertNotIn("1а", filtered)
        self.assertNotIn("4д", filtered)
        self.assertNotIn("7А", filtered)
        thursday = datetime(2026, 8, 27, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="10a", now=thursday)
        self.assertIn("Химия", msg)
        self.assertNotIn("Русский язык", msg)
        self.assertNotIn("Английский язык", msg)

    def test_week_is_standard_and_skips_empty_changes(self):
        lessons = parse_timetable_csv(CSV_SAMPLE, sheet="5-11 классы")
        text = "\n".join(les.line() for les in lessons)
        thursday = datetime(2026, 8, 27, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="8А", now=thursday)
        self.assertIn("ПОНЕДЕЛЬНИК", msg)
        self.assertIn("ЧЕТВЕРГ", msg)
        self.assertIn("Основное расписание", msg)
        self.assertIn("Замен на сайте сейчас нет", msg)
        self.assertIn("История", msg)
        self.assertIn("Алгебра", msg)

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

    def test_parse_hours_from_eight_every_two(self):
        self.assertEqual(parse_hours(), (8, 10, 12, 14, 16, 18, 20, 22))

    def test_slots_before_eight_wait_until_eight(self):
        tz = timezone(timedelta(hours=4))
        early = datetime(2026, 8, 27, 7, 30, tzinfo=tz)
        self.assertIsNone(last_due_parse_slot(early))
        nxt = next_parse_at(early)
        self.assertEqual(nxt.hour, 8)
        self.assertEqual(nxt.day, 27)

    def test_slots_midday_then_two_hours(self):
        tz = timezone(timedelta(hours=4))
        now = datetime(2026, 8, 27, 9, 15, tzinfo=tz)
        due = last_due_parse_slot(now)
        self.assertEqual(due.hour, 8)
        nxt = next_parse_at(now)
        self.assertEqual(nxt.hour, 10)

    def test_slots_after_last_go_to_tomorrow_eight(self):
        tz = timezone(timedelta(hours=4))
        late = datetime(2026, 8, 27, 23, 10, tzinfo=tz)
        due = last_due_parse_slot(late)
        self.assertEqual(due.hour, 22)
        nxt = next_parse_at(late)
        self.assertEqual(nxt.day, 28)
        self.assertEqual(nxt.hour, 8)


    def test_split_classes(self):
        self.assertEqual(split_class_filters("10А, 10Б"), ["10А", "10Б"])
        self.assertEqual(split_class_filters("класс 8А и 8В"), ["8А", "8В"])
        self.assertEqual(split_class_filters("9д бля"), ["9д"])
        self.assertEqual(split_class_filters("9Д"), ["9Д"])

    def test_extract_bells(self):
        html = """
        <html><body>
        <h1>Расписание звонков</h1>
        <p>Понедельник</p>
        <p>1 урок — 9.00 – 9.40</p>
        <p>2 урок — 9.50 – 10.30</p>
        <p>Вторник-суббота</p>
        <p>1 урок — 8.00 – 8.40</p>
        <p>Расписание</p>
        </body></html>
        """
        bells = extract_bells(html)
        self.assertIn("Понедельник", bells)
        self.assertIn("9.00", bells)
        self.assertIn("Вторник-суббота", bells)

    def test_next_clock_rolls_tomorrow(self):
        tz = timezone(timedelta(hours=4))
        now = datetime(2026, 8, 27, 8, 0, tzinfo=tz)
        nxt = next_clock_at(7, 30, now)
        self.assertEqual(nxt.day, 28)
        self.assertEqual(nxt.hour, 7)
        self.assertEqual(nxt.minute, 30)

    def test_canonicalize_old_wordpress_urls(self):
        self.assertEqual(
            canonicalize_school_url("https://sosh46.ru/raspisanie/"),
            "https://sosh46.ru/schedule",
        )
        self.assertEqual(
            canonicalize_school_url("https://sosh46.ru/raspisanie-zvonkov/"),
            "https://sosh46.ru/schedule",
        )

    def test_parse_new_site_api(self):
        payload = {
            "lessons": [
                {
                    "className": "10а",
                    "day": "Среда",
                    "time": "8.00 – 8.40",
                    "number": 1,
                    "subject": "Информатика",
                    "teacher": "Тимершаехова А. Р.",
                    "room": "303",
                }
            ],
            "changes": [
                {
                    "id": "change-1",
                    "className": "10а",
                    "day": "Вторник",
                    "time": "9.50 – 10.30",
                    "number": 3,
                    "subject": "Классные часы",
                    "teacher": "",
                    "room": "",
                    "note": "Изменение в расписании",
                }
            ],
            "bells": [
                {
                    "dayGroup": "regular",
                    "shift": 1,
                    "lesson": 1,
                    "start": "8.00",
                    "end": "8.40",
                    "break": "10 мин",
                }
            ],
        }
        lessons, bells = parse_schedule_api(payload)
        text = lessons_to_text(lessons)
        self.assertIn("Информатика", text)
        self.assertIn("303", text)
        self.assertIn("Классные часы", text)
        wednesday = datetime(2026, 9, 2, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="10А", now=wednesday, week=False)
        self.assertIn("Информатика", msg)
        self.assertNotIn("Классные часы", msg)
        week = format_day_schedule(text, class_filter="10А", now=wednesday, week=True)
        self.assertIn("Классные часы", week)
        self.assertIn("Изменения", week)
        self.assertIn("1 урок — 8.00 – 8.40", bells_to_text(payload["bells"]))
        self.assertIn("8.00", format_bells(bells, now=wednesday))

    def test_site_redesign_is_short_notice_not_huge_diff(self):
        old = ScheduleSnapshot(
            source_url="https://sosh46.ru/raspisanie/",
            pages=["https://sosh46.ru/raspisanie/"],
            text="старая таблица google",
        )
        new = ScheduleSnapshot(
            source_url="https://sosh46.ru/schedule",
            pages=["https://sosh46.ru/schedule", "https://sosh46.ru/api/schedule"],
            text="Расписание | СРЕДА | 10а | 1. 8.00 Информатика",
        )
        old.fingerprint = fingerprint_of(old)
        new.fingerprint = fingerprint_of(new)
        report = describe_changes(old, new)
        self.assertIsNotNone(report)
        self.assertIn("обновился", report.lower())
        self.assertNotIn("старая таблица", report)

    def test_empty_today_hints_other_days(self):
        text = "Расписание | СРЕДА | 10а | 1. 8.00 Информатика"
        tuesday = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="10А", now=tuesday, week=False)
        self.assertIn("пусто", msg.lower())
        self.assertIn("СРЕДА", msg)
        self.assertIn("неделя", msg)

    def test_changes_from_snapshot_not_last_diff(self):
        payload = {
            "lessons": [],
            "changes": [
                {
                    "id": "change-1",
                    "className": "5а",
                    "day": "Вторник",
                    "time": "9.50 – 10.30",
                    "number": 3,
                    "subject": "Классные часы",
                    "teacher": "",
                    "room": "",
                    "note": "Изменение в расписании",
                }
            ],
            "bells": [],
        }
        lessons, _bells = parse_schedule_api(payload)
        text = lessons_to_text(lessons)
        msg = format_changes_schedule(text, "5А")
        self.assertIn("Классные часы", msg)
        empty = format_changes_schedule(text, "10А")
        self.assertIn("нет", empty.lower())

    def test_parse_next_flight_chunks(self):
        html = (
            '<script>self.__next_f.push([1,"14:{\\"className\\":\\"10а\\",'
            '\\"day\\":\\"Среда\\",\\"time\\":\\"8.00\\",\\"number\\":1,'
            '\\"subject\\":\\"Информатика\\",\\"teacher\\":\\"T\\",\\"room\\":\\"303\\"}\\n"])</script>'
            '<script>self.__next_f.push([1,"101:{\\"id\\":\\"change-1\\",\\"className\\":\\"5а\\",'
            '\\"day\\":\\"Вторник\\",\\"time\\":\\"9.50\\",\\"number\\":3,'
            '\\"subject\\":\\"Классные часы\\",\\"teacher\\":\\"\\",\\"room\\":\\"\\",'
            '\\"note\\":\\"Изменение в расписании\\"}\\n"])</script>'
        )
        payload = parse_next_flight_payload(html)
        self.assertEqual(payload["lessons"][0]["subject"], "Информатика")
        self.assertEqual(payload["changes"][0]["subject"], "Классные часы")

    def test_sheet_tab_with_one_lesson_is_changes(self):
        csv_text = """ВТОРНИК,,,,,,,,
,,8А,8Б
9.50 – 10.30,3,Классные часы,Классные часы
"""
        lessons = parse_timetable_csv(csv_text, sheet="5-11")
        self.assertEqual(infer_sheet_kind(lessons), "Изменения")

    def test_sheet_tab_with_full_day_is_schedule(self):
        csv_text = """СРЕДА,,,,,,,,
,,8А
8.00 – 8.40,1,Алгебра
8.50 – 9.30,2,История
9.50 – 10.30,3,Физика
10.50 – 11.30,4,Русский
11.50 – 12.30,5,Химия
"""
        lessons = parse_timetable_csv(csv_text, sheet="Изменения")
        self.assertEqual(infer_sheet_kind(lessons), "Расписание")

    def test_missing_8d_lists_other_eighth_grades(self):
        text = "\n".join([
            "Расписание | СРЕДА | 8А | 1. 8.00 Алгебра",
            "Расписание | СРЕДА | 8Б | 1. 8.00 История",
            "Расписание | СРЕДА | 8В | 1. 8.00 Физика",
            "Расписание | СРЕДА | 8Г | 1. 8.00 Химия",
        ])
        wednesday = datetime(2026, 9, 2, 12, 0, tzinfo=timezone(timedelta(hours=4)))
        msg = format_day_schedule(text, class_filter="8Д", now=wednesday, week=True)
        self.assertIn("8Д", msg)
        self.assertIn("8А", msg)
        self.assertIn("параллели", msg)


if __name__ == "__main__":
    unittest.main()
