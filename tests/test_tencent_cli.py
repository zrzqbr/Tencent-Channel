import json
import subprocess
import unittest
from unittest.mock import patch

from qq_guard.tencent_cli import TencentCliClient


class TencentCliClientTests(unittest.TestCase):
    def test_uses_explicit_shared_credential_home(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"success": True, "data": {"feeds": []}}),
            stderr="",
        )
        with patch("qq_guard.tencent_cli.shutil.which", return_value="/usr/bin/tencent-channel-cli"):
            client = TencentCliClient(
                credential_home="/srv/tencent-channel/home",
                min_interval_seconds=0,
            )
        with patch("qq_guard.tencent_cli.subprocess.run", return_value=completed) as run:
            client.list_channel_feeds("123", "456")
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/srv/tencent-channel/home")

    def test_channel_feed_listing_follows_official_page_cursor(self):
        pages = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "success": True,
                        "data": {
                            "feeds": [{"feed_id": "first"}],
                            "feed_attach_info": "opaque-page-2",
                            "has_more": True,
                        },
                    }
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "success": True,
                        "data": {"feeds": [{"feed_id": "second"}], "has_more": False},
                    }
                ),
                stderr="",
            ),
        ]
        with patch("qq_guard.tencent_cli.shutil.which", return_value="/usr/bin/tencent-channel-cli"):
            client = TencentCliClient(min_interval_seconds=0)
        with patch("qq_guard.tencent_cli.subprocess.run", side_effect=pages) as run:
            feeds = client.list_channel_feeds("123", "456", count=2)
        self.assertEqual([item["feed_id"] for item in feeds], ["first", "second"])
        self.assertIn("--feed-attach-info", run.call_args.args[0])
        self.assertIn("opaque-page-2", run.call_args.args[0])

    def test_capability_index_accepts_json_array(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"domain": "feed", "commands": []}]),
            stderr="",
        )
        with patch("qq_guard.tencent_cli.shutil.which", return_value="/usr/bin/tencent-channel-cli"):
            client = TencentCliClient(min_interval_seconds=0)
        with patch("qq_guard.tencent_cli.subprocess.run", return_value=completed):
            self.assertEqual(client.capability_index()[0]["domain"], "feed")

    def test_incremental_listing_stops_when_first_page_reaches_watermark(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "data": {
                        "feeds": [{"feed_id": "new"}, {"feed_id": "known"}],
                        "feed_attach_info": "unused-page-2",
                        "has_more": True,
                    },
                }
            ),
            stderr="",
        )
        with patch("qq_guard.tencent_cli.shutil.which", return_value="/usr/bin/tencent-channel-cli"):
            client = TencentCliClient(min_interval_seconds=0)
        with patch("qq_guard.tencent_cli.subprocess.run", return_value=completed) as run:
            feeds = client.list_channel_feeds_incremental(
                "123", "456", count=20, known_feed_ids=["known"]
            )
        self.assertEqual([item["feed_id"] for item in feeds], ["new", "known"])
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
