import json
import tempfile
import unittest
from pathlib import Path

from qq_guard.ai_review import AIReviewClient
from qq_guard.config import AIReviewSettings
from qq_guard.models import (
    ClassificationResult,
    IncomingContent,
    ItemKind,
    ModerationAction,
    ModerationAssessment,
    RiskLevel,
    Section,
)


def review_response(action="allow", score=5):
    level = "low" if score < 25 else "medium"
    return {
        "output_text": json.dumps(
            {
                "section": "practical_article",
                "classification_confidence": 0.93,
                "risk_level": level,
                "risk_score": score,
                "recommended_action": action,
                "summary": "图文结合的完整案例文章",
                "reasons": [],
            },
            ensure_ascii=False,
        )
    }


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "guard.sqlite3"
        self.settings = AIReviewSettings(
            enabled=True,
            provider="tencent_tokenhub",
            model="hy3",
            vision_model="youtu-vita",
            include_images=True,
        )
        self.item = IncomingContent(
            platform_item_id="feed-1",
            kind=ItemKind.FORUM_THREAD,
            guild_id="100",
            channel_id="200",
            author_id="author-1",
            title="数据库迁移实战",
            body="本文记录迁移步骤和故障复盘。",
            media_urls=("https://example.com/case.png",),
        )
        self.classification = ClassificationResult(
            section=Section.PRACTICAL_ARTICLE,
            confidence=0.8,
            reasons=("命中实战关键词",),
            hashtags=("实用文章",),
        )
        self.assessment = ModerationAssessment(
            action=ModerationAction.ALLOW,
            risk_level=RiskLevel.LOW,
            risk_score=0,
            policy_version="test.ai1",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_vita_evidence_is_sent_to_hy3_and_both_results_are_cached(self):
        calls = []

        def transport(url, headers, body, timeout):
            payload = json.loads(body.decode("utf-8"))
            calls.append((url, payload))
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            if url.endswith("/chat/completions"):
                self.assertEqual(payload["model"], "youtu-vita")
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "图片是迁移步骤截图，包含命令和结果，无风险内容。"
                            }
                        }
                    ]
                }
            self.assertTrue(url.endswith("/responses"))
            self.assertEqual(payload["model"], "hy3")
            evidence = json.loads(payload["input"][0]["content"][0]["text"])
            self.assertIn("迁移步骤截图", evidence["vision"]["analysis"])
            self.assertEqual(payload["text"]["format"]["type"], "json_schema")
            return review_response()

        client = AIReviewClient(
            self.settings,
            self.database_path,
            api_key="test-key",
            transport=transport,
        )
        first = client.review(
            self.item, None, self.classification, self.assessment
        )
        second = client.review(
            self.item, None, self.classification, self.assessment
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(first.vision_status, "completed")
        self.assertEqual(second.status, "cached")
        self.assertEqual(second.vision_status, "cached")

    def test_vision_failure_forces_human_review_even_if_hy3_allows(self):
        def transport(url, headers, body, timeout):
            if url.endswith("/chat/completions"):
                return {"choices": []}
            return review_response(action="allow", score=0)

        decision = AIReviewClient(
            self.settings,
            self.database_path,
            api_key="test-key",
            transport=transport,
        ).review(self.item, None, self.classification, self.assessment)

        self.assertEqual(decision.vision_status, "failed")
        self.assertEqual(decision.recommended_action, ModerationAction.REVIEW)
        self.assertGreaterEqual(decision.risk_score, 25)
        self.assertIn("vision_unavailable", [reason.code for reason in decision.reasons])


if __name__ == "__main__":
    unittest.main()
