import json
import tempfile
import unittest
from pathlib import Path

from qq_guard.config import GuardConfig
from qq_guard.tencent_monitor import TencentChannelMonitor


class FakeTencentApi:
    def __init__(self, feeds, details):
        self.feeds = feeds
        self.details = details
        self.deletions = []
        self.list_calls = []

    def list_channel_feeds(self, guild_id, channel_id, count=20):
        self.list_calls.append((guild_id, channel_id))
        return list(self.feeds.get((guild_id, channel_id), self.feeds.get(channel_id, [])))

    def get_feed_detail(self, guild_id, channel_id, feed_id):
        key = (guild_id, feed_id)
        if key in self.details:
            return dict(self.details[key])
        if feed_id in self.details:
            return dict(self.details[feed_id])
        for values in self.feeds.values():
            for feed in values:
                if feed.get("feed_id") == feed_id:
                    return {
                        "title": feed.get("title", ""),
                        "content": feed.get("content_snippet", ""),
                        "feed_type": 1,
                        "topic_names": [],
                        "images": [],
                    }
        raise KeyError(feed_id)

    def delete_feed(self, guild_id, channel_id, feed_id, create_time, live):
        self.deletions.append((feed_id, live))
        return {"success": True}


class TencentMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def config(self, delete_mode="dry_run", auto_delete_duplicates=False):
        self.config_path.write_text(
            json.dumps(
                {
                    "database_path": "audit.sqlite3",
                    "delete_mode": delete_mode,
                    "auto_delete_duplicates": auto_delete_duplicates,
                    "tencent_channel": {
                        "enabled": True,
                        "guild_id": "100",
                        "channels": {
                            "qa_discussion": "200",
                            "weekly_question": "201"
                        },
                        "scan_count": 20
                    }
                }
            ),
            encoding="utf-8",
        )
        return GuardConfig.from_file(str(self.config_path))

    def multi_config(self):
        self.config_path.write_text(
            json.dumps(
                {
                    "database_path": "audit.sqlite3",
                    "delete_mode": "dry_run",
                    "auto_delete_duplicates": False,
                    "section_hashtags": {
                        "qa_discussion": ["问答"],
                        "practical_article": ["实用文章"],
                    },
                    "tencent_channels": [
                        {
                            "name": "WorkBuddy",
                            "guild_id": "100",
                            "channels": {"qa_discussion": "200"},
                        },
                        {
                            "name": "腾讯云架构师峰会",
                            "guild_id": "300",
                            "auto_classify_channels": {"文章": "301"},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return GuardConfig.from_file(str(self.config_path))

    @staticmethod
    def feed(feed_id, author, title, body, created):
        return {
            "feed_id": feed_id,
            "author_id": author,
            "title": title,
            "content_snippet": body,
            "create_time_raw": created,
        }

    @staticmethod
    def detail(title, body, topics=None):
        return {
            "title": title,
            "content": body,
            "feed_type": 1,
            "topic_names": topics or [],
            "images": [],
        }

    def test_consecutive_exact_duplicate_calls_delete_for_newer(self):
        feeds = {
            "200": [
                self.feed("B_new", "u1", "问题", "同一内容", 20),
                self.feed("B_old", "u1", "问题", "同一内容", 10),
            ],
            "201": [],
        }
        details = {
            "B_new": self.detail("问题", "同一内容"),
            "B_old": self.detail("问题", "同一内容"),
        }
        api = FakeTencentApi(feeds, details)
        report = TencentChannelMonitor(self.config("live", auto_delete_duplicates=True), api).scan_once()
        self.assertEqual(len(report.duplicate_findings), 1)
        self.assertEqual(api.deletions, [("B_new", True)])

    def test_test_mode_detects_duplicate_without_calling_delete(self):
        feeds = {
            "200": [
                self.feed("B_new", "u1", "问题", "同一内容", 20),
                self.feed("B_old", "u1", "问题", "同一内容", 10),
            ],
            "201": [],
        }
        details = {
            "B_new": self.detail("问题", "同一内容"),
            "B_old": self.detail("问题", "同一内容"),
        }
        api = FakeTencentApi(feeds, details)
        report = TencentChannelMonitor(self.config(), api).scan_once()
        self.assertEqual(report.duplicate_findings[0].delete_status, "detected_only")
        self.assertEqual(api.deletions, [])

    def test_intervening_post_breaks_consecutive_match(self):
        feeds = {
            "200": [
                self.feed("B_3", "u1", "A", "相同", 30),
                self.feed("B_2", "u2", "B", "其他", 20),
                self.feed("B_1", "u1", "A", "相同", 10),
            ],
            "201": [],
        }
        details = {}
        api = FakeTencentApi(feeds, details)
        report = TencentChannelMonitor(self.config(), api).scan_once()
        self.assertEqual(len(report.duplicate_findings), 0)
        self.assertEqual(api.deletions, [])

    def test_same_content_different_author_is_not_duplicate(self):
        feeds = {
            "200": [
                self.feed("B_2", "u2", "A", "相同", 20),
                self.feed("B_1", "u1", "A", "相同", 10),
            ],
            "201": [],
        }
        api = FakeTencentApi(feeds, {})
        report = TencentChannelMonitor(self.config(), api).scan_once()
        self.assertEqual(len(report.duplicate_findings), 0)

    def test_weekly_missing_topic_is_reported(self):
        feeds = {
            "200": [],
            "201": [self.feed("B_weekly", "u1", "每周一问", "问题", 10)],
        }
        api = FakeTencentApi(feeds, {"B_weekly": self.detail("每周一问", "问题", topics=[])})
        report = TencentChannelMonitor(self.config(), api).scan_once()
        self.assertEqual(report.weekly_missing_topic, 1)

    def test_two_guilds_are_scanned_and_auto_channel_deduplicates_by_class(self):
        feeds = {
            ("100", "200"): [],
            ("300", "301"): [
                self.feed("Q_new", "u1", "怎么设计", "同一问题", 30),
                self.feed("P_mid", "u2", "架构案例", "实用文章", 20),
                self.feed("Q_old", "u1", "怎么设计", "同一问题", 10),
            ],
        }
        details = {
            "Q_new": self.detail("怎么设计", "同一问题", topics=["问答"]),
            "P_mid": self.detail("架构案例", "实用文章", topics=["实用文章"]),
            "Q_old": self.detail("怎么设计", "同一问题", topics=["问答"]),
        }
        api = FakeTencentApi(feeds, details)
        report = TencentChannelMonitor(self.multi_config(), api).scan_once()

        self.assertEqual(report.guilds, ("WorkBuddy", "腾讯云架构师峰会"))
        self.assertEqual(report.scanned_feeds, 3)
        self.assertEqual(
            report.classification_counts["腾讯云架构师峰会:qa_discussion"], 2
        )
        self.assertEqual(
            report.classification_counts["腾讯云架构师峰会:practical_article"], 1
        )
        self.assertEqual(len(report.duplicate_findings), 1)
        self.assertEqual(report.duplicate_findings[0].guild_id, "300")
        self.assertEqual(report.duplicate_findings[0].section, "qa_discussion")
        self.assertEqual(report.duplicate_findings[0].delete_status, "detected_only")
        self.assertEqual(api.deletions, [])

    def test_sensitive_term_is_reported_without_delete(self):
        feeds = {
            "200": [self.feed("B_sensitive", "u1", "sb", "sb", 10)],
            "201": [],
        }
        details = {"B_sensitive": self.detail("sb", "sb")}
        api = FakeTencentApi(feeds, details)
        report = TencentChannelMonitor(self.config(), api).scan_once()
        self.assertEqual(len(report.moderation_findings), 1)
        finding = report.moderation_findings[0]
        self.assertIn("sensitive_term_en", [reason.code for reason in finding.reasons])
        self.assertIn(finding.action, {"review", "delete_candidate"})
        self.assertEqual(api.deletions, [])


if __name__ == "__main__":
    unittest.main()
