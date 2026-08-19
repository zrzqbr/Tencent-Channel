import json
import tempfile
import unittest
from pathlib import Path

from qq_guard.classifier import ContentClassifier
from qq_guard.config import GuardConfig
from qq_guard.models import IncomingContent, ItemKind, ModerationAction, Section
from qq_guard.moderation import ModerationEngine


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "database_path": "audit.sqlite3",
                    "section_hashtags": {
                        "practical_article": ["实用文章"],
                        "qa_discussion": ["问答与交流"],
                        "weekly_question": ["每周一问"],
                    },
                    "board_policies": {
                        "100": {
                            "name": "文章",
                            "expected_sections": ["practical_article", "featured", "official_news", "weekly_question"],
                            "require_hashtag": False,
                            "min_text_length": 10,
                            "allow_external_links": True,
                        },
                        "101": {
                            "name": "交流讨论",
                            "expected_sections": ["qa_discussion"],
                            "min_text_length": 4,
                            "allow_external_links": False,
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = GuardConfig.from_file(str(path))
        self.classifier = ContentClassifier(self.config)
        self.engine = ModerationEngine(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def item(self, body, title="", channel="100", media=()):
        return IncomingContent(
            platform_item_id="item-1",
            kind=ItemKind.FORUM_THREAD,
            guild_id="guild-1",
            channel_id=channel,
            author_id="user-1",
            title=title,
            body=body,
            media_urls=tuple(media),
        )

    def assess(self, item):
        classification = self.classifier.classify(item)
        return classification, self.engine.evaluate(item, classification)

    def test_chinese_sensitive_term_is_explainable(self):
        _, assessment = self.assess(self.item("你这个傻逼不要灌水"))
        self.assertIn("sensitive_term_zh", [r.code for r in assessment.reasons])
        self.assertEqual(assessment.action, ModerationAction.DELETE_CANDIDATE)

    def test_english_sensitive_term_uses_word_boundary(self):
        _, hit = self.assess(self.item("This is s-b content"))
        self.assertIn("sensitive_term_en", [r.code for r in hit.reasons])
        _, safe = self.assess(
            self.item("A practical guide to subscribe events", media=("image.png",))
        )
        self.assertNotIn("sensitive_term_en", [r.code for r in safe.reasons])

    def test_contact_information_enters_review(self):
        _, assessment = self.assess(self.item("需要资料请加微信 abcde123"))
        codes = [reason.code for reason in assessment.reasons]
        self.assertIn("contact_information_detected", codes)
        self.assertNotEqual(assessment.action, ModerationAction.ALLOW)

    def test_short_repeated_content_is_flagged(self):
        _, assessment = self.assess(self.item("哈哈哈哈哈"))
        codes = [reason.code for reason in assessment.reasons]
        self.assertIn("repeated_characters", codes)
        self.assertIn("classification_uncertain", codes)

    def test_board_section_mismatch_is_flagged(self):
        _, assessment = self.assess(
            self.item("请问这个问题怎么解决？", channel="100")
        )
        self.assertIn("section_mismatch", [r.code for r in assessment.reasons])

    def test_long_rich_article_with_internal_questions_is_article(self):
        item = self.item(
            "以下提示词包含多个示例问题？每一项都给出完整说明和使用方式。" * 6,
            title="8个实用提示词",
            media=("image.png",),
        )
        classification, assessment = self.assess(item)
        self.assertEqual(classification.section, Section.PRACTICAL_ARTICLE)
        self.assertNotIn("section_mismatch", [r.code for r in assessment.reasons])


if __name__ == "__main__":
    unittest.main()
