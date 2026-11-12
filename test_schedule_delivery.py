import importlib
import json
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import schedule_watch as watch


class ScheduleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "123456:test-token"}):
            self.bot = importlib.import_module("bot")
        self.snapshot = watch.snapshot_from_payload(
            {"lessons": [{"className": "9Д", "day": "Среда", "subject": "Физика"}]},
            watch.DEFAULT_SCHOOL_URL,
            [],
        )

    async def test_morning_retries_failed_chat_and_fetches_missing_snapshot(self):
        chats = {
            "1": {"schedule": {"watch": True, "class_filter": "9Д"}},
            "2": {"schedule": {"watch": True, "class_filter": "9Д", "snapshot": self.snapshot.to_dict()}},
        }
        now = datetime(2026, 11, 11, 8, tzinfo=watch.SCHOOL_TZ)
        send = AsyncMock(side_effect=[RuntimeError("Telegram unavailable"), None, None])
        with (
            patch.object(self.bot, "data", chats),
            patch.object(self.bot, "save_data"),
            patch.object(self.bot, "school_now", return_value=now),
            patch.object(self.bot, "collect_schedule", AsyncMock(return_value=self.snapshot)),
            patch.object(self.bot, "send_plain", send),
        ):
            with self.assertLogs(self.bot.log, level="ERROR"):
                self.assertTrue(await self.bot.send_morning_if_needed())
            self.assertFalse(await self.bot.send_morning_if_needed())
        self.assertEqual([call.args[0] for call in send.await_args_list], [1, 2, 1])

    async def test_refresh_does_not_notify_for_another_class(self):
        new = watch.snapshot_from_payload(
            {
                "lessons": [{"className": "9Д", "day": "Среда", "subject": "Физика"}],
                "changes": [{"className": "9А", "day": "Среда", "subject": "Химия"}],
            },
            watch.DEFAULT_SCHOOL_URL,
            [],
        )
        chats = {"1": {"schedule": {
            "watch": True, "class_filter": "9Д", "snapshot": self.snapshot.to_dict(),
        }}}
        with (
            patch.object(self.bot, "data", chats),
            patch.object(self.bot, "save_data"),
            patch.object(self.bot, "collect_schedule", AsyncMock(return_value=new)),
            patch.object(self.bot, "ping_chat", AsyncMock()) as ping,
            patch.object(self.bot, "send_plain", AsyncMock()) as send,
        ):
            await self.bot.refresh_schedule(1, notify=True)
        ping.assert_not_awaited()
        send.assert_not_awaited()
        self.assertEqual(chats["1"]["schedule"]["snapshot"], new.to_dict())

    async def test_full_school_snapshot_keeps_late_lessons_and_changes(self):
        payload = {
            "lessons": [
                {"className": "1А", "day": "Среда", "number": n, "subject": "А" * 90}
                for n in range(1000)
            ] + [{"className": "9Д", "day": "Среда", "number": 1, "subject": "Физика"}],
            "changes": [{"className": "9Д", "day": "Среда", "number": 1, "subject": "Химия"}],
        }

        async def fetch(_session, url, _ssl):
            if url == watch.SOSH46_API_URL:
                return json.dumps(payload).encode(), url, "application/json"
            if url.endswith("gid=0"):
                csv = (
                    "СРЕДА,,,\n,,9Д\n"
                    "8.00,1,Длинное устаревшее название урока\n8.50,2,История\n"
                )
                return csv.encode(), url, "text/csv"
            return b"", url, "text/csv"

        with patch.object(watch, "_fetch", side_effect=fetch):
            snapshot = await watch.collect_sosh46(None, None)
        self.assertNotIn("устаревшее название", snapshot.text)
        self.assertIn("История", snapshot.text)
        for snap in (snapshot, watch.snapshot_from_payload(payload, watch.DEFAULT_SCHOOL_URL, [])):
            with self.subTest(length=len(snap.text)):
                msg = watch.format_day_schedule(snap.text, class_filter="9Д", week=True)
                self.assertIn("Утвержденное:", msg)
                self.assertIn("Физика", msg)
                self.assertIn("Измененное:", msg)
                self.assertIn("Химия", msg)
