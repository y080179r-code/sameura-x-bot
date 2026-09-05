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

    def test_high_rate_waits_24h_of_observation_time(self):
        now = datetime(2026, 9, 5, 12, 7, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T12:00:00+09:00", "rate": 85.0}
        prev = {"observed_at": "2026-09-05T11:00:00+09:00", "rate": 85.0}
        state = bot.default_state()
        state["last_posted_at"] = datetime(2026, 9, 5, 10, 0, tzinfo=JST).isoformat()
        state["last_posted_observed_at"] = "2026-09-04T13:00:00+09:00"  # 23h ago
        state["last_posted_rate"] = 85.0
        d = bot.choose_decision(obs, state, prev, True, now)
        self.assertFalse(d.post)

        state["last_posted_observed_at"] = "2026-09-04T12:00:00+09:00"  # 24h ago
        d = bot.choose_decision(obs, state, prev, True, now)
        self.assertTrue(d.post)
        self.assertIn("24時間", d.reason)

    def test_under_10_uses_observation_time_not_post_time(self):
        # The 11:00 observation was posted late at 11:55. When the 12:00 observation
        # appears at 12:07, it should still post because the DATA is one hour newer.
        now = datetime(2026, 9, 5, 12, 7, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T12:00:00+09:00", "rate": 8.7}
        prev = {"observed_at": "2026-09-05T11:00:00+09:00", "rate": 8.7}
        state = bot.default_state()
        state["last_posted_observed_at"] = "2026-09-05T11:00:00+09:00"
        state["last_posted_rate"] = 8.7
        state["last_posted_at"] = datetime(2026, 9, 5, 11, 55, tzinfo=JST).isoformat()

        # The 20-minute anti-burst guard still wins initially.
        self.assertFalse(bot.choose_decision(obs, state, prev, True, now).post)

        # At the next poll the SAME 12:00 observation is still eligible even though
        # update_history would report is_new=False. It is not lost.
        now2 = datetime(2026, 9, 5, 12, 37, tzinfo=JST)
        d = bot.choose_decision(obs, state, obs, False, now2)
        self.assertTrue(d.post)
        self.assertIn("1時間ごと", d.reason)

    def test_under_10_posts_next_hour_even_if_previous_post_was_late(self):
        now = datetime(2026, 9, 5, 19, 37, tzinfo=JST)
        obs = {"observed_at": "2026-09-05T19:00:00+09:00", "rate": 8.7}
        prev = {"observed_at": "2026-09-05T18:00:00+09:00", "rate": 8.8}
        state = bot.default_state()
        state["last_posted_observed_at"] = "2026-09-05T18:00:00+09:00"
        state["last_posted_rate"] = 8.8
        # 18:00 data happened to be posted at 19:05. Wall-clock cadence would skip
        # 19:00; observation-time cadence must post it at 19:37.
        state["last_posted_at"] = datetime(2026, 9, 5, 19, 5, tzinfo=JST).isoformat()
        d = bot.choose_decision(obs, state, prev, False, now)
        self.assertTrue(d.post)
        self.assertEqual(d.kind, "regular")

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


    def test_low_rate_display_has_siren_before_rate_and_water_drop(self):
        obs = {
            "observed_at": "2026-09-05T19:00:00+09:00",
            "rate": 8.8,
            "rainfall_mm_h": 0.0,
            "storage_thousand_m3": 29690.0,
            "inflow_m3_s": 11.5,
            "outflow_m3_s": 41.6,
            "source": "国土交通省 川の防災情報",
            "source_url": bot.RIVER_URL,
            "source_kind": "realtime",
        }
        prev = {"observed_at": "2026-09-05T18:00:00+09:00", "rate": 8.9}
        state = bot.default_state()
        state["history"] = [prev, {"observed_at": obs["observed_at"], "rate": obs["rate"]}]
        text = bot.build_post(obs, state, prev, bot.Decision(True, "test", "regular"))
        self.assertIn("🚨 8.8% 💧", text)
        self.assertIn("前回比 -0.1pt 🥲", text)
        self.assertIn("貯水量 29,690×10³m³", text)

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

class TestDroughtStatusV45(unittest.TestCase):
    def test_parse_home_banner_active(self):
        text = "早明浦ダムの貯水率低下に伴い、8月28日9時から第四次取水制限が実施されています。"
        status = bot.parse_drought_status_text(text)
        self.assertIsNotNone(status)
        self.assertTrue(status["restriction_active"])
        self.assertEqual(status["restriction_level"], "第四次")

    def test_parse_water_source_continuing_active(self):
        text = "令和8年9月1日 継続中 第四次取水制限（香川県60.0%）"
        status = bot.parse_drought_status_text(text)
        self.assertIsNotNone(status)
        self.assertTrue(status["restriction_active"])
        self.assertEqual(status["restriction_level"], "第四次")

    def test_historical_restriction_alone_is_not_current(self):
        text = "令和8年8月3日 第一次取水制限 令和8年8月10日 第二次取水制限"
        self.assertIsNone(bot.parse_drought_status_text(text))

    def test_drought_hashtag_only_when_official_restriction_active(self):
        base_obs = {
            "observed_at": "2026-09-05T20:00:00+09:00",
            "rate": 8.6,
            "rainfall_mm_h": 0.0,
            "storage_thousand_m3": 29440.0,
            "inflow_m3_s": 10.2,
            "outflow_m3_s": 44.0,
            "source": "国土交通省 川の防災情報",
            "source_url": bot.RIVER_URL,
            "source_kind": "realtime",
        }
        prev = {"observed_at": "2026-09-05T19:00:00+09:00", "rate": 8.7}
        state = bot.default_state()
        state["history"] = [prev, {"observed_at": base_obs["observed_at"], "rate": base_obs["rate"]}]

        active_obs = dict(base_obs, drought_restriction_active=True)
        text = bot.build_post(active_obs, state, prev, bot.Decision(True, "test", "regular"))
        self.assertIn("#早明浦ダム #吉野川 #渇水", text)

        normal_obs = dict(base_obs, drought_restriction_active=False)
        text = bot.build_post(normal_obs, state, prev, bot.Decision(True, "test", "regular"))
        self.assertIn("#早明浦ダム #吉野川", text)
        self.assertNotIn("#渇水", text)

    def test_regular_title_is_fixed_and_source_line_is_absent(self):
        obs = {
            "observed_at": "2026-09-05T20:00:00+09:00",
            "rate": 8.6,
            "rainfall_mm_h": 0.0,
            "storage_thousand_m3": 29440.0,
            "inflow_m3_s": 10.2,
            "outflow_m3_s": 44.0,
            "source": "国土交通省 川の防災情報",
            "source_url": bot.RIVER_URL,
            "source_kind": "realtime",
            "drought_restriction_active": True,
        }
        prev = {"observed_at": "2026-09-05T19:00:00+09:00", "rate": 8.7}
        state = bot.default_state()
        state["history"] = [prev, {"observed_at": obs["observed_at"], "rate": obs["rate"]}]
        text = bot.build_post(obs, state, prev, bot.Decision(True, "test", "regular"))
        self.assertTrue(text.startswith("💧 早明浦ダム 貯水率\n"))
        self.assertNotIn("出典：", text)
