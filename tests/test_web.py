import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from qq_guard.admin_store import AdminStore
from qq_guard.config import GuardConfig
from qq_guard.placement import move_targets
from qq_guard.scan_control import ScanLock
from qq_guard.tencent_cli import TencentCliError
from qq_guard.web import _cn_time, _plain_ai_text, create_app


TEST_PASSWORD = "test-only-password-9!"


class FakeTencentClient:
    def __init__(self) -> None:
        self.deleted = []
        self.moved = []
        self.edited = []
        self.capability_calls = []
        self.delete_error = None
        self.detail_error = None

    def capability_index(self):
        return [
            {
                "domain": "feed",
                "commands": [
                    {
                        "use": "get-feed-detail",
                        "short": "查看帖子详情",
                        "group": "read",
                        "risk": "read",
                    },
                    {
                        "use": "del-feed",
                        "short": "删除帖子",
                        "group": "write",
                        "risk": "high-risk-write",
                    },
                    {
                        "use": "move-feed",
                        "short": "移动帖子到其他版块",
                        "group": "write",
                        "risk": "write",
                    },
                ],
            }
        ]

    def capability_schema(self, domain, action):
        if action == "move-feed":
            flags = [
                {"name": "guild-id", "type": "string", "required": True, "description": "频道 ID"},
                {"name": "channel-id", "type": "string", "required": True, "description": "目标栏目 ID"},
                {"name": "feed-id", "type": "string", "required": True, "description": "帖子 ID"},
            ]
        else:
            flags = [
                {"name": "feed-id", "type": "string", "required": True, "description": "帖子 ID"}
            ]
        return {
            "command": f"{domain}.{action}",
            "description": "测试官方能力",
            "group": "read" if action == "get-feed-detail" else "write",
            "risk": "read" if action == "get-feed-detail" else "high-risk-write",
            "flags": flags,
        }

    def execute_capability(self, domain, action, parameters, confirmed=False, dry_run=False):
        self.capability_calls.append((domain, action, parameters, confirmed, dry_run))
        return {"success": True, "data": {"title": "官方返回"}}

    def version(self):
        return "test-version"

    def login_status(self):
        return {"success": True, "data": {"valid": True, "message": "已登录"}}

    def delete_feed(self, guild_id, channel_id, feed_id, create_time, live):
        self.deleted.append((guild_id, channel_id, feed_id, create_time, live))
        if self.delete_error is not None:
            raise self.delete_error
        return {"success": True}

    def get_feed_detail(self, guild_id, channel_id, feed_id):
        if self.detail_error is not None:
            raise self.detail_error
        return {"create_time_raw": "123456"}

    def move_feed(self, guild_id, original_channel_id, target_channel_id, feed_id):
        self.moved.append((guild_id, original_channel_id, target_channel_id, feed_id))
        return {"success": True}

    def alter_feed(
        self, guild_id, channel_id, feed_id, create_time, feed_type, title, content, markdown=False
    ):
        self.edited.append(
            (guild_id, channel_id, feed_id, create_time, feed_type, title, content, markdown)
        )
        return {"success": True}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config.json"
        self.database_path = root / "guard.sqlite3"
        self.config_path.write_text(
            json.dumps(
                {
                    "database_path": str(self.database_path),
                    "delete_mode": "dry_run",
                    "auto_delete_duplicates": False,
                    "auto_delete_policy_violations": False,
                    "channel_sections": {},
                    "official_author_ids": [],
                    "section_hashtags": {
                        "featured": ["精华"],
                        "weekly_question": ["每周一问"],
                        "practical_article": ["实用文章"],
                        "qa_discussion": ["问答"],
                        "official_news": ["官方资讯"],
                    },
                    "rules": {
                        "weekly_phrase_keywords": ["每周一问"],
                        "question_keywords": ["请问", "如何"],
                        "practical_keywords": ["案例", "步骤"],
                        "min_practical_text_length": 100,
                        "min_featured_text_length": 220,
                        "weekly_requires_any_hashtag": True,
                    },
                    "board_policies": {},
                    "moderation": {
                        "enabled": True,
                        "policy_version": "2026-08-19.1",
                        "review_threshold": 25,
                        "delete_candidate_threshold": 80,
                        "min_meaningful_length": 4,
                        "detect_contact_information": True,
                        "detect_external_links": True,
                        "detect_obfuscated_terms": True,
                        "terms": [
                            {
                                "term": "casino",
                                "language": "en",
                                "category": "prohibited",
                                "severity": "high",
                                "action": "delete_candidate",
                                "match_type": "word",
                            }
                        ],
                    },
                    "tencent_channels": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.fake_cli = FakeTencentClient()
        environment = {
            "QQ_GUARD_SECRET_KEY": "test-secret-key-that-is-long-enough",
            "QQ_GUARD_ADMIN_PASSWORD_HASH": generate_password_hash(
                TEST_PASSWORD, method="pbkdf2:sha256:600000"
            ),
            "QQ_GUARD_MANUAL_DELETE_ENABLED": "true",
        }
        self.env_patch = patch.dict(os.environ, environment, clear=False)
        self.env_patch.start()
        self.app = create_app(str(self.config_path), cli_factory=lambda: self.fake_cli)
        self.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_plain_ai_text_removes_markdown_symbols(self):
        self.assertEqual(
            _plain_ai_text("**发现风险**\n- 图片包含外部联系方式\n2. 建议人工核对"),
            "发现风险\n图片包含外部联系方式\n建议人工核对",
        )

    @staticmethod
    def csrf(response) -> str:
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError("页面中没有 CSRF token")
        return match.group(1).decode("utf-8")

    def login(self):
        response = self.client.get("/login")
        token = self.csrf(response)
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "password": TEST_PASSWORD},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("今天要处理的内容".encode("utf-8"), response.data)
        return response

    def insert_tencent_review(
        self, feed_id="feed-1", title="违规测试", policy_version="2026-08-19.1"
    ) -> int:
        AdminStore(self.database_path)
        with sqlite3.connect(str(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO tencent_moderation_findings
                (guild_id, guild_name, channel_id, feed_id, title, section, action,
                 risk_level, risk_score, policy_version, reasons_json, review_status,
                 author_id, body, media_urls_json, source_created_at, classification_json,
                 delete_status, created_at)
                VALUES ('1', '测试频道', '2', ?, ?, 'qa_discussion',
                        'delete_candidate', 'high', 80, ?, ?, 'pending',
                        'author', 'casino', '[]', '123456', '{}', 'not_requested',
                        '2026-08-19T00:00:00+00:00')
                """,
                (
                    feed_id,
                    title,
                    policy_version,
                    json.dumps([{"code": "sensitive_term_en", "message": "命中英文敏感词", "score": 80}]),
                ),
            )
            return int(cursor.lastrowid)

    def insert_cached_content(self, feed_id="feed-ok", title="正常帖子"):
        AdminStore(self.database_path)
        detail = {
            "feed_id": feed_id,
            "guild_id": "1",
            "guild_name": "测试频道",
            "channel_id": "2",
            "channel_name": "问答与交流",
            "title": title,
            "content": "这是一条没有发现问题的完整正文",
            "author": "测试用户",
            "author_id": "author-1",
            "feed_type": 1,
            "create_time": "2026-08-21 10:00:00",
            "create_time_raw": 1787277600,
            "share_url": "https://pd.qq.com/s/test",
        }
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_feed_cache
                (guild_id, channel_id, feed_id, version_key, detail_json, fetched_at,
                 guild_name, channel_name, first_seen_at, last_seen_at)
                VALUES ('1', '2', ?, 'v1', ?, '2026-08-21T02:00:00+00:00',
                        '测试频道', '问答与交流', '2026-08-21T02:00:00+00:00',
                        '2026-08-21T02:00:00+00:00')
                """,
                (feed_id, json.dumps(detail, ensure_ascii=False)),
            )

    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_rejects_wrong_password(self):
        response = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={"csrf_token": self.csrf(response), "password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("密码不正确".encode("utf-8"), response.data)

    def test_login_and_security_headers(self):
        response = self.login()
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("AI 巡检只在你点击后进行".encode("utf-8"), response.data)
        self.assertIn("今天要处理的内容".encode("utf-8"), response.data)
        self.assertIn("全部内容".encode("utf-8"), response.data)
        self.assertIn("内容审核".encode("utf-8"), response.data)

    def test_dashboard_exposes_task_queues_as_actionable_content_cards(self):
        self.insert_tencent_review()
        response = self.login()
        self.assertIn("需要删帖".encode("utf-8"), response.data)
        self.assertIn("调整栏目".encode("utf-8"), response.data)
        self.assertIn("需要核对".encode("utf-8"), response.data)
        self.assertIn("可以保留".encode("utf-8"), response.data)
        self.assertNotIn("先处理这些".encode("utf-8"), response.data)
        self.assertIn("AI分析出了什么".encode("utf-8"), response.data)
        self.assertIn("建议下一步".encode("utf-8"), response.data)
        self.assertIn("查看并处理".encode("utf-8"), response.data)
        self.assertIn(b"data-review-card", response.data)
        self.assertNotIn(b'class="queue-header"', response.data)
        self.assertNotIn(b'class="review-drawer"', response.data)
        self.assertNotIn(b"built-in method clear", response.data)
        self.assertNotIn("查看判断依据".encode("utf-8"), response.data)
        self.assertNotIn("两项判断不一致".encode("utf-8"), response.data)

    def test_conflicting_ai_result_is_described_as_manual_check(self):
        row_id = self.insert_tencent_review()
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET action = 'allow', risk_level = 'high' WHERE id = ?",
                (row_id,),
            )
        response = self.login()
        self.assertIn("人工核对".encode("utf-8"), response.data)
        self.assertIn("AI发现了需要确认的风险信号".encode("utf-8"), response.data)
        self.assertNotIn("风险提示与处理建议不同".encode("utf-8"), response.data)

    def test_dashboard_review_card_includes_original_post_link(self):
        self.insert_cached_content(feed_id="feed-original")
        self.insert_tencent_review(feed_id="feed-original")
        response = self.login()
        self.assertIn("打开原帖".encode("utf-8"), response.data)
        self.assertIn(
            b'href="https://pd.qq.com/s/test" target="_blank"',
            response.data,
        )

    def test_dashboard_distinguishes_real_channel_from_detected_content_type(self):
        row_id = self.insert_tencent_review("feed-placement", "应该放到实用文章")
        self.insert_cached_content("feed-placement", "应该放到实用文章")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy",
            "guild_id": "1",
            "channels": {"qa_discussion": "2", "practical_article": "3"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "2": {"name": "问答与交流", "expected_sections": ["qa_discussion"]},
            "3": {"name": "实用文章", "expected_sections": ["practical_article"]},
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET section = 'practical_article', action = 'review', risk_level = 'medium',
                    risk_score = 35, classification_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps({
                        "section": "practical_article",
                        "confidence": 0.9,
                        "reasons": ["这是一篇完整教程"],
                        "validation_issues": [],
                    }, ensure_ascii=False),
                    row_id,
                ),
            )
            weekly_detail = {
                "guild_id": "1",
                "channel_id": "4",
                "channel_name": "每周一问",
                "title": "本周问题",
            }
            connection.execute(
                """
                INSERT INTO tencent_feed_cache
                (guild_id, channel_id, feed_id, version_key, detail_json, fetched_at,
                 guild_name, channel_name, first_seen_at, last_seen_at)
                VALUES ('1', '4', 'weekly-feed', 'v1', ?, '2026-08-21T02:00:00+00:00',
                        'WorkBuddy', '每周一问', '2026-08-21T02:00:00+00:00',
                        '2026-08-21T02:00:00+00:00')
                """,
                (json.dumps(weekly_detail, ensure_ascii=False),),
            )

        self.login()
        response = self.client.get("/?task=placement")

        self.assertIn("当前栏目：问答与交流".encode("utf-8"), response.data)
        self.assertIn("内容类型：实用文章".encode("utf-8"), response.data)
        self.assertIn("调整到“实用文章”".encode("utf-8"), response.data)
        self.assertIn(b'<option value="1:4"', response.data)
        self.assertIn("每周一问".encode("utf-8"), response.data)
        self.assertNotIn(b'<option value="1:2">', response.data)

    def test_synced_current_channel_prevents_stale_same_channel_suggestion(self):
        row_id = self.insert_tencent_review("feed-moved", "已经在实用文章")
        self.insert_cached_content("feed-moved", "已经在实用文章")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy",
            "guild_id": "1",
            "channels": {"qa_discussion": "2", "practical_article": "3"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "2": {"name": "问答与交流", "expected_sections": ["qa_discussion"]},
            "3": {"name": "实用文章", "expected_sections": ["practical_article"]},
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE tencent_feed_cache
                SET channel_id = '3', channel_name = '实用文章'
                WHERE feed_id = 'feed-moved'
                """
            )
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET section = 'practical_article', action = 'review', risk_level = 'medium',
                    risk_score = 35, classification_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps({
                        "section": "practical_article",
                        "confidence": 0.9,
                        "reasons": ["实用文章"],
                        "validation_issues": [],
                    }, ensure_ascii=False),
                    row_id,
                ),
            )

        self.login()
        response = self.client.get("/?task=placement")

        self.assertNotIn("已经在实用文章".encode("utf-8"), response.data)
        item = AdminStore(self.database_path).get_review("tencent", row_id)
        self.assertEqual(item["channel_id"], "3")
        self.assertEqual(item["current_channel_name"], "实用文章")

    def test_move_rejects_current_physical_channel(self):
        self.insert_cached_content()
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "测试频道",
            "guild_id": "1",
            "channels": {"qa_discussion": "2"},
            "poll_interval_seconds": 300,
        }]
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        response = self.login()

        response = self.client.post(
            "/contents/1/feed-ok/move",
            data={"csrf_token": self.csrf(response), "move_target": "1:2"},
            follow_redirects=True,
        )

        self.assertIn("帖子已经在这个栏目".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.moved, [])

    def test_auto_classified_physical_channel_appears_only_once(self):
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "峰会频道",
            "guild_id": "1",
            "channels": {"qa_discussion": "2"},
            "auto_classify_channels": {"文章": "9"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "9": {
                "name": "文章",
                "expected_sections": [
                    "practical_article", "featured", "official_news", "weekly_question"
                ],
            }
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        targets = move_targets(GuardConfig.from_file(str(self.config_path)))
        article_targets = [target for target in targets if target["channel_id"] == "9"]

        self.assertEqual(len(article_targets), 1)
        self.assertEqual(article_targets[0]["label"], "文章")
        self.assertEqual(article_targets[0]["key"], "1:9")

    def test_quality_scores_without_high_risk_evidence_do_not_suggest_delete(self):
        row_id = self.insert_tencent_review(title="哈哈哈哈哈")
        reasons = [
            {
                "code": "low_information_content",
                "category": "quality",
                "severity": "medium",
                "message": "正文有效信息量低于当前栏目最低要求",
                "score": 25,
                "auto_delete_eligible": False,
            },
            {
                "code": "repeated_characters",
                "category": "quality",
                "severity": "medium",
                "message": "内容主要由重复字符组成，疑似灌水或测试内容",
                "score": 25,
                "auto_delete_eligible": False,
            },
        ]
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET reasons_json = ? WHERE id = ?",
                (json.dumps(reasons, ensure_ascii=False), row_id),
            )

        response = self.login()

        self.assertIn("人工核对".encode("utf-8"), response.data)
        self.assertIn("疑似灌水或测试内容".encode("utf-8"), response.data)
        self.assertNotIn("查看证据后决定是否删除".encode("utf-8"), response.data)

    def test_official_page_groups_tools_by_admin_tasks(self):
        self.login()
        response = self.client.get("/official")

        self.assertEqual(response.status_code, 200)
        self.assertIn("选择要完成的工作".encode("utf-8"), response.data)
        self.assertIn("查清楚".encode("utf-8"), response.data)
        self.assertIn("处理风险".encode("utf-8"), response.data)
        self.assertIn("移动帖子到其他栏目".encode("utf-8"), response.data)
        self.assertNotIn("移动帖子到其他版块".encode("utf-8"), response.data)
        self.assertIn("当前只允许查看频道数据".encode("utf-8"), response.data)
        self.assertIn(b'<details class="capability-catalog">', response.data)
        self.assertNotIn(b'<details class="capability-catalog" open>', response.data)

    def test_official_action_uses_synced_dropdown_candidates(self):
        self.insert_tencent_review(feed_id="feed-sync", title="同步候选帖子")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy", "guild_id": "1",
            "channels": {"qa_discussion": "2", "practical_article": "3"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "2": {"name": "WorkBuddy·问答与交流", "expected_sections": ["qa_discussion"]},
            "3": {"name": "WorkBuddy·实用文章", "expected_sections": ["practical_article"]},
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.login()
        response = self.client.get("/official/feed/move-feed")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="param__guild-id"'.encode("utf-8"), response.data)
        self.assertIn('name="param__channel-id"'.encode("utf-8"), response.data)
        self.assertIn('name="param__feed-id"'.encode("utf-8"), response.data)
        self.assertIn("同步候选帖子".encode("utf-8"), response.data)
        self.assertIn(">WorkBuddy</option>".encode("utf-8"), response.data)
        self.assertIn("WorkBuddy · 实用文章".encode("utf-8"), response.data)
        self.assertNotIn("WorkBuddy · 1".encode("utf-8"), response.data)
        self.assertNotIn("实用文章 · 3".encode("utf-8"), response.data)
        self.assertIn(">频道<b".encode("utf-8"), response.data)
        self.assertIn(">栏目<b".encode("utf-8"), response.data)
        self.assertIn(">帖子<b".encode("utf-8"), response.data)
        self.assertNotIn(">频道 ID<b".encode("utf-8"), response.data)

    def test_utc_scan_time_is_displayed_as_beijing_time(self):
        self.assertEqual(_cn_time("2026-08-20T06:18:00+00:00"), "2026-08-20 14:18")
        self.assertEqual(_cn_time("1787123462"), "2026-08-19 15:11")
        self.assertEqual(_cn_time("1787123462000"), "2026-08-19 15:11")

    def test_review_detail_explains_sensitive_term_without_internal_score(self):
        row_id = self.insert_tencent_review()
        self.login()

        response = self.client.get(f"/reviews/tencent/{row_id}")

        self.assertIn("删除这条内容".encode("utf-8"), response.data)
        self.assertNotIn("再次输入管理密码".encode("utf-8"), response.data)
        self.assertIn("敏感词/违禁词".encode("utf-8"), response.data)
        self.assertIn("命中英文敏感词".encode("utf-8"), response.data)
        self.assertNotIn("风险影响 +80".encode("utf-8"), response.data)

    def test_review_detail_hides_internal_ai_connection_error(self):
        row_id = self.insert_tencent_review(title="等待智能判断")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET ai_error = ? WHERE id = ?",
                ("缺少 TENCENT_TOKENHUB_API_KEY", row_id),
            )
        self.login()

        response = self.client.get(f"/reviews/tencent/{row_id}")

        self.assertNotIn(b"TENCENT_TOKENHUB_API_KEY", response.data)
        self.assertIn("智能判断服务尚未连接".encode("utf-8"), response.data)

    def test_review_detail_shows_external_link_result_without_clickable_unknown_url(self):
        row_id = self.insert_tencent_review(title="外链资料")
        analysis = {
            "summary": "帖子引用了相关资料",
            "external_link_status": "normal",
            "external_link_summary": "链接与正文讨论的资料直接相关，未发现诱导跳转。",
            "external_links": ["https://docs.example.com/guide"],
        }
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET analysis_source = 'ai', ai_status = 'completed', ai_analysis_json = ?
                WHERE id = ?
                """,
                (json.dumps(analysis, ensure_ascii=False), row_id),
            )
        self.login()

        response = self.client.get(f"/reviews/tencent/{row_id}")

        self.assertIn("外链检查".encode("utf-8"), response.data)
        self.assertIn("正常资料链接".encode("utf-8"), response.data)
        self.assertIn(b"https://docs.example.com/guide", response.data)
        self.assertNotIn(b'href="https://docs.example.com/guide"', response.data)

    def test_conflicting_high_risk_allow_can_be_resolved_directly(self):
        row_id = self.insert_tencent_review(title="风险与建议冲突")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET action = 'allow' WHERE id = ?",
                (row_id,),
            )
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            f"/reviews/tencent/{row_id}/resolve",
            data={"csrf_token": token, "resolution": "approved", "notes": "人工确认"},
            follow_redirects=True,
        )
        self.assertIn("处理结果已保存".encode("utf-8"), response.data)
        with sqlite3.connect(str(self.database_path)) as connection:
            status = connection.execute(
                "SELECT review_status FROM tencent_moderation_findings WHERE id = ?", (row_id,)
            ).fetchone()[0]
        self.assertEqual(status, "approved")

    def test_dashboard_action_returns_to_dashboard_without_page_jump(self):
        row_id = self.insert_tencent_review()
        response = self.login()

        response = self.client.post(
            f"/reviews/tencent/{row_id}/resolve",
            data={
                "csrf_token": self.csrf(response),
                "resolution": "approved",
                "notes": "人工确认",
                "next": "/?task=delete",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/?task=delete")

    def test_post_requires_csrf(self):
        self.login()
        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 400)

    def test_all_admin_pages_render_after_login(self):
        self.login()
        for path in ["/contents", "/reviews", "/placements", "/ai-analysis", "/duplicates", "/rules", "/channels", "/audit", "/test", "/official"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_all_content_page_includes_posts_without_moderation_findings(self):
        self.insert_cached_content()
        self.login()

        response = self.client.get("/contents")

        self.assertIn("正常帖子".encode("utf-8"), response.data)
        self.assertIn("没有发现问题".encode("utf-8"), response.data)
        self.assertIn("正文已同步".encode("utf-8"), response.data)
        self.assertIn("内容每 30 分钟自动同步".encode("utf-8"), response.data)
        self.assertIn(b'action="/sync"', response.data)
        self.assertNotIn("再次输入管理密码".encode("utf-8"), response.data)

    def test_all_content_prefers_unix_time_over_tencent_local_time_string(self):
        self.insert_cached_content(feed_id="feed-time", title="时间测试")
        with sqlite3.connect(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT detail_json FROM tencent_feed_cache WHERE feed_id = 'feed-time'"
            ).fetchone()
            detail = json.loads(row[0])
            detail["create_time"] = "2026-08-22 22:54:01"
            detail["create_time_raw"] = 1787410441
            connection.execute(
                "UPDATE tencent_feed_cache SET detail_json = ? WHERE feed_id = 'feed-time'",
                (json.dumps(detail, ensure_ascii=False),),
            )
        self.login()

        response = self.client.get("/contents")

        self.assertIn(b"2026-08-22 22:54", response.data)
        self.assertNotIn(b"2026-08-23 06:54", response.data)

    def test_cached_content_can_be_edited_directly(self):
        self.insert_cached_content()
        response = self.login()

        response = self.client.post(
            "/contents/1/feed-ok/edit",
            data={
                "csrf_token": self.csrf(response),
                "title": "修改后的标题",
                "body": "修改后的正文",
            },
            follow_redirects=True,
        )

        self.assertIn("帖子已修改".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.edited[0][5:7], ("修改后的标题", "修改后的正文"))
        item = AdminStore(self.database_path).get_content("1", "feed-ok")
        self.assertEqual((item["title"], item["body"]), ("修改后的标题", "修改后的正文"))

    def test_cached_content_delete_reconciles_post_already_missing_on_tencent(self):
        self.insert_cached_content()
        self.fake_cli.delete_error = TencentCliError(
            "业务错误 (retCode=20074): 请求失败，请稍后重试"
        )
        self.fake_cli.detail_error = TencentCliError(
            "业务错误 (retCode=10014): 呀，来晚了，数据已被删除"
        )
        response = self.login()

        response = self.client.post(
            "/contents/1/feed-ok/delete",
            data={"csrf_token": self.csrf(response)},
            follow_redirects=True,
        )

        self.assertIn("后台记录已同步为已删除".encode("utf-8"), response.data)
        with sqlite3.connect(str(self.database_path)) as connection:
            deleted_at = connection.execute(
                "SELECT deleted_at FROM tencent_feed_cache WHERE guild_id = '1' AND feed_id = 'feed-ok'"
            ).fetchone()[0]
        self.assertTrue(deleted_at)

    def test_failed_repeat_delete_does_not_restore_deleted_cache(self):
        self.insert_cached_content()
        store = AdminStore(self.database_path)
        store.record_content_delete("1", "feed-ok", "admin")

        store.record_content_delete("1", "feed-ok", "admin", "repeat request failed")

        with sqlite3.connect(str(self.database_path)) as connection:
            deleted_at = connection.execute(
                "SELECT deleted_at FROM tencent_feed_cache WHERE guild_id = '1' AND feed_id = 'feed-ok'"
            ).fetchone()[0]
        self.assertTrue(deleted_at)

    def test_official_read_capability_executes_and_is_audited(self):
        response = self.login()
        response = self.client.get("/official/feed/get-feed-detail")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/official/feed/get-feed-detail",
            data={"csrf_token": self.csrf(response), "param__feed-id": "B_test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("操作结果".encode("utf-8"), response.data)
        self.assertEqual(
            self.fake_cli.capability_calls[-1],
            ("feed", "get-feed-detail", {"feed_id": "B_test"}, False, False),
        )

    def test_official_high_risk_action_executes_without_reauthentication(self):
        with patch.dict(os.environ, {"QQ_GUARD_OFFICIAL_WRITES_ENABLED": "true"}):
            self.login()
            response = self.client.get("/official/feed/del-feed")
            response = self.client.post(
                "/official/feed/del-feed",
                data={
                    "csrf_token": self.csrf(response),
                    "param__feed-id": "B_test",
                },
            )
        self.assertIn("操作结果".encode("utf-8"), response.data)
        self.assertEqual(
            self.fake_cli.capability_calls,
            [("feed", "del-feed", {"feed_id": "B_test"}, True, False)],
        )

    def test_content_test_explains_classification(self):
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            "/test",
            data={
                "csrf_token": token,
                "channel_id": "999",
                "title": "每周讨论",
                "body": "#每周一问 请问大家如何选择工具？",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("每周一问".encode("utf-8"), response.data)
        self.assertIn("判断结果".encode("utf-8"), response.data)
        self.assertNotIn("风险影响 +".encode("utf-8"), response.data)

    def test_add_sensitive_term_updates_version(self):
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            "/rules/terms",
            data={
                "csrf_token": token,
                "term": "blockedword",
                "language": "en",
                "category": "custom",
                "severity": "high",
                "action": "review",
                "match_type": "word",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(any(item["term"] == "blockedword" for item in raw["moderation"]["terms"]))
        self.assertNotEqual(raw["moderation"]["policy_version"], "2026-08-19.1")

    def test_admin_can_manage_current_topics_and_content_policies(self):
        response = self.login()
        response = self.client.post(
            "/rules/section-topics",
            data={
                "csrf_token": self.csrf(response),
                "section": "weekly_question",
                "required_hashtags": "#WorkBuddy的哇塞瞬间",
                "enabled": "on",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("指定话题规则已保存".encode("utf-8"), response.data)
        self.assertIn("#WorkBuddy的哇塞瞬间".encode("utf-8"), response.data)

        response = self.client.post(
            "/rules/content-policies",
            data={
                "csrf_token": self.csrf(response),
                "name": "小红书相关内容",
                "keywords": "小红书，XHS，rednote",
                "guidance": "避免直接作答，提醒管理员核对是否适合在频道中讨论",
                "action": "review",
                "enabled": "on",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("重点内容策略已保存".encode("utf-8"), response.data)
        self.assertIn("需要人工核对".encode("utf-8"), response.data)

        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["section_topic_policies"]["weekly_question"]["required_hashtags"],
            ["WorkBuddy的哇塞瞬间"],
        )
        self.assertEqual(raw["content_policies"][0]["keywords"], ["小红书", "XHS", "rednote"])
        self.assertEqual(raw["content_policies"][0]["action"], "review")

    def test_ai_settings_use_tokenhub_hy3_and_vita(self):
        self.login()
        response = self.client.get("/rules")
        response = self.client.post(
            "/rules/ai-review",
            data={
                "csrf_token": self.csrf(response),
                "enabled": "on",
                "include_images": "on",
                "model": "hy3",
                "vision_model": "youtu-vita",
                "minimum_allow_confidence": "0.85",
                "timeout_seconds": "30",
                "vision_timeout_seconds": "45",
                "max_input_chars": "12000",
                "max_images": "3",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["ai_review"]["provider"], "tencent_tokenhub")
        self.assertEqual(raw["ai_review"]["model"], "hy3")
        self.assertEqual(raw["ai_review"]["vision_model"], "youtu-vita")
        self.assertTrue(raw["ai_review"]["include_images"])

    def test_review_resolution_is_audited(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            f"/reviews/tencent/{row_id}/resolve",
            data={"csrf_token": token, "resolution": "approved", "notes": "上下文正常"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(str(self.database_path)) as connection:
            status = connection.execute(
                "SELECT review_status FROM tencent_moderation_findings WHERE id = ?", (row_id,)
            ).fetchone()[0]
            actions = connection.execute(
                "SELECT COUNT(*) FROM admin_audit_actions WHERE action = 'review.resolve'"
            ).fetchone()[0]
        self.assertEqual(status, "approved")
        self.assertEqual(actions, 1)

    def test_review_detail_exposes_actual_ai_execution_state(self):
        row_id = self.insert_tencent_review()
        self.login()
        response = self.client.get(f"/reviews/tencent/{row_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI分析结果".encode("utf-8"), response.data)
        self.assertIn("AI分析状态".encode("utf-8"), response.data)
        self.assertIn("AI分析理由".encode("utf-8"), response.data)
        self.assertIn("图片检查".encode("utf-8"), response.data)
        self.assertNotIn("文字综合判断".encode("utf-8"), response.data)

    def test_ai_analysis_page_exposes_conclusion_or_fallback_reason(self):
        self.insert_tencent_review()
        self.login()
        response = self.client.get("/ai-analysis")
        self.assertEqual(response.status_code, 200)
        self.assertIn("判断记录".encode("utf-8"), response.data)
        self.assertIn("需要人工核对".encode("utf-8"), response.data)
        self.assertIn("当前只有基础内容检查结果".encode("utf-8"), response.data)
        self.assertIn("命中英文敏感词".encode("utf-8"), response.data)

    def test_ai_analysis_paginates_and_normalizes_unsupported_delete_suggestion(self):
        for index in range(9):
            self.insert_tencent_review(f"feed-{index}", f"记录 {index}")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET reasons_json = ? WHERE feed_id = 'feed-0'",
                (json.dumps([{
                    "code": "low_information_content",
                    "category": "quality",
                    "severity": "medium",
                    "message": "正文有效信息量较少",
                    "score": 80,
                    "auto_delete_eligible": False,
                }], ensure_ascii=False),),
            )
        self.login()
        response = self.client.get("/ai-analysis")
        self.assertEqual(response.data.count(b'class="panel analysis-record"'), 8)
        self.assertIn("第 1 / 2 页".encode("utf-8"), response.data)
        response = self.client.get("/ai-analysis?page=2")
        self.assertEqual(response.data.count(b'class="panel analysis-record"'), 1)
        self.assertIn("需要人工核对".encode("utf-8"), response.data)
        self.assertNotIn("发现高风险信号".encode("utf-8"), response.data)

    def test_audit_page_hides_internal_fields_and_paginates(self):
        store = AdminStore(self.database_path)
        store.record_audit(
            "admin",
            "official.failed",
            "feed",
            "move-feed",
            {"error": "retCode=153 request_id=req-secret", "payload": {"feed_id": "hidden"}},
        )
        for index in range(24):
            store.record_audit("admin", "policy.update", "moderation", str(index), {"version": index})
        self.login()
        response = self.client.get("/audit")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'class="audit-item'), 20)
        self.assertIn("第 1 / 2 页".encode("utf-8"), response.data)
        self.assertNotIn(b"official.failed", response.data)
        self.assertNotIn(b"retCode", response.data)
        self.assertNotIn(b"request_id", response.data)
        response = self.client.get("/audit?page=2")
        self.assertIn("频道操作失败".encode("utf-8"), response.data)
        self.assertIn("腾讯平台当前请求较多".encode("utf-8"), response.data)

    def test_duplicate_records_hide_feed_and_channel_ids(self):
        AdminStore(self.database_path)
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_duplicate_actions
                (newer_feed_id, older_feed_id, guild_id, guild_name, channel_id,
                 section, delete_status, error, created_at)
                VALUES ('new-feed-secret', 'old-feed-secret', 'guild-secret',
                        '测试频道', 'channel-secret', 'qa_discussion',
                        'detected_only', '', '2026-08-21T01:00:00+00:00')
                """
            )
        self.login()
        response = self.client.get("/duplicates")
        self.assertIn("重复内容记录".encode("utf-8"), response.data)
        self.assertIn("后发布的内容与前一条完全相同".encode("utf-8"), response.data)
        self.assertIn("已发现，等待人工处理".encode("utf-8"), response.data)
        self.assertNotIn(b"new-feed-secret", response.data)
        self.assertNotIn(b"old-feed-secret", response.data)
        self.assertNotIn(b"channel-secret", response.data)
        self.assertNotIn(b"detected_only", response.data)

    def test_channel_and_content_check_forms_use_named_board_choices(self):
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy",
            "guild_id": "123456789",
            "channels": {"qa_discussion": "987654321"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "987654321": {
                "name": "WorkBuddy · 问答与交流",
                "expected_sections": ["qa_discussion"],
            }
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.login()
        for path in ("/channels", "/test"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn("WorkBuddy · 问答与交流".encode("utf-8"), response.data)
                self.assertNotIn("栏目 ID".encode("utf-8"), response.data)
                self.assertNotIn("栏目编号".encode("utf-8"), response.data)

    def test_delete_executes_directly_for_logged_in_admin(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        response = self.client.post(
            f"/reviews/tencent/{row_id}/delete",
            data={"csrf_token": self.csrf(response)},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake_cli.deleted, [("1", "2", "feed-1", "123456", True)])
        with sqlite3.connect(str(self.database_path)) as connection:
            status = connection.execute(
                "SELECT delete_status FROM tencent_moderation_findings WHERE id = ?", (row_id,)
            ).fetchone()[0]
        self.assertEqual(status, "deleted")

        AdminStore(self.database_path).record_delete_result(
            row_id,
            "admin",
            "failed",
            "重复请求的一次性确认已失效",
            "不应覆盖成功状态",
        )
        with sqlite3.connect(str(self.database_path)) as connection:
            status = connection.execute(
                "SELECT delete_status FROM tencent_moderation_findings WHERE id = ?", (row_id,)
            ).fetchone()[0]
        self.assertEqual(status, "deleted")

    def test_delete_does_not_require_password_field(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        response = self.client.post(
            f"/reviews/tencent/{row_id}/delete",
            data={"csrf_token": self.csrf(response)},
            follow_redirects=True,
        )
        self.assertIn("内容已从腾讯频道删除".encode("utf-8"), response.data)
        self.assertEqual(len(self.fake_cli.deleted), 1)

    def test_bulk_delete_executes_selected_items_directly(self):
        first = self.insert_tencent_review("feed-1", "违规内容一")
        second = self.insert_tencent_review("feed-2", "违规内容二")
        response = self.login()
        response = self.client.post(
            "/reviews/bulk-delete",
            data={
                "csrf_token": self.csrf(response),
                "review_ids": [str(first), str(second)],
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("批量删除完成：成功 2 条、失败 0 条".encode("utf-8"), response.data)
        self.assertEqual(
            self.fake_cli.deleted,
            [
                ("1", "2", "feed-1", "123456", True),
                ("1", "2", "feed-2", "123456", True),
            ],
        )

    def test_bulk_delete_requires_selection(self):
        response = self.login()
        response = self.client.post(
            "/reviews/bulk-delete",
            data={"csrf_token": self.csrf(response)},
            follow_redirects=True,
        )
        self.assertIn("请先勾选要删除的内容".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.deleted, [])

    def test_batch_action_challenge_can_only_be_consumed_once(self):
        store = AdminStore(self.database_path)
        token = store.create_action_challenge("bulk_delete", "admin")
        store.consume_action_challenge("bulk_delete", "admin", token)
        with self.assertRaisesRegex(ValueError, "已经提交"):
            store.consume_action_challenge("bulk_delete", "admin", token)

    def test_review_queue_hides_historical_policy_rows_and_delete_propagates(self):
        old_id = self.insert_tencent_review("same-feed", "旧策略记录", "policy.1")
        latest_id = self.insert_tencent_review("same-feed", "新策略记录", "policy.2")
        store = AdminStore(self.database_path)
        items = store.reviews(status="")
        matching = [item for item in items if item["item_id"] == "same-feed"]
        self.assertEqual([item["id"] for item in matching], [latest_id])
        with self.assertRaisesRegex(ValueError, "历史审核记录"):
            store.ensure_current_tencent_review(old_id)
        store.record_delete_result(latest_id, "admin", "deleted", "", "测试删除成功")
        with sqlite3.connect(str(self.database_path)) as connection:
            states = connection.execute(
                "SELECT delete_status, review_status FROM tencent_moderation_findings WHERE feed_id = 'same-feed' ORDER BY id"
            ).fetchall()
        self.assertEqual(states, [("deleted", "deleted"), ("deleted", "deleted")])

    def test_bulk_move_changes_real_board_and_records_audit(self):
        first = self.insert_tencent_review("feed-1", "应该属于实用文章一")
        second = self.insert_tencent_review("feed-2", "应该属于实用文章二")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [
            {
                "name": "测试频道",
                "guild_id": "1",
                "channels": {"qa_discussion": "2", "practical_article": "3"},
                "poll_interval_seconds": 300,
            }
        ]
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        response = self.login()
        response = self.client.post(
            "/reviews/bulk-move",
            data={
                "csrf_token": self.csrf(response),
                "review_ids": [str(first), str(second)],
                "move_target": "1:3:practical_article",
            },
            follow_redirects=True,
        )
        self.assertIn("已将 2 条内容移动".encode("utf-8"), response.data)
        self.assertEqual(
            self.fake_cli.moved,
            [("1", "2", "3", "feed-1"), ("1", "2", "3", "feed-2")],
        )
        with sqlite3.connect(str(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT channel_id, section, review_status FROM tencent_moderation_findings ORDER BY id"
            ).fetchall()
            audits = connection.execute(
                "SELECT COUNT(*) FROM admin_audit_actions WHERE action = 'move.execute'"
            ).fetchone()[0]
        self.assertEqual(rows, [("3", "practical_article", "approved")] * 2)
        self.assertEqual(audits, 2)

    def test_placement_page_suggests_article_misposted_in_qa_board(self):
        row_id = self.insert_tencent_review("feed-article", "完整的实战案例文章")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy", "guild_id": "1",
            "channels": {"qa_discussion": "2", "practical_article": "3"},
            "poll_interval_seconds": 300,
        }]
        raw["board_policies"] = {
            "2": {"name": "WorkBuddy·问答与交流", "expected_sections": ["qa_discussion"]},
            "3": {"name": "WorkBuddy·实用文章", "expected_sections": ["practical_article", "featured"]},
        }
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET section = 'practical_article', classification_json = ? WHERE id = ?",
                (json.dumps({"section": "practical_article", "confidence": 0.91, "reasons": ["图文案例文章"], "hashtags": [], "validation_issues": []}), row_id),
            )
        self.login()
        response = self.client.get("/placements")
        self.assertIn("栏目调整".encode("utf-8"), response.data)
        self.assertIn("调整到：WorkBuddy · 实用文章".encode("utf-8"), response.data)
        self.assertIn("图文案例文章".encode("utf-8"), response.data)
        self.assertIn(f'value="{row_id}"'.encode(), response.data)

    def test_weekly_without_hashtag_never_gets_weekly_move_recommendation(self):
        row_id = self.insert_tencent_review("feed-weekly", "本周问题")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tencent_channels"] = [{
            "name": "WorkBuddy", "guild_id": "1",
            "channels": {"qa_discussion": "2", "weekly_question": "4"},
            "poll_interval_seconds": 300,
        }]
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE tencent_moderation_findings SET section = 'weekly_question', classification_json = ? WHERE id = ?",
                (json.dumps({"section": "weekly_question", "confidence": 0.9, "reasons": ["每周一问语义"], "hashtags": [], "validation_issues": ["missing_weekly_hashtag"]}), row_id),
            )
        self.login()
        response = self.client.get("/placements")
        self.assertIn("缺少井号话题".encode("utf-8"), response.data)
        self.assertNotIn(b'value="1:4:weekly_question"', response.data)

    def test_scan_runs_in_background_and_reports_progress(self):
        class Report:
            def public_summary(self):
                return {
                    "scanned_feeds": 7,
                    "duplicates": 1,
                    "ai_reviewed": 2,
                    "ai_fallbacks": 0,
                    "ai_vision_reviewed": 1,
                }

        class Monitor:
            def __init__(self, config, client, progress_callback=None):
                self.progress_callback = progress_callback

            def review_cached_once(self):
                self.progress_callback(40, "规则初审", "正在检查敏感词")
                self.progress_callback(95, "保存审核结果", "正在写入记录")
                return Report()

        response = self.login()
        with patch("qq_guard.web.TencentChannelMonitor", Monitor):
            response = self.client.post(
                "/scan",
                data={"csrf_token": self.csrf(response)},
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(response.status_code, 202)
            status_url = response.get_json()["status_url"]
            state = None
            for _ in range(30):
                state = self.client.get(status_url).get_json()
                if state["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["percent"], 100)
        self.assertEqual(state["summary"]["scanned_feeds"], 7)
        self.assertEqual(state["summary"]["ai_vision_reviewed"], 1)

    def test_content_sync_runs_separately_from_ai_review(self):
        sync_calls = []

        class Report:
            def public_summary(self):
                return {
                    "synced_feeds": 12,
                    "new_feeds": 3,
                    "updated_feeds": 1,
                    "cached_feeds": 8,
                    "finished_at": "2026-08-22T14:54:01+00:00",
                }

        class Monitor:
            def __init__(self, config, client, progress_callback=None):
                self.progress_callback = progress_callback

            def sync_once(self, **kwargs):
                sync_calls.append(kwargs)
                self.progress_callback(60, "同步频道内容", "正在读取帖子和图片")
                return Report()

        response = self.login()
        with patch("qq_guard.web.TencentChannelMonitor", Monitor):
            response = self.client.post(
                "/sync",
                data={"csrf_token": self.csrf(response)},
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(response.status_code, 202)
            status_url = response.get_json()["status_url"]
            state = None
            for _ in range(30):
                state = self.client.get(status_url).get_json()
                if state["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["task_type"], "sync")
        self.assertEqual(state["summary"]["synced_feeds"], 12)
        self.assertIn("未执行 AI 分析", state["message"])
        self.assertEqual(sync_calls, [{}])

    def test_scan_rejects_overlapping_run(self):
        response = self.login()
        lock = ScanLock(self.database_path)
        self.assertTrue(lock.acquire())
        try:
            response = self.client.post(
                "/scan",
                data={"csrf_token": self.csrf(response)},
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
        finally:
            lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["status"], "busy")


if __name__ == "__main__":
    unittest.main()
