import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from qq_guard.classifier import ContentClassifier
from qq_guard.config import GuardConfig
from qq_guard.models import DeleteResult, IncomingContent, ItemKind
from qq_guard.service import GuardService
from qq_guard.storage import AuditStore


class FakeDeleteAdapter:
    def __init__(self, fail: bool = False) -> None:
        self.deleted = []
        self.fail = fail

    async def delete(self, item: IncomingContent) -> DeleteResult:
        self.deleted.append(item.platform_item_id)
        if self.fail:
            return DeleteResult(status="failed", error="permission denied")
        return DeleteResult(status="deleted")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        config_path = Path(self.temp_dir.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "database_path": "guard.sqlite3",
                    "delete_mode": "live",
                    "auto_delete_duplicates": True,
                    "section_hashtags": {
                        "weekly_question": ["每周一问"],
                        "qa_discussion": ["问答与交流"],
                        "practical_article": ["实用文章"],
                        "featured": ["精华"],
                        "official_news": ["官方资讯"]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = GuardConfig.from_file(str(config_path))
        self.adapter = FakeDeleteAdapter()
        self.store = AuditStore(self.config.database_path)
        self.service = GuardService(
            self.config,
            ContentClassifier(self.config),
            self.store,
            self.adapter,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def item(self, item_id: str, author: str = "user-1", body: str = "#问答与交流 同一个问题？") -> IncomingContent:
        return IncomingContent(
            platform_item_id=item_id,
            kind=ItemKind.FORUM_THREAD,
            guild_id="guild-1",
            channel_id="channel-1",
            author_id=author,
            body=body,
        )

    async def test_second_identical_consecutive_item_is_deleted(self) -> None:
        first = await self.service.handle(self.item("item-1"))
        second = await self.service.handle(self.item("item-2"))
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.previous_platform_item_id, "item-1")
        self.assertEqual(second.delete_status, "deleted")
        self.assertEqual(self.adapter.deleted, ["item-2"])
        self.assertIn("exact_consecutive_duplicate", [r.code for r in second.decision_reasons])

    async def test_different_author_is_not_deleted(self) -> None:
        await self.service.handle(self.item("item-1", author="user-1"))
        second = await self.service.handle(self.item("item-2", author="user-2"))
        self.assertFalse(second.duplicate)
        self.assertEqual(self.adapter.deleted, [])

    async def test_intervening_content_breaks_consecutive_duplicate(self) -> None:
        await self.service.handle(self.item("item-1", body="#问答与交流 A？"))
        await self.service.handle(self.item("item-2", body="#问答与交流 B？"))
        third = await self.service.handle(self.item("item-3", body="#问答与交流 A？"))
        self.assertFalse(third.duplicate)

    async def test_same_content_in_different_sections_is_not_duplicate(self) -> None:
        await self.service.handle(self.item("item-1", body="#问答与交流 相同正文"))
        second = await self.service.handle(self.item("item-2", body="#实用文章 相同正文"))
        self.assertFalse(second.duplicate)

    async def test_gateway_redelivery_is_ignored(self) -> None:
        await self.service.handle(self.item("item-1"))
        repeated_event = await self.service.handle(self.item("item-1"))
        self.assertTrue(repeated_event.redelivery)
        self.assertEqual(repeated_event.delete_status, "redelivery_ignored")

    async def test_delete_failure_is_audited(self) -> None:
        failing_adapter = FakeDeleteAdapter(fail=True)
        service = GuardService(
            self.config,
            ContentClassifier(self.config),
            self.store,
            failing_adapter,
        )
        await service.handle(self.item("item-1"))
        second = await service.handle(self.item("item-2"))
        self.assertEqual(second.delete_status, "failed")
        recent = self.store.recent_events(1, duplicates_only=True)
        self.assertEqual(recent[0]["delete_status"], "failed")
        self.assertEqual(recent[0]["delete_error"], "permission denied")

    async def test_sensitive_content_requires_review_without_auto_delete(self) -> None:
        decision = await self.service.handle(
            self.item("item-sensitive", body="#问答与交流 赌博推广")
        )
        self.assertEqual(decision.recommended_action, "delete_candidate")
        self.assertEqual(decision.delete_status, "review_required")
        self.assertEqual(self.adapter.deleted, [])
        self.assertIn("sensitive_term_zh", [r.code for r in decision.decision_reasons])
        queue = self.store.review_queue()
        self.assertEqual(queue[0]["platform_item_id"], "item-sensitive")
        self.assertEqual(queue[0]["review_status"], "pending")

    async def test_explicit_policy_rule_can_delete_only_when_enabled(self) -> None:
        enabled = replace(self.config, auto_delete_policy_violations=True)
        adapter = FakeDeleteAdapter()
        service = GuardService(
            enabled,
            ContentClassifier(enabled),
            self.store,
            adapter,
        )
        decision = await service.handle(
            self.item("item-policy-delete", body="#问答与交流 赌博推广")
        )
        self.assertEqual(decision.delete_status, "deleted")
        self.assertEqual(adapter.deleted, ["item-policy-delete"])

    async def test_quality_score_alone_never_auto_deletes(self) -> None:
        moderation = replace(self.config.moderation, delete_candidate_threshold=50)
        enabled = replace(
            self.config,
            auto_delete_policy_violations=True,
            moderation=moderation,
        )
        adapter = FakeDeleteAdapter()
        service = GuardService(
            enabled,
            ContentClassifier(enabled),
            self.store,
            adapter,
        )
        decision = await service.handle(self.item("item-quality", body="哈哈哈哈哈"))
        self.assertEqual(decision.recommended_action, "delete_candidate")
        self.assertEqual(decision.delete_status, "review_required")
        self.assertEqual(adapter.deleted, [])


if __name__ == "__main__":
    unittest.main()
