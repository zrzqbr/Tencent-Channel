import hashlib
import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .classifier import ContentClassifier
from .config import GuardConfig, TencentChannelSettings
from .models import IncomingContent, ItemKind, PolicyReason, Section
from .moderation import ModerationEngine


class TencentFeedApi(Protocol):
    def list_channel_feeds(self, guild_id: str, channel_id: str, count: int = 20) -> List[Dict[str, Any]]:
        ...

    def get_feed_detail(self, guild_id: str, channel_id: str, feed_id: str) -> Dict[str, Any]:
        ...

    def delete_feed(
        self,
        guild_id: str,
        channel_id: str,
        feed_id: str,
        create_time: str,
        live: bool,
    ) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class DuplicateFinding:
    guild_id: str
    guild_name: str
    channel_id: str
    section: str
    newer_feed_id: str
    older_feed_id: str
    delete_status: str
    error: Optional[str] = None


@dataclass(frozen=True)
class ModerationFinding:
    guild_id: str
    guild_name: str
    channel_id: str
    feed_id: str
    title: str
    section: str
    action: str
    risk_level: str
    risk_score: int
    policy_version: str
    reasons: Tuple[PolicyReason, ...]


@dataclass(frozen=True)
class ScanReport:
    scanned_feeds: int
    duplicate_findings: Tuple[DuplicateFinding, ...]
    weekly_missing_topic: int
    started_at: str
    finished_at: str
    delete_mode: str
    guilds: Tuple[str, ...]
    classification_counts: Mapping[str, int]
    moderation_findings: Tuple[ModerationFinding, ...]

    def public_summary(self) -> Dict[str, Any]:
        return {
            "scanned_feeds": self.scanned_feeds,
            "duplicates": len(self.duplicate_findings),
            "delete_results": [finding.delete_status for finding in self.duplicate_findings],
            "duplicate_findings": [
                {
                    "guild": finding.guild_name or finding.guild_id,
                    "channel_id": finding.channel_id,
                    "section": finding.section,
                    "newer_feed_id": finding.newer_feed_id,
                    "older_feed_id": finding.older_feed_id,
                    "reason_code": "exact_consecutive_duplicate",
                    "reason": "同一作者在同一频道、同一栏目内连续发布完全相同内容",
                    "delete_status": finding.delete_status,
                    "error": finding.error,
                }
                for finding in self.duplicate_findings
            ],
            "weekly_missing_topic": self.weekly_missing_topic,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "delete_mode": self.delete_mode,
            "guilds": list(self.guilds),
            "classification_counts": dict(self.classification_counts),
            "review_required": sum(
                finding.action == "review" for finding in self.moderation_findings
            ),
            "delete_candidates": sum(
                finding.action == "delete_candidate" for finding in self.moderation_findings
            ),
            "moderation_findings": [
                {
                    "guild": finding.guild_name or finding.guild_id,
                    "channel_id": finding.channel_id,
                    "feed_id": finding.feed_id,
                    "title": finding.title,
                    "section": finding.section,
                    "action": finding.action,
                    "risk_level": finding.risk_level,
                    "risk_score": finding.risk_score,
                    "policy_version": finding.policy_version,
                    "reasons": [
                        {
                            "code": reason.code,
                            "category": reason.category,
                            "severity": reason.severity,
                            "message": reason.message,
                            "evidence": reason.evidence,
                            "score": reason.score,
                            "auto_delete_eligible": reason.auto_delete_eligible,
                        }
                        for reason in finding.reasons
                    ],
                }
                for finding in self.moderation_findings
            ],
        }


class TencentChannelMonitor:
    def __init__(self, config: GuardConfig, api: TencentFeedApi) -> None:
        settings = config.tencent_channels or (
            (config.tencent_channel,) if config.tencent_channel is not None else ()
        )
        if not settings:
            raise ValueError("config.json 中尚未配置 tencent_channels")
        self.config = config
        self.settings: Tuple[TencentChannelSettings, ...] = tuple(settings)
        self.api = api
        self.classifier = ContentClassifier(config)
        self.moderation_engine = ModerationEngine(config)
        self.database_path = Path(config.database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_audit()

    def scan_once(self) -> ScanReport:
        started_at = _utc_now()
        total = 0
        weekly_missing = 0
        findings: List[DuplicateFinding] = []
        moderation_findings: List[ModerationFinding] = []
        classification_counts: Dict[str, int] = {}
        guild_names: List[str] = []

        for settings in self.settings:
            guild_label = settings.name or settings.guild_id
            guild_names.append(guild_label)

            for section, channel_id in settings.channels.items():
                feeds = self._list_feeds(settings, channel_id)
                total += len(feeds)
                classification_counts[f"{guild_label}:{section.value}"] = len(feeds)
                details: Dict[str, Dict[str, Any]] = {}
                for feed in feeds:
                    detail = self._detail(settings, channel_id, feed, details)
                    if section is Section.WEEKLY_QUESTION:
                        if not list(detail.get("topic_names") or []):
                            weekly_missing += 1
                    classification = self._classify_feed(settings, channel_id, feed, detail)
                    moderation_findings.extend(
                        self._moderation_finding(
                            settings, channel_id, feed, detail, classification
                        )
                    )
                findings.extend(
                    self._find_duplicates(settings, channel_id, section, feeds, details)
                )

            for _channel_name, channel_id in settings.auto_classify_channels.items():
                feeds = self._list_feeds(settings, channel_id)
                total += len(feeds)
                details: Dict[str, Dict[str, Any]] = {}
                by_section: Dict[Section, List[Dict[str, Any]]] = {}
                for feed in feeds:
                    detail = self._detail(settings, channel_id, feed, details)
                    result = self._classify_feed(settings, channel_id, feed, detail)
                    section = result.section
                    by_section.setdefault(section, []).append(feed)
                    key = f"{guild_label}:{section.value}"
                    classification_counts[key] = classification_counts.get(key, 0) + 1
                    if "missing_weekly_hashtag" in result.validation_issues:
                        weekly_missing += 1
                    moderation_findings.extend(
                        self._moderation_finding(
                            settings, channel_id, feed, detail, result
                        )
                    )
                for section, section_feeds in by_section.items():
                    findings.extend(
                        self._find_duplicates(
                            settings,
                            channel_id,
                            section,
                            section_feeds,
                            details,
                        )
                    )

        report = ScanReport(
            scanned_feeds=total,
            duplicate_findings=tuple(findings),
            weekly_missing_topic=weekly_missing,
            started_at=started_at,
            finished_at=_utc_now(),
            delete_mode=self.config.delete_mode,
            guilds=tuple(guild_names),
            classification_counts=classification_counts,
            moderation_findings=tuple(moderation_findings),
        )
        self._record_scan(report)
        return report

    def run_forever(self) -> None:
        while True:
            report = self.scan_once()
            print(json.dumps(report.public_summary(), ensure_ascii=False), flush=True)
            time.sleep(min(settings.poll_interval_seconds for settings in self.settings))

    def _list_feeds(
        self, settings: TencentChannelSettings, channel_id: str
    ) -> List[Dict[str, Any]]:
        feeds = self.api.list_channel_feeds(
            settings.guild_id,
            channel_id,
            settings.scan_count,
        )
        return sorted(
            feeds,
            key=lambda feed: int(feed.get("create_time_raw") or 0),
            reverse=True,
        )

    def _detail(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        cache: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        feed_id = str(feed.get("feed_id") or "")
        if feed_id not in cache:
            cache[feed_id] = self.api.get_feed_detail(settings.guild_id, channel_id, feed_id)
        return cache[feed_id]

    def _classify_feed(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        detail: Mapping[str, Any],
    ):
        return self.classifier.classify(
            self._incoming_content(settings, channel_id, feed, detail)
        )

    def _incoming_content(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        detail: Mapping[str, Any],
    ) -> IncomingContent:
        topics = " ".join(f"#{topic}" for topic in (detail.get("topic_names") or []))
        body = "\n".join(
            value for value in (str(detail.get("content") or ""), topics) if value
        )
        media = tuple(
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in list(detail.get("images") or []) + list(detail.get("videos") or [])
        )
        return IncomingContent(
            platform_item_id=str(feed.get("feed_id") or ""),
            kind=ItemKind.FORUM_THREAD,
            guild_id=settings.guild_id,
            channel_id=channel_id,
            author_id=str(feed.get("author_id") or ""),
            title=str(detail.get("title") or feed.get("title") or ""),
            body=body,
            media_urls=media,
            created_at=str(feed.get("create_time_raw") or ""),
        )

    def _moderation_finding(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        detail: Mapping[str, Any],
        classification,
    ) -> List[ModerationFinding]:
        item = self._incoming_content(settings, channel_id, feed, detail)
        assessment = self.moderation_engine.evaluate(item, classification)
        if assessment.action.value == "allow" and not assessment.reasons:
            return []
        finding = ModerationFinding(
            guild_id=settings.guild_id,
            guild_name=settings.name,
            channel_id=channel_id,
            feed_id=item.platform_item_id,
            title=item.title,
            section=classification.section.value,
            action=assessment.action.value,
            risk_level=assessment.risk_level.value,
            risk_score=assessment.risk_score,
            policy_version=assessment.policy_version,
            reasons=assessment.reasons,
        )
        self._record_moderation(finding)
        return [finding]

    def _find_duplicates(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        section: Section,
        feeds: Sequence[Mapping[str, Any]],
        details: Dict[str, Dict[str, Any]],
    ) -> List[DuplicateFinding]:
        findings: List[DuplicateFinding] = []
        for index in range(len(feeds) - 1):
            newer, older = feeds[index], feeds[index + 1]
            if str(newer.get("author_id")) != str(older.get("author_id")):
                continue
            if self._summary_signature(newer) != self._summary_signature(older):
                continue
            newer_detail = self._detail(settings, channel_id, newer, details)
            older_detail = self._detail(settings, channel_id, older, details)
            if self._full_fingerprint(newer_detail) != self._full_fingerprint(older_detail):
                continue
            newer_id = str(newer.get("feed_id") or "")
            older_id = str(older.get("feed_id") or "")
            if self._already_processed(settings.guild_id, newer_id):
                continue

            # Test mode is deliberately side-effect free: it must never call
            # Tencent's deletion endpoint, including the CLI's dry-run form.
            if not self.config.auto_delete_duplicates or self.config.delete_mode != "live":
                status = "detected_only"
                error = None
            else:
                try:
                    self.api.delete_feed(
                        settings.guild_id,
                        channel_id,
                        newer_id,
                        str(newer.get("create_time_raw") or ""),
                        live=True,
                    )
                    status = "deleted"
                    error = None
                except Exception as exc:
                    status = "failed"
                    error = f"{type(exc).__name__}: {exc}"
            finding = DuplicateFinding(
                guild_id=settings.guild_id,
                guild_name=settings.name,
                channel_id=channel_id,
                section=section.value,
                newer_feed_id=newer_id,
                older_feed_id=older_id,
                delete_status=status,
                error=error,
            )
            findings.append(finding)
            self._record_finding(finding)
        return findings

    @staticmethod
    def _summary_signature(feed: Mapping[str, Any]) -> Tuple[str, str]:
        return (_normalize(feed.get("title")), _normalize(feed.get("content_snippet")))

    @staticmethod
    def _full_fingerprint(feed: Mapping[str, Any]) -> str:
        canonical = {
            "title": _normalize(feed.get("title")),
            "content": _normalize(feed.get("content")),
            "feed_type": feed.get("feed_type"),
            "topic_names": sorted(_normalize(value) for value in (feed.get("topic_names") or [])),
            "images": _stable_media(feed.get("images") or []),
            "videos": _stable_media(feed.get("videos") or []),
        }
        serialized = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _initialize_audit(self) -> None:
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tencent_scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    scanned_feeds INTEGER NOT NULL,
                    duplicates INTEGER NOT NULL,
                    weekly_missing_topic INTEGER NOT NULL,
                    delete_mode TEXT NOT NULL,
                    guilds_json TEXT NOT NULL DEFAULT '[]',
                    classification_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS tencent_duplicate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    newer_feed_id TEXT NOT NULL UNIQUE,
                    older_feed_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL DEFAULT '',
                    guild_name TEXT NOT NULL DEFAULT '',
                    channel_id TEXT NOT NULL DEFAULT '',
                    section TEXT NOT NULL,
                    delete_status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tencent_moderation_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    feed_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL,
                    action TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, feed_id, policy_version)
                );
                """
            )
            self._ensure_column(connection, "tencent_scan_runs", "guilds_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(
                connection,
                "tencent_scan_runs",
                "classification_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "tencent_duplicate_actions",
                "guild_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "tencent_duplicate_actions",
                "guild_name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "tencent_duplicate_actions",
                "channel_id",
                "TEXT NOT NULL DEFAULT ''",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _already_processed(self, guild_id: str, newer_feed_id: str) -> bool:
        with sqlite3.connect(str(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT delete_status
                FROM tencent_duplicate_actions
                WHERE newer_feed_id = ? AND guild_id IN (?, '')
                """,
                (newer_feed_id, guild_id),
            ).fetchone()
        return bool(row and row[0] == "deleted")

    def _record_finding(self, finding: DuplicateFinding) -> None:
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO tencent_duplicate_actions
                (newer_feed_id, older_feed_id, guild_id, guild_name, channel_id,
                 section, delete_status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.newer_feed_id,
                    finding.older_feed_id,
                    finding.guild_id,
                    finding.guild_name,
                    finding.channel_id,
                    finding.section,
                    finding.delete_status,
                    finding.error,
                    _utc_now(),
                ),
            )

    def _record_moderation(self, finding: ModerationFinding) -> None:
        reasons = [
            {
                "code": reason.code,
                "category": reason.category,
                "severity": reason.severity,
                "message": reason.message,
                "evidence": reason.evidence,
                "score": reason.score,
                "auto_delete_eligible": reason.auto_delete_eligible,
            }
            for reason in finding.reasons
        ]
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_moderation_findings
                (guild_id, guild_name, channel_id, feed_id, title, section, action,
                 risk_level, risk_score, policy_version, reasons_json, review_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(guild_id, feed_id, policy_version) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    channel_id = excluded.channel_id,
                    title = excluded.title,
                    section = excluded.section,
                    action = excluded.action,
                    risk_level = excluded.risk_level,
                    risk_score = excluded.risk_score,
                    reasons_json = excluded.reasons_json,
                    created_at = excluded.created_at
                """,
                (
                    finding.guild_id,
                    finding.guild_name,
                    finding.channel_id,
                    finding.feed_id,
                    finding.title,
                    finding.section,
                    finding.action,
                    finding.risk_level,
                    finding.risk_score,
                    finding.policy_version,
                    json.dumps(reasons, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def _record_scan(self, report: ScanReport) -> None:
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_scan_runs
                (started_at, finished_at, scanned_feeds, duplicates, weekly_missing_topic,
                 delete_mode, guilds_json, classification_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.started_at,
                    report.finished_at,
                    report.scanned_feeds,
                    len(report.duplicate_findings),
                    report.weekly_missing_topic,
                    report.delete_mode,
                    json.dumps(report.guilds, ensure_ascii=False),
                    json.dumps(report.classification_counts, ensure_ascii=False, sort_keys=True),
                ),
            )


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(char for char in text if char not in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff")
    return " ".join(text.split())


def _stable_media(media: Sequence[Any]) -> List[Any]:
    stable: List[Any] = []
    for value in media:
        if isinstance(value, dict):
            stable.append(
                {
                    key: value[key]
                    for key in sorted(value)
                    if key not in {"download_url", "expire_time", "temporary_url"}
                }
            )
        else:
            stable.append(value)
    return stable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
