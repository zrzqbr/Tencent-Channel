import json
import tempfile
import unittest
from pathlib import Path

from qq_guard.ai_review import AIReviewClient, fuse_ai_review
from qq_guard.config import (
    AIReviewSettings,
    ContentPolicy,
    GuardConfig,
    SectionTopicPolicy,
)
from qq_guard.models import (
    AIReviewDecision,
    ClassificationResult,
    IncomingContent,
    ItemKind,
    ModerationAction,
    ModerationAssessment,
    PolicyReason,
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
                "external_link_status": "not_found",
                "external_link_summary": "未发现外链",
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
            self.assertEqual(
                evidence["required_section_topics"][0]["required_hashtags"],
                ["#WorkBuddy的哇塞瞬间"],
            )
            self.assertEqual(
                evidence["administrator_policies"][0]["name"],
                "小红书相关内容",
            )
            self.assertEqual(payload["text"]["format"]["type"], "json_schema")
            return review_response()

        policy_config = GuardConfig(
            database_path=self.database_path,
            section_topic_policies={
                Section.WEEKLY_QUESTION: SectionTopicPolicy(
                    section=Section.WEEKLY_QUESTION,
                    required_hashtags=("WorkBuddy的哇塞瞬间",),
                )
            },
            content_policies=(
                ContentPolicy(
                    name="小红书相关内容",
                    keywords=("小红书", "XHS", "rednote"),
                    guidance="避免直接作答，提醒管理员人工核对",
                    action="review",
                ),
            ),
        )
        client = AIReviewClient(
            self.settings,
            self.database_path,
            api_key="test-key",
            transport=transport,
            policy_config=policy_config,
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

    def test_invalid_hy3_json_is_retried_once_before_fallback(self):
        hy3_calls = 0

        def transport(url, headers, body, timeout):
            nonlocal hy3_calls
            if url.endswith("/chat/completions"):
                return {"choices": [{"message": {"content": "图片无风险内容"}}]}
            hy3_calls += 1
            if hy3_calls < 2:
                return {"output_text": '{"section":"practical_article"'}
            return review_response()

        decision = AIReviewClient(
            self.settings,
            self.database_path,
            api_key="test-key",
            transport=transport,
        ).review(self.item, None, self.classification, self.assessment)

        self.assertEqual(hy3_calls, 2)
        self.assertEqual(decision.status, "completed")
        self.assertEqual(decision.section, Section.PRACTICAL_ARTICLE)

    def test_high_score_allow_with_only_low_risk_evidence_is_normalized(self):
        client = AIReviewClient(self.settings, self.database_path, api_key="test-key")
        value = {
            "section": "practical_article",
            "classification_confidence": 0.98,
            "risk_level": "critical",
            "risk_score": 95,
            "recommended_action": "allow",
            "summary": "正常教程文章",
            "reasons": [{
                "code": "section_match",
                "category": "practical_article",
                "severity": "low",
                "message": "内容与栏目一致",
                "evidence": "包含完整步骤",
                "score": 1,
            }],
        }

        decision = client._decision(value, "completed")

        self.assertEqual(decision.recommended_action, ModerationAction.ALLOW)
        self.assertEqual(decision.risk_level, RiskLevel.LOW)
        self.assertEqual(decision.risk_score, 1)
        self.assertIn("ai_score_normalized", [reason.code for reason in decision.reasons])

    def test_text_link_and_board_policy_are_sent_to_ai(self):
        captured = {}
        item = IncomingContent(
            platform_item_id="feed-link",
            kind=ItemKind.FORUM_THREAD,
            guild_id="100",
            channel_id="200",
            author_id="author-1",
            title="资料讨论",
            body="参考 docs.example.com/guide 后再讨论",
        )

        def transport(url, headers, body, timeout):
            captured.update(json.loads(body.decode("utf-8")))
            return review_response()

        client = AIReviewClient(
            self.settings,
            self.database_path,
            api_key="test-key",
            transport=transport,
        )
        client.review(item, None, self.classification, self.assessment)
        evidence = json.loads(captured["input"][0]["content"][0]["text"])

        self.assertEqual(
            evidence["external_links"],
            [{"url": "docs.example.com/guide", "domain": "docs.example.com"}],
        )

    def test_prohibited_external_link_cannot_be_returned_as_allow(self):
        client = AIReviewClient(self.settings, self.database_path, api_key="test-key")
        value = {
            "section": "qa_discussion",
            "classification_confidence": 0.98,
            "risk_level": "low",
            "risk_score": 0,
            "recommended_action": "allow",
            "summary": "普通讨论",
            "external_link_status": "normal",
            "external_link_summary": "正文引用了站外资料",
            "reasons": [],
        }

        decision = client._decision(
            value,
            "completed",
            detected_external_links=("docs.example.com/guide",),
            allow_external_links=False,
        )

        self.assertEqual(decision.external_link_status, "prohibited")
        self.assertIn("当前栏目明确禁止", decision.external_link_summary)
        self.assertEqual(decision.recommended_action, ModerationAction.REVIEW)
        self.assertGreaterEqual(decision.risk_score, 25)
        self.assertIn("external_link_not_allowed", [reason.code for reason in decision.reasons])

    def test_uncertain_image_qr_cannot_be_returned_as_allow(self):
        ai = AIReviewDecision(
            section=Section.QA_DISCUSSION,
            classification_confidence=0.95,
            risk_level=RiskLevel.LOW,
            risk_score=0,
            recommended_action=ModerationAction.ALLOW,
            summary="图片中有二维码",
            external_link_status="uncertain",
            external_link_summary="图片中有二维码，但旁边没有用途说明。",
        )

        _, assessment = fuse_ai_review(
            self.classification,
            self.assessment,
            ai,
            self.settings,
        )

        self.assertEqual(assessment.action, ModerationAction.REVIEW)
        self.assertIn("external_link_uncertain", [reason.code for reason in assessment.reasons])

    def test_admin_review_policy_cannot_be_overridden_by_ai_allow(self):
        policy_reason = self.assessment.reasons + (
            PolicyReason(
                code="content_policy_review",
                category="content_policy",
                severity="medium",
                message="涉及小红书，请人工核对",
                evidence="触发内容：小红书",
                score=25,
            ),
        )
        rule_assessment = ModerationAssessment(
            action=ModerationAction.REVIEW,
            risk_level=RiskLevel.MEDIUM,
            risk_score=25,
            policy_version="test.ai1",
            reasons=policy_reason,
        )
        ai = AIReviewDecision(
            section=Section.QA_DISCUSSION,
            classification_confidence=0.95,
            risk_level=RiskLevel.LOW,
            risk_score=0,
            recommended_action=ModerationAction.ALLOW,
            summary="普通问答",
        )

        _, assessment = fuse_ai_review(
            self.classification,
            rule_assessment,
            ai,
            self.settings,
        )

        self.assertEqual(assessment.action, ModerationAction.REVIEW)
        self.assertIn("content_policy_review", [reason.code for reason in assessment.reasons])

    def test_current_topic_keeps_section_when_ai_guesses_another_section(self):
        classification = ClassificationResult(
            section=Section.WEEKLY_QUESTION,
            confidence=1.0,
            reasons=("命中当前指定话题",),
            hashtags=("workbuddy的哇塞瞬间",),
        )
        ai = AIReviewDecision(
            section=Section.PRACTICAL_ARTICLE,
            classification_confidence=0.9,
            risk_level=RiskLevel.LOW,
            risk_score=0,
            recommended_action=ModerationAction.ALLOW,
            summary="看起来像实用文章",
        )
        topic_policies = {
            Section.WEEKLY_QUESTION: SectionTopicPolicy(
                section=Section.WEEKLY_QUESTION,
                required_hashtags=("WorkBuddy的哇塞瞬间",),
            )
        }

        merged, _ = fuse_ai_review(
            classification,
            self.assessment,
            ai,
            self.settings,
            topic_policies,
        )

        self.assertEqual(merged.section, Section.WEEKLY_QUESTION)
        self.assertEqual(merged.reasons, classification.reasons)


if __name__ == "__main__":
    unittest.main()
