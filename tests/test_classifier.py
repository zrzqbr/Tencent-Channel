import json
import tempfile
import unittest
from pathlib import Path

from qq_guard.classifier import ContentClassifier
from qq_guard.config import GuardConfig
from qq_guard.models import IncomingContent, ItemKind, Section


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        config_path = Path(self.temp_dir.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "database_path": "guard.sqlite3",
                    "official_author_ids": ["official-user"],
                    "section_hashtags": {
                        "featured": ["精华"],
                        "weekly_question": ["每周一问"],
                        "practical_article": ["实用文章"],
                        "qa_discussion": ["问答与交流"],
                        "official_news": ["官方资讯"]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = GuardConfig.from_file(str(config_path))
        self.classifier = ContentClassifier(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def item(self, body: str, **kwargs) -> IncomingContent:
        values = {
            "platform_item_id": "item-1",
            "kind": ItemKind.FORUM_THREAD,
            "guild_id": "guild-1",
            "channel_id": "channel-1",
            "author_id": "user-1",
            "body": body,
        }
        values.update(kwargs)
        return IncomingContent(**values)

    def test_weekly_question_requires_hashtag(self) -> None:
        result = self.classifier.classify(self.item("欢迎参与每周一问：你最常用什么工具？"))
        self.assertEqual(result.section, Section.UNCLASSIFIED)
        self.assertIn("missing_weekly_hashtag", result.validation_issues)

    def test_weekly_question_with_topic_is_accepted(self) -> None:
        result = self.classifier.classify(self.item("#效率工具 每周一问：你最常用什么工具？"))
        self.assertEqual(result.section, Section.WEEKLY_QUESTION)

    def test_explicit_weekly_hashtag_is_accepted(self) -> None:
        result = self.classifier.classify(self.item("#每周一问 分享你的答案"))
        self.assertEqual(result.section, Section.WEEKLY_QUESTION)

    def test_hashtag_can_touch_preceding_chinese_text(self) -> None:
        result = self.classifier.classify(self.item("欢迎参与#每周一问 分享你的答案"))
        self.assertEqual(result.section, Section.WEEKLY_QUESTION)

    def test_question_goes_to_qa(self) -> None:
        result = self.classifier.classify(self.item("请问大家如何解决这个部署问题？"))
        self.assertEqual(result.section, Section.QA_DISCUSSION)

    def test_rich_practical_case_goes_to_article(self) -> None:
        body = "这是一个真实案例复盘，包含操作步骤、解决方案和最佳实践。" * 4
        result = self.classifier.classify(self.item(body, media_urls=("https://example.com/a.png",)))
        self.assertEqual(result.section, Section.PRACTICAL_ARTICLE)

    def test_featured_requires_explicit_tag(self) -> None:
        result = self.classifier.classify(self.item("#精华 一份完整案例复盘", media_urls=("https://example.com/a.png",)))
        self.assertEqual(result.section, Section.FEATURED)

    def test_official_author_is_official_news(self) -> None:
        result = self.classifier.classify(self.item("版本更新公告", author_id="official-user"))
        self.assertEqual(result.section, Section.OFFICIAL_NEWS)


if __name__ == "__main__":
    unittest.main()
