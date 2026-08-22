import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_guard.config import KnowledgeBaseSettings
from qq_guard.knowledge_base import (
    KnowledgeAnswerService,
    KnowledgeBaseClient,
    KnowledgeBaseUnavailable,
    initialize_knowledge_schema,
)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = self.root / "workbuddy-kb"
        self.cli.write_text("#!/bin/sh\n", encoding="utf-8")
        self.settings = KnowledgeBaseSettings(
            enabled=True,
            cli_path=self.cli,
            top_k=5,
            timeout_seconds=3,
            answer_model="hy3",
            max_answer_chars=1200,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def result(status="ready", can_answer=True):
        return {
            "coverage": "strong" if can_answer else "partial",
            "answer_status": status,
            "can_answer": can_answer,
            "index_updated_at": "2026-08-23T09:00:00+08:00",
            "matches": [
                {
                    "title": "定价",
                    "url": "https://www.workbuddy.cn/docs/workbuddy/Pricing",
                    "source_type": "official-docs",
                    "auto_answer_eligible": True,
                    "passages": [
                        {"heading": "个人版 > 加量包", "text": "付费会员可以购买加量包。"}
                    ],
                }
            ],
        }

    def test_search_validates_ready_evidence(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(self.result()), stderr=""
        )
        client = KnowledgeBaseClient(self.settings, api_key="test")
        with patch("qq_guard.knowledge_base.subprocess.run", return_value=completed) as run:
            lookup = client.search("积分怎么充值")
        self.assertTrue(lookup.can_answer)
        self.assertEqual(lookup.answer_status, "ready")
        self.assertEqual(run.call_args.args[0][1:3], ["search", "--query"])

    def test_ready_without_eligible_passage_is_downgraded(self):
        value = self.result()
        value["matches"][0]["auto_answer_eligible"] = False
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(value), stderr=""
        )
        client = KnowledgeBaseClient(self.settings)
        with patch("qq_guard.knowledge_base.subprocess.run", return_value=completed):
            lookup = client.search("积分怎么充值")
        self.assertFalse(lookup.can_answer)
        self.assertEqual(lookup.answer_status, "review")

    def test_timeout_and_invalid_json_are_safe_failures(self):
        client = KnowledgeBaseClient(self.settings)
        with patch(
            "qq_guard.knowledge_base.subprocess.run",
            side_effect=subprocess.TimeoutExpired("kb", 3),
        ):
            with self.assertRaises(KnowledgeBaseUnavailable):
                client.search("问题")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="bad", stderr="")
        with patch("qq_guard.knowledge_base.subprocess.run", return_value=completed):
            with self.assertRaises(KnowledgeBaseUnavailable):
                client.search("问题")

    def test_generation_uses_evidence_and_appends_official_source(self):
        requests = []

        def transport(url, headers, body, timeout):
            requests.append(json.loads(body))
            return {"output_text": json.dumps({"answer": "付费会员可在积分将用完时购买加量包。"})}

        client = KnowledgeBaseClient(self.settings, api_key="test", transport=transport)
        lookup = client._validate_lookup(self.result())
        draft = client.generate_draft("积分怎么充值", lookup)
        evidence = json.loads(requests[0]["input"][0]["content"][0]["text"])
        self.assertEqual(evidence["question"], "积分怎么充值")
        self.assertIn("付费会员可以购买加量包", evidence["official_evidence"][0]["passages"][0]["text"])
        self.assertIn("https://www.workbuddy.cn/docs/workbuddy/Pricing", draft)

    def test_review_and_unavailable_never_generate_publishable_draft(self):
        self_settings = self.settings
        for index, expected_status in enumerate(("review", "unavailable"), 1):
            with self.subTest(status=expected_status):
                database = self.root / f"guard-{index}.sqlite3"
                initialize_knowledge_schema(database)

                class Client:
                    def search(self, query):
                        return KnowledgeBaseClient(self_settings)._validate_lookup(
                            KnowledgeBaseTests.result(
                                status=expected_status, can_answer=False
                            )
                        )

                    def generate_draft(self, query, lookup):
                        raise AssertionError("insufficient evidence must not generate")

                service = KnowledgeAnswerService(self.settings, database, client=Client())
                service.process_question(
                    guild_id="1", guild_name="WorkBuddy", channel_id="2", feed_id="feed-1",
                    feed_create_time="123", title="Linux", body="支持 Linux 吗", author_id="u1",
                )
                with sqlite3.connect(str(database)) as connection:
                    row = connection.execute(
                        "SELECT knowledge_status, can_answer, draft, generation_status FROM knowledge_answer_drafts"
                    ).fetchone()
                self.assertEqual(row, (expected_status, 0, "", "not_allowed"))

    def test_lookup_is_saved_before_draft_generation(self):
        database = self.root / "guard-generating.sqlite3"
        settings = self.settings

        class Client:
            def search(self, query):
                return KnowledgeBaseClient(settings)._validate_lookup(
                    KnowledgeBaseTests.result()
                )

            def generate_draft(self, query, lookup):
                with sqlite3.connect(str(database)) as connection:
                    row = connection.execute(
                        "SELECT knowledge_status, can_answer, generation_status "
                        "FROM knowledge_answer_drafts"
                    ).fetchone()
                self.assertEqual(row, ("ready", 1, "generating"))
                return "可以购买加量包。\n\n官方来源：https://www.workbuddy.cn/docs/workbuddy/Pricing"

        client = Client()
        client.assertEqual = self.assertEqual
        service = KnowledgeAnswerService(settings, database, client=client)
        service.process_question(
            guild_id="1",
            guild_name="WorkBuddy",
            channel_id="2",
            feed_id="feed-generating",
            feed_create_time="123",
            title="积分",
            body="积分怎么充值",
            author_id="u1",
        )

        with sqlite3.connect(str(database)) as connection:
            row = connection.execute(
                "SELECT generation_status, draft FROM knowledge_answer_drafts"
            ).fetchone()
        self.assertEqual(row[0], "completed")
        self.assertIn("官方来源", row[1])


if __name__ == "__main__":
    unittest.main()
