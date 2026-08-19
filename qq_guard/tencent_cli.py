import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence


class TencentCliError(RuntimeError):
    pass


class TencentCliClient:
    """腾讯官方频道 CLI 的无 shell 封装，避免参数注入和凭据回显。"""

    def __init__(self, executable: str = "tencent-channel-cli", timeout_seconds: int = 30) -> None:
        resolved = shutil.which(executable)
        if not resolved:
            raise TencentCliError(f"找不到 {executable}，请先安装腾讯频道 CLI")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def list_channel_feeds(self, guild_id: str, channel_id: str, count: int = 20) -> List[Dict[str, Any]]:
        payload = self._run(
            [
                "feed",
                "get-channel-timeline-feeds",
                "--guild-id",
                self._digits(guild_id, "guild_id"),
                "--channel-id",
                self._digits(channel_id, "channel_id"),
                "--count",
                str(max(2, min(int(count), 100))),
                "--json",
            ]
        )
        return list((payload.get("data") or {}).get("feeds") or [])

    def get_feed_detail(self, guild_id: str, channel_id: str, feed_id: str) -> Dict[str, Any]:
        payload = self._run(
            [
                "feed",
                "get-feed-detail",
                "--feed-id",
                self._feed_id(feed_id),
                "--guild-id",
                self._digits(guild_id, "guild_id"),
                "--channel-id",
                self._digits(channel_id, "channel_id"),
                "--json",
            ]
        )
        return dict((payload.get("data") or {}).get("feed") or {})

    def delete_feed(
        self,
        guild_id: str,
        channel_id: str,
        feed_id: str,
        create_time: str,
        live: bool,
    ) -> Dict[str, Any]:
        arguments = [
            "feed",
            "del-feed",
            "--feed-id",
            self._feed_id(feed_id),
            "--guild-id",
            self._digits(guild_id, "guild_id"),
            "--channel-id",
            self._digits(channel_id, "channel_id"),
            "--create-time",
            self._timestamp(create_time),
        ]
        arguments.append("--yes" if live else "--dry-run")
        arguments.append("--json")
        return self._run(arguments)

    def _run(self, arguments: Sequence[str]) -> Dict[str, Any]:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TencentCliError("腾讯频道 CLI 请求超时") from exc
        raw = (completed.stdout or "").strip()
        try:
            start = raw.index("{")
            payload = json.loads(raw[start:])
        except (ValueError, json.JSONDecodeError) as exc:
            message = (completed.stderr or raw or "腾讯频道 CLI 未返回 JSON").strip()
            raise TencentCliError(message[:500]) from exc
        if completed.returncode != 0 or not payload.get("success", False):
            error = payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise TencentCliError(message or "腾讯频道 CLI 调用失败")
        return payload

    @staticmethod
    def _digits(value: str, name: str) -> str:
        text = str(value).strip()
        if not text.isdigit():
            raise TencentCliError(f"{name} 格式无效")
        return text

    @staticmethod
    def _feed_id(value: str) -> str:
        text = str(value).strip()
        if not text or not all(char.isalnum() or char in "_=-" for char in text):
            raise TencentCliError("feed_id 格式无效")
        return text

    @staticmethod
    def _timestamp(value: str) -> str:
        text = str(value).strip()
        if not text.isdigit():
            raise TencentCliError("create_time 格式无效")
        return text

