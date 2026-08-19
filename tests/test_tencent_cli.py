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


if __name__ == "__main__":
    unittest.main()
