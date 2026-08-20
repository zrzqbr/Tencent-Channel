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
from qq_guard.scan_control import ScanLock
from qq_guard.web import _cn_time, create_app


TEST_PASSWORD = "test-only-password-9!"


class FakeTencentClient:
    def __init__(self) -> None:
        self.deleted = []
        self.moved = []
        self.capability_calls = []

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
                        "short": "移动帖子",
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
        return {"success": True}

    def get_feed_detail(self, guild_id, channel_id, feed_id):
        return {"create_time_raw": "123456"}

    def move_feed(self, guild_id, original_channel_id, target_channel_id, feed_id):
        self.moved.append((guild_id, original_channel_id, target_channel_id, feed_id))
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
        self.assertIn("今日待办".encode("utf-8"), response.data)
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
        self.assertIn("抓取内容".encode("utf-8"), response.data)
        self.assertIn("图片识别".encode("utf-8"), response.data)
        self.assertIn("AI 综合判断".encode("utf-8"), response.data)
        self.assertIn("等待审批".encode("utf-8"), response.data)
        self.assertIn("全部内容".encode("utf-8"), response.data)

    def test_dashboard_exposes_task_queues_and_review_drawer(self):
        self.insert_tencent_review()
        response = self.login()
        self.assertIn("没问题，可保留".encode("utf-8"), response.data)
        self.assertIn("放错栏目".encode("utf-8"), response.data)
        self.assertIn("需要你判断".encode("utf-8"), response.data)
        self.assertIn("可能要删除".encode("utf-8"), response.data)
        self.assertIn("机器没看完整".encode("utf-8"), response.data)
        self.assertIn("下一步".encode("utf-8"), response.data)
        self.assertIn("问题类型".encode("utf-8"), response.data)
        self.assertIn("看证据和分数".encode("utf-8"), response.data)

    def test_official_page_groups_tools_by_admin_tasks(self):
        self.login()
        response = self.client.get("/official")

        self.assertEqual(response.status_code, 200)
        self.assertIn("官方工具台".encode("utf-8"), response.data)
        self.assertIn("查清楚".encode("utf-8"), response.data)
        self.assertIn("处理风险".encode("utf-8"), response.data)
        self.assertIn("当前关闭，只能预演".encode("utf-8"), response.data)

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
        self.assertIn("WorkBuddy · 1".encode("utf-8"), response.data)
        self.assertIn("WorkBuddy · 实用文章 · 3".encode("utf-8"), response.data)

    def test_utc_scan_time_is_displayed_as_beijing_time(self):
        self.assertEqual(_cn_time("2026-08-20T06:18:00+00:00"), "2026-08-20 14:18")

    def test_review_detail_explains_sensitive_term_and_score(self):
        row_id = self.insert_tencent_review()
        self.login()

        response = self.client.get(f"/reviews/tencent/{row_id}")

        self.assertIn("进入删除确认".encode("utf-8"), response.data)
        self.assertIn("敏感词/违禁词".encode("utf-8"), response.data)
        self.assertIn("命中英文敏感词".encode("utf-8"), response.data)
        self.assertIn("风险贡献 +80".encode("utf-8"), response.data)

    def test_conflicting_high_risk_allow_requires_explicit_confirmation(self):
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
        self.assertIn("风险等级与建议动作不一致".encode("utf-8"), response.data)
        with sqlite3.connect(str(self.database_path)) as connection:
            status = connection.execute(
                "SELECT review_status FROM tencent_moderation_findings WHERE id = ?", (row_id,)
            ).fetchone()[0]
        self.assertEqual(status, "pending")

        response = self.client.post(
            f"/reviews/tencent/{row_id}/resolve",
            data={
                "csrf_token": self.csrf(response),
                "resolution": "approved",
                "notes": "已核对原文和证据",
                "conflict_confirmation": "confirmed",
            },
            follow_redirects=True,
        )
        self.assertIn("审核结论已保存".encode("utf-8"), response.data)

    def test_post_requires_csrf(self):
        self.login()
        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 400)

    def test_all_admin_pages_render_after_login(self):
        self.login()
        for path in ["/reviews", "/placements", "/ai-analysis", "/duplicates", "/rules", "/channels", "/audit", "/test", "/official"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_official_read_capability_executes_and_is_audited(self):
        response = self.login()
        response = self.client.get("/official/feed/get-feed-detail")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/official/feed/get-feed-detail",
            data={"csrf_token": self.csrf(response), "param__feed-id": "B_test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("官方返回".encode("utf-8"), response.data)
        self.assertEqual(
            self.fake_cli.capability_calls[-1],
            ("feed", "get-feed-detail", {"feed_id": "B_test"}, False, False),
        )

    def test_official_high_risk_live_action_requires_password(self):
        self.login()
        response = self.client.get("/official/feed/del-feed")
        response = self.client.post(
            "/official/feed/del-feed",
            data={
                "csrf_token": self.csrf(response),
                "param__feed-id": "B_test",
                "execution_mode": "live",
                "password": "wrong",
                "reason": "测试高风险保护",
                "confirmation": "confirmed",
                "confirmation_phrase": "确认执行",
            },
        )
        self.assertIn("尚未开启官方写操作".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.capability_calls, [])

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
        self.assertIn("检测结果".encode("utf-8"), response.data)

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
        self.assertIn("审核执行轨迹".encode("utf-8"), response.data)
        self.assertIn("当前为规则判定".encode("utf-8"), response.data)
        self.assertIn("Youtu-VITA 图片分析".encode("utf-8"), response.data)
        self.assertIn("Hy3 综合判断".encode("utf-8"), response.data)

    def test_ai_analysis_page_exposes_conclusion_or_fallback_reason(self):
        self.insert_tencent_review()
        self.login()
        response = self.client.get("/ai-analysis")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI 判断依据".encode("utf-8"), response.data)
        self.assertIn("规则判定".encode("utf-8"), response.data)
        self.assertIn("本条没有执行大模型分析".encode("utf-8"), response.data)
        self.assertIn("命中英文敏感词".encode("utf-8"), response.data)

    def test_delete_requires_second_confirmation_and_reauthentication(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            f"/reviews/tencent/{row_id}/prepare-delete",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("这一步会真实删除频道内容".encode("utf-8"), response.data)
        token = self.csrf(response)
        response = self.client.post(
            f"/reviews/tencent/{row_id}/delete",
            data={
                "csrf_token": token,
                "password": TEST_PASSWORD,
                "confirmation": "confirmed",
                "reason": "命中明确的违规敏感词规则",
            },
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

    def test_delete_fails_with_wrong_password(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        token = self.csrf(response)
        response = self.client.post(
            f"/reviews/tencent/{row_id}/prepare-delete",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        response = self.client.post(
            f"/reviews/tencent/{row_id}/delete",
            data={
                "csrf_token": self.csrf(response),
                "password": "bad",
                "confirmation": "confirmed",
                "reason": "不应实际执行删除操作",
            },
            follow_redirects=True,
        )
        self.assertIn("密码验证失败".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.deleted, [])

    def test_bulk_delete_requires_selection_and_second_confirmation(self):
        first = self.insert_tencent_review("feed-1", "违规内容一")
        second = self.insert_tencent_review("feed-2", "违规内容二")
        response = self.login()
        response = self.client.post(
            "/reviews/bulk-delete/prepare",
            data={
                "csrf_token": self.csrf(response),
                "review_ids": [str(first), str(second)],
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("将真实删除 2 条".encode("utf-8"), response.data)
        response = self.client.post(
            "/reviews/bulk-delete",
            data={
                "csrf_token": self.csrf(response),
                "password": TEST_PASSWORD,
                "confirmation": "confirmed",
                "reason": "两条均命中明确违规规则",
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

    def test_bulk_delete_wrong_password_deletes_nothing(self):
        row_id = self.insert_tencent_review()
        response = self.login()
        response = self.client.post(
            "/reviews/bulk-delete/prepare",
            data={"csrf_token": self.csrf(response), "review_ids": [str(row_id)]},
            follow_redirects=True,
        )
        response = self.client.post(
            "/reviews/bulk-delete",
            data={
                "csrf_token": self.csrf(response),
                "password": "wrong",
                "confirmation": "confirmed",
                "reason": "不应执行任何删除",
            },
            follow_redirects=True,
        )
        self.assertIn("未执行任何删除".encode("utf-8"), response.data)
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
            "/reviews/bulk-move/prepare",
            data={
                "csrf_token": self.csrf(response),
                "review_ids": [str(first), str(second)],
                "move_target": "1:3:practical_article",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("栏目调整二次确认".encode("utf-8"), response.data)
        response = self.client.post(
            "/reviews/bulk-move",
            data={
                "csrf_token": self.csrf(response),
                "password": TEST_PASSWORD,
                "confirmation": "confirmed",
                "reason": "这些内容是完整案例文章",
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
        self.assertIn("栏目移动".encode("utf-8"), response.data)
        self.assertIn("建议移入：WorkBuddy · 实用文章".encode("utf-8"), response.data)
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
                }

        class Monitor:
            def __init__(self, config, client, progress_callback=None):
                self.progress_callback = progress_callback

            def scan_once(self):
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
