import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from qq_guard.admin_store import AdminStore
from qq_guard.web import create_app


TEST_PASSWORD = "test-only-password-9!"


class FakeTencentClient:
    def __init__(self) -> None:
        self.deleted = []

    def delete_feed(self, guild_id, channel_id, feed_id, create_time, live):
        self.deleted.append((guild_id, channel_id, feed_id, create_time, live))
        return {"success": True}

    def get_feed_detail(self, guild_id, channel_id, feed_id):
        return {"create_time_raw": "123456"}


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
        self.assertIn("治理总览".encode("utf-8"), response.data)
        return response

    def insert_tencent_review(self) -> int:
        AdminStore(self.database_path)
        with sqlite3.connect(str(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO tencent_moderation_findings
                (guild_id, guild_name, channel_id, feed_id, title, section, action,
                 risk_level, risk_score, policy_version, reasons_json, review_status,
                 author_id, body, media_urls_json, source_created_at, classification_json,
                 delete_status, created_at)
                VALUES ('1', '测试频道', '2', 'feed-1', '违规测试', 'qa_discussion',
                        'delete_candidate', 'high', 80, '2026-08-19.1', ?, 'pending',
                        'author', 'casino', '[]', '123456', '{}', 'not_requested',
                        '2026-08-19T00:00:00+00:00')
                """,
                (json.dumps([{"code": "sensitive_term_en", "message": "命中英文敏感词", "score": 80}]),),
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

    def test_post_requires_csrf(self):
        self.login()
        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 400)

    def test_all_admin_pages_render_after_login(self):
        self.login()
        for path in ["/reviews", "/duplicates", "/rules", "/channels", "/audit", "/test"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

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
                "confirmation": "删除",
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
                "confirmation": "删除",
                "reason": "不应实际执行删除操作",
            },
            follow_redirects=True,
        )
        self.assertIn("密码验证失败".encode("utf-8"), response.data)
        self.assertEqual(self.fake_cli.deleted, [])


if __name__ == "__main__":
    unittest.main()
