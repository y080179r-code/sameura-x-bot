import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("STATE_PATH", tempfile.mktemp())
os.environ.setdefault("DRY_RUN", "true")

import bot  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


class BotTests(unittest.TestCase):
    def test_threshold_down(self):
        self.assertEqual(bot.crossed_threshold(10.1, 9.9), ("down", 10.0))

    def test_threshold_up(self):
        self.assertEqual(bot.crossed_threshold(9.9, 10.1), ("up", 10.0))

    def test_adaptive_cadence(self):
        cases = [
            (5.0, 1.0),
            (9.9, 1.0),
            (10.0, 2.0),
            (19.9, 2.0),
            (20.0, 3.0),
            (39.9, 3.0),
            (40.0, 6.0),
            (59.9, 6.0),
            (60.0, 12.0),
            (79.9, 12.0),
            (80.0, 24.0),
            (100.0, 24.0),
        ]
        for rate, expected in cases:
            with self.subTest(rate=rate):
                self.assertEqual(bot.cadence_hours(rate), expected)

    def test_high_rate_waits_24h(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T12:00:00+09:00", "rate": 85.0}
        prev = {"observed_at": "2026-09-05T11:00:00+09:00", "rate": 85.0}
        state = bot.default_state()
        state["last_posted_at"] = (now - timedelta(hours=23)).isoformat()
        state["last_posted_observed_at"] = "2026-09-04T13:00:00+09:00"
        state["last_posted_rate"] = 85.0
        d = bot.choose_decision(obs, state, prev, True, now)
        self.assertFalse(d.post)

        state["last_posted_at"] = (now - timedelta(hours=24, minutes=1)).isoformat()
        d = bot.choose_decision(obs, state, prev, True, now)
        self.assertTrue(d.post)
        self.assertIn("24時間", d.reason)

    def test_under_10_posts_hourly(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T12:00:00+09:00", "rate": 8.7}
        prev = {"observed_at": "2026-09-05T11:00:00+09:00", "rate": 8.7}
        state = bot.default_state()
        state["last_posted_observed_at"] = "2026-09-05T11:00:00+09:00"
        state["last_posted_rate"] = 8.7
        state["last_posted_at"] = (now - timedelta(minutes=59)).isoformat()
        self.assertFalse(bot.choose_decision(obs, state, prev, True, now).post)
        state["last_posted_at"] = (now - timedelta(hours=1, minutes=1)).isoformat()
        self.assertTrue(bot.choose_decision(obs, state, prev, True, now).post)

    def test_rapid_change_overrides_slow_cadence(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T12:00:00+09:00", "rate": 86.2}
        prev = {"observed_at": "2026-09-05T11:00:00+09:00", "rate": 86.1}
        state = bot.default_state()
        state["last_posted_observed_at"] = "2026-09-05T10:00:00+09:00"
        state["last_posted_rate"] = 85.0
        state["last_posted_at"] = (now - timedelta(hours=2)).isoformat()
        d = bot.choose_decision(obs, state, prev, True, now)
        self.assertTrue(d.post)
        self.assertEqual(d.kind, "rapid")

    def test_post_has_no_url(self):
        obs = {
            "observed_at": "2026-09-05T08:00:00+09:00",
            "rate": 8.7,
            "rainfall_mm_h": 0.0,
            "storage_thousand_m3": 12789.0,
            "inflow_m3_s": 4.8,
            "outflow_m3_s": 18.1,
            "source": "国土交通省 川の防災情報",
            "source_url": bot.RIVER_URL,
            "source_kind": "realtime",
        }
        prev = {"observed_at": "2026-09-05T07:00:00+09:00", "rate": 8.8}
        state = bot.default_state()
        state["history"] = [prev, {"observed_at": obs["observed_at"], "rate": obs["rate"]}]
        text = bot.build_post(obs, state, prev, bot.Decision(True, "test", "regular"))
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertIn("8.7%", text)
        self.assertLess(len(text), 280)

    def test_extract_river_rows(self):
        html = b"""
        <table>
          <tr><td>2026/09/05</td><td>08:00</td><td>0.0</td><td>12789</td><td>4.8</td><td>18.1</td><td>8.7</td></tr>
          <tr><td></td><td>09:00</td><td>1.2</td><td>12840</td><td>20.1</td><td>18.0</td><td>8.8</td></tr>
        </table>
        """
        rows = bot._extract_river_rows(html, datetime(2026, 9, 5, 9, 7, tzinfo=JST))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["rate"], 8.8)
        self.assertEqual(rows[-1]["observed_at"], "2026-09-05T09:00:00+09:00")


if __name__ == "__main__":
    unittest.main()

class TestMoodV4(unittest.TestCase):
    def test_happy_moods(self):
        self.assertEqual(bot.mood(0.2, 50), "🙂↗️")
        self.assertEqual(bot.mood(0.7, 50), "😊💧")
        self.assertEqual(bot.mood(1.2, 50), "😄🌊")
        self.assertEqual(bot.mood(2.2, 50), "🤩🎉")

    def test_low_storage_stays_serious(self):
        self.assertEqual(bot.mood(0.5, 9.0), "🙂💧")
        self.assertEqual(bot.mood(-1.2, 9.0), "😰🚨")

    def test_movement_comment(self):
        self.assertEqual(bot.movement_comment(1.2, 30), "いい感じに増えてます😊")
        self.assertEqual(bot.movement_comment(-2.2, 30), "大きく減少しています🚨")
