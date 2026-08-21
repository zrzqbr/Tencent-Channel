import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class TencentCliError(RuntimeError):
    pass


class TencentCliClient:
    """腾讯官方频道 CLI 的无 shell 封装，避免参数注入和凭据回显。"""

    def __init__(
        self,
        executable: str = "tencent-channel-cli",
        timeout_seconds: int = 30,
        min_interval_seconds: float = 0.4,
        credential_home: Optional[str] = None,
        rate_limit_retry_seconds: float = 5.0,
    ) -> None:
        resolved = shutil.which(executable)
        if not resolved:
            raise TencentCliError(f"找不到 {executable}，请先安装腾讯频道 CLI")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.rate_limit_retry_seconds = max(0.0, float(rate_limit_retry_seconds))
        resolved_home = str(
            credential_home or os.environ.get("QQ_GUARD_TENCENT_HOME", "")
        ).strip()
        if not resolved_home and Path("/srv/tencent-channel/home").is_dir():
            resolved_home = "/srv/tencent-channel/home"
        self.environment = os.environ.copy()
        if resolved_home:
            self.environment["HOME"] = resolved_home
        self._last_call = 0.0
        self._last_rate_limit_retry = 0.0
        self._lock = threading.Lock()

    def list_channel_feeds(self, guild_id: str, channel_id: str, count: int = 20) -> List[Dict[str, Any]]:
        """Read up to ``count`` feeds, following the official opaque page cursor.

        Tencent currently returns fewer items than requested on the first page for
        some boards. Treating that first page as the complete result silently
        misses older/newly displaced posts, so pagination must follow
        ``feed_attach_info`` while ``has_more`` is true.
        """
        target_count = max(2, min(int(count), 100))
        safe_guild = self._digits(guild_id, "guild_id")
        safe_channel = self._digits(channel_id, "channel_id")
        feeds: List[Dict[str, Any]] = []
        seen_ids = set()
        cursor = ""
        seen_cursors = set()
        while len(feeds) < target_count:
            arguments = [
                "feed",
                "get-channel-timeline-feeds",
                "--guild-id",
                safe_guild,
                "--channel-id",
                safe_channel,
                "--count",
                str(target_count - len(feeds)),
            ]
            if cursor:
                arguments.extend(["--feed-attach-info", cursor])
            arguments.append("--json")
            payload = self._run(arguments, retries=5)
            data = payload.get("data") or {}
            page = list(data.get("feeds") or [])
            for feed in page:
                feed_id = str(feed.get("feed_id") or "")
                if feed_id and feed_id in seen_ids:
                    continue
                if feed_id:
                    seen_ids.add(feed_id)
                feeds.append(dict(feed))
                if len(feeds) >= target_count:
                    break
            next_cursor = str(data.get("feed_attach_info") or "")
            if not data.get("has_more") or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return feeds

    def list_channel_feeds_incremental(
        self,
        guild_id: str,
        channel_id: str,
        count: int = 20,
        known_feed_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Read new timeline pages until the persisted watermark is encountered."""
        target_count = max(2, min(int(count), 100))
        safe_guild = self._digits(guild_id, "guild_id")
        safe_channel = self._digits(channel_id, "channel_id")
        known = {str(value) for value in (known_feed_ids or ()) if str(value)}
        feeds: List[Dict[str, Any]] = []
        seen_ids = set()
        seen_cursors = set()
        cursor = ""
        while len(feeds) < target_count:
            arguments = [
                "feed",
                "get-channel-timeline-feeds",
                "--guild-id",
                safe_guild,
                "--channel-id",
                safe_channel,
                "--count",
                str(target_count - len(feeds)),
            ]
            if cursor:
                arguments.extend(["--feed-attach-info", cursor])
            arguments.append("--json")
            payload = self._run(arguments, retries=5)
            data = payload.get("data") or {}
            page = list(data.get("feeds") or [])
            page_reached_watermark = False
            for feed in page:
                feed_id = str(feed.get("feed_id") or "")
                if feed_id and feed_id in seen_ids:
                    continue
                if feed_id:
                    seen_ids.add(feed_id)
                    if feed_id in known:
                        page_reached_watermark = True
                feeds.append(dict(feed))
                if len(feeds) >= target_count:
                    break
            next_cursor = str(data.get("feed_attach_info") or "")
            if (
                page_reached_watermark
                or not data.get("has_more")
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return feeds

    def list_guild_feeds_incremental(
        self,
        guild_id: str,
        count: int = 100,
        known_feed_ids: Optional[Sequence[str]] = None,
        *,
        full_sync: bool = False,
    ) -> List[Dict[str, Any]]:
        """Read a guild-wide timeline with fewer official API calls.

        The guild timeline includes the source channel for every feed, so one
        paginated request replaces one request per configured board. A manual
        full sync follows older pages; scheduled scans stop at a known feed.
        """
        target_count = 1000 if full_sync else max(100, min(int(count), 500))
        safe_guild = self._digits(guild_id, "guild_id")
        known = {str(value) for value in (known_feed_ids or ()) if str(value)}
        feeds: List[Dict[str, Any]] = []
        seen_ids = set()
        seen_cursors = set()
        cursor = ""
        while len(feeds) < target_count:
            page_count = min(100, target_count - len(feeds))
            arguments = [
                "feed",
                "get-guild-feeds",
                "--guild-id",
                safe_guild,
                "--get-type",
                "2",
                "--count",
                str(page_count),
            ]
            if cursor:
                arguments.extend(["--feed-attach-info", cursor])
            arguments.append("--json")
            payload = self._run(arguments, retries=0)
            data = payload.get("data") or {}
            page = list(data.get("feeds") or [])
            page_reached_watermark = False
            for feed in page:
                feed_id = str(feed.get("feed_id") or "")
                if feed_id and feed_id in seen_ids:
                    continue
                if feed_id:
                    seen_ids.add(feed_id)
                    if feed_id in known:
                        page_reached_watermark = True
                feeds.append(dict(feed))
                if len(feeds) >= target_count:
                    break
            next_cursor = str(data.get("feed_attach_info") or "")
            if (
                (page_reached_watermark and not full_sync)
                or not data.get("has_more")
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return feeds

    def version(self) -> str:
        payload = self._run(["version", "--json"])
        return str((payload.get("data") or {}).get("version") or "")

    def doctor(self) -> Dict[str, Any]:
        return self._run(["doctor", "--json"])

    def login_status(self) -> Dict[str, Any]:
        return self._run(["login", "status", "--json"])

    def capability_index(self) -> List[Dict[str, Any]]:
        payload = self._run_json(["schema", "--json"], require_success=False)
        if not isinstance(payload, list):
            raise TencentCliError("腾讯官方 CLI 未返回能力清单")
        return [dict(item) for item in payload if isinstance(item, dict)]

    def capability_schema(self, domain: str, action: str) -> Dict[str, Any]:
        path = self._capability_path(domain, action)
        payload = self._run_json(["schema", path, "--json"], require_success=False)
        if not isinstance(payload, dict) or not payload.get("command"):
            raise TencentCliError("腾讯官方 CLI 未返回能力参数定义")
        return dict(payload)

    def execute_capability(
        self,
        domain: str,
        action: str,
        parameters: Dict[str, Any],
        *,
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Execute an official schema-discovered command without invoking a shell."""
        path = self._capability_path(domain, action)
        schema = self.capability_schema(domain, action)
        if str(schema.get("command")) != path:
            raise TencentCliError("官方能力定义与请求不一致")
        arguments = [domain, action]
        if dry_run:
            arguments.append("--dry-run")
        if confirmed:
            arguments.append("--yes")
        arguments.append("--json")
        return self._run(arguments, retries=1, stdin_payload=parameters)

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
            ],
            retries=5,
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

    def move_feed(
        self,
        guild_id: str,
        original_channel_id: str,
        target_channel_id: str,
        feed_id: str,
    ) -> Dict[str, Any]:
        return self._run(
            [
                "feed",
                "move-feed",
                "--guild-id",
                self._digits(guild_id, "guild_id"),
                "--original-channel-id",
                self._digits(original_channel_id, "original_channel_id"),
                "--channel-id",
                self._digits(target_channel_id, "channel_id"),
                "--feed-id",
                self._feed_id(feed_id),
                "--yes",
                "--json",
            ],
            retries=3,
        )

    def alter_feed(
        self,
        guild_id: str,
        channel_id: str,
        feed_id: str,
        create_time: str,
        feed_type: int,
        title: str,
        content: str,
        *,
        markdown: bool = False,
    ) -> Dict[str, Any]:
        parameters: Dict[str, Any] = {
            "feed_id": self._feed_id(feed_id),
            "guild_id": self._digits(guild_id, "guild_id"),
            "channel_id": self._digits(channel_id, "channel_id"),
            "create_time": self._timestamp(create_time),
            "feed_type": 2 if int(feed_type or 1) == 2 else 1,
            "title": str(title)[:500],
        }
        parameters["markdown_content" if markdown else "content"] = str(content)[:50000]
        return self.execute_capability(
            "feed",
            "alter-feed",
            parameters,
            confirmed=True,
        )

    def _run(
        self,
        arguments: Sequence[str],
        retries: int = 0,
        stdin_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._run_json(
            arguments,
            retries=retries,
            stdin_payload=stdin_payload,
            require_success=True,
        )
        if not isinstance(payload, dict):
            raise TencentCliError("腾讯频道 CLI 返回格式无效")
        return payload

    def _run_json(
        self,
        arguments: Sequence[str],
        retries: int = 0,
        stdin_payload: Optional[Dict[str, Any]] = None,
        require_success: bool = True,
    ) -> Any:
        rate_limit_retried = False
        timeout_retried = False
        for attempt in range(max(0, int(retries)) + 1):
            self._throttle()
            try:
                completed = subprocess.run(
                    [self.executable, *arguments],
                    capture_output=True,
                    text=True,
                    input=(
                        json.dumps(stdin_payload, ensure_ascii=False, separators=(",", ":"))
                        if stdin_payload is not None
                        else None
                    ),
                    timeout=self.timeout_seconds,
                    check=False,
                    env=self.environment,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt < retries and not timeout_retried:
                    timeout_retried = True
                    time.sleep(1.0)
                    continue
                raise TencentCliError("腾讯频道 CLI 请求超时") from exc
            raw = (completed.stdout or "").strip()
            try:
                object_start = raw.find("{")
                array_start = raw.find("[")
                candidates = [value for value in (object_start, array_start) if value >= 0]
                if not candidates:
                    raise ValueError("missing JSON payload")
                start = min(candidates)
                payload = json.loads(raw[start:])
            except (ValueError, json.JSONDecodeError) as exc:
                message = (completed.stderr or raw or "腾讯频道 CLI 未返回 JSON").strip()
                raise TencentCliError(message[:500]) from exc
            if completed.returncode == 0 and (
                not require_success
                or (isinstance(payload, dict) and payload.get("success", False))
            ):
                return payload
            error = payload.get("error") if isinstance(payload, dict) else {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            message = message or "腾讯频道 CLI 调用失败"
            retry_cooldown_elapsed = (
                time.monotonic() - self._last_rate_limit_retry >= 300.0
            )
            if (
                self._is_rate_limit(message)
                and attempt < retries
                and not rate_limit_retried
                and retry_cooldown_elapsed
            ):
                rate_limit_retried = True
                self._last_rate_limit_retry = time.monotonic()
                time.sleep(self.rate_limit_retry_seconds)
                continue
            raise TencentCliError(message)
        raise TencentCliError("腾讯频道 CLI 调用失败")

    @staticmethod
    def _capability_path(domain: str, action: str) -> str:
        safe_domain = str(domain).strip()
        safe_action = str(action).strip()
        if safe_domain not in {"feed", "manage"}:
            raise TencentCliError("不支持的官方能力域")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,79}", safe_action):
            raise TencentCliError("官方能力名称格式无效")
        return f"{safe_domain}.{safe_action}"

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval_seconds - (now - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()

    @staticmethod
    def _is_rate_limit(message: str) -> bool:
        value = str(message).casefold()
        return "retcode=153" in value or "频率上限" in value or "rate limit" in value

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
