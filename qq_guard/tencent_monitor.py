import hashlib
import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .ai_review import AIReviewClient, AIReviewUnavailable, fuse_ai_review
from .classifier import ContentClassifier
from .config import GuardConfig, TencentChannelSettings
from .models import IncomingContent, ItemKind, PolicyReason, Section
from .moderation import ModerationEngine
from .scan_control import ScanLock


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
    author_id: str = ""
    body: str = ""
    media_urls: Tuple[str, ...] = ()
    source_created_at: str = ""
    classification_json: str = "{}"
    analysis_source: str = "rules"
    ai_status: str = "not_requested"
    ai_model: str = ""
    ai_confidence: Optional[float] = None
    ai_analysis_json: str = "{}"
    ai_error: str = ""


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
    ai_reviewed: int = 0
    ai_fallbacks: int = 0
    ai_model: str = ""
    new_feeds: int = 0
    updated_feeds: int = 0
    cached_feeds: int = 0

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
            "ai_reviewed": self.ai_reviewed,
            "ai_fallbacks": self.ai_fallbacks,
            "ai_model": self.ai_model,
            "new_feeds": self.new_feeds,
            "updated_feeds": self.updated_feeds,
            "cached_feeds": self.cached_feeds,
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
                    "analysis_source": finding.analysis_source,
                    "ai_status": finding.ai_status,
                    "ai_model": finding.ai_model,
                    "ai_confidence": finding.ai_confidence,
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
    def __init__(
        self,
        config: GuardConfig,
        api: TencentFeedApi,
        ai_client: Optional[AIReviewClient] = None,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> None:
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
        self.ai_client = ai_client or AIReviewClient(config.ai_review, self.database_path)
        self.progress_callback = progress_callback
        self._ai_reviewed = 0
        self._ai_fallbacks = 0
        self._new_feeds = 0
        self._updated_feeds = 0
        self._cached_feeds = 0
        self._initialize_audit()

    def scan_once(self, *, full_sync: bool = False) -> ScanReport:
        self._progress(5, "连接频道", "正在读取频道和栏目配置")
        started_at = _utc_now()
        total = 0
        weekly_missing = 0
        findings: List[DuplicateFinding] = []
        moderation_findings: List[ModerationFinding] = []
        classification_counts: Dict[str, int] = {}
        guild_names: List[str] = []
        self._ai_reviewed = 0
        self._ai_fallbacks = 0
        self._new_feeds = 0
        self._updated_feeds = 0
        self._cached_feeds = 0

        work_units = sum(
            len(settings.channels) + len(settings.auto_classify_channels)
            for settings in self.settings
        )
        completed_units = 0

        for settings in self.settings:
            guild_label = settings.name or settings.guild_id
            guild_names.append(guild_label)
            channel_jobs = [
                (section.display_name, channel_id)
                for section, channel_id in settings.channels.items()
            ] + [
                (channel_name, channel_id)
                for channel_name, channel_id in settings.auto_classify_channels.items()
            ]
            guild_feeds = self._list_guild_feeds(settings, full_sync=full_sync)
            if guild_feeds is None:
                feeds_by_channel = None
            else:
                total += len(guild_feeds)
                feeds_by_channel: Dict[str, List[Dict[str, Any]]] = {}
                for feed in guild_feeds:
                    channel_id = self._resolve_channel_id(settings, feed)
                    self._store_summary(settings, channel_id, feed)
                    feeds_by_channel.setdefault(channel_id, []).append(feed)

            for channel_name, channel_id in channel_jobs:
                self._progress(
                    10 + int(75 * completed_units / max(work_units, 1)),
                    "读取并分析内容",
                    f"正在处理 {guild_label} · {channel_name}",
                )
                if feeds_by_channel is None:
                    feeds = self._list_feeds(settings, channel_id)
                    total += len(feeds)
                else:
                    feeds = sorted(
                        feeds_by_channel.get(channel_id, []),
                        key=lambda feed: int(feed.get("create_time_raw") or 0),
                        reverse=True,
                    )[: settings.scan_count]
                channel_weekly, channel_duplicates, channel_findings = self._process_channel(
                    settings,
                    channel_id,
                    feeds,
                    guild_label,
                    classification_counts,
                )
                weekly_missing += channel_weekly
                findings.extend(channel_duplicates)
                moderation_findings.extend(channel_findings)
                completed_units += 1

        self._progress(88, "确定性去重", "正在汇总严格重复项与栏目判定")
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
            ai_reviewed=self._ai_reviewed,
            ai_fallbacks=self._ai_fallbacks,
            ai_model=self.config.ai_review.model if self.config.ai_review.enabled else "",
            new_feeds=self._new_feeds,
            updated_feeds=self._updated_feeds,
            cached_feeds=self._cached_feeds,
        )
        self._progress(95, "保存审核结果", "正在写入分类、风险、AI 状态与判定理由")
        self._record_scan(report)
        return report

    def run_forever(self) -> None:
        base_delay = min(settings.poll_interval_seconds for settings in self.settings)
        while True:
            delay = base_delay
            try:
                with ScanLock(self.database_path) as acquired:
                    if acquired:
                        report = self.scan_once()
                        print(json.dumps(report.public_summary(), ensure_ascii=False), flush=True)
                    else:
                        print(
                            json.dumps(
                                {"status": "skipped", "reason": "scan_already_running"},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    delay = max(base_delay, 1800)
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": "scan_error",
                            "error": str(exc)[:500],
                            "retry_after_seconds": delay,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            time.sleep(delay)

    def _progress(self, percent: int, phase: str, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(percent, phase, message)

    def _list_feeds(
        self, settings: TencentChannelSettings, channel_id: str
    ) -> List[Dict[str, Any]]:
        incremental = getattr(self.api, "list_channel_feeds_incremental", None)
        if callable(incremental):
            feeds = incremental(
                settings.guild_id,
                channel_id,
                settings.scan_count,
                self._known_feed_ids(settings.guild_id, channel_id),
            )
        else:
            feeds = self.api.list_channel_feeds(
                settings.guild_id,
                channel_id,
                settings.scan_count,
            )
        feeds = sorted(
            feeds,
            key=lambda feed: int(feed.get("create_time_raw") or 0),
            reverse=True,
        )
        for feed in feeds:
            self._store_summary(settings, channel_id, feed)
        return feeds

    def _list_guild_feeds(
        self,
        settings: TencentChannelSettings,
        *,
        full_sync: bool,
    ) -> Optional[List[Dict[str, Any]]]:
        incremental = getattr(self.api, "list_guild_feeds_incremental", None)
        if not callable(incremental):
            return None
        feeds = incremental(
            settings.guild_id,
            max(settings.scan_count, 100),
            () if full_sync else self._known_feed_ids(settings.guild_id, ""),
            full_sync=full_sync,
        )
        return sorted(
            feeds,
            key=lambda feed: int(feed.get("create_time_raw") or 0),
            reverse=True,
        )

    def _known_feed_ids(self, guild_id: str, channel_id: str) -> Tuple[str, ...]:
        with sqlite3.connect(str(self.database_path)) as connection:
            if channel_id:
                rows = connection.execute(
                    """
                    SELECT feed_id FROM tencent_feed_cache
                    WHERE guild_id = ? AND channel_id = ?
                    """,
                    (guild_id, channel_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT feed_id FROM tencent_feed_cache WHERE guild_id = ?",
                    (guild_id,),
                ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _resolve_channel_id(
        self,
        settings: TencentChannelSettings,
        feed: Mapping[str, Any],
    ) -> str:
        explicit_id = str(feed.get("channel_id") or "").strip()
        configured_ids = {
            *settings.channels.values(),
            *settings.auto_classify_channels.values(),
        }
        if explicit_id:
            return explicit_id

        source_name = _normalize_channel_name(feed.get("channel_name"))
        if not source_name:
            return ""

        names_by_id: Dict[str, set[str]] = {
            channel_id: set() for channel_id in configured_ids
        }
        for section, channel_id in settings.channels.items():
            names_by_id[channel_id].add(_normalize_channel_name(section.display_name))
        for channel_name, channel_id in settings.auto_classify_channels.items():
            names_by_id[channel_id].add(_normalize_channel_name(channel_name))
        for channel_id, policy in self.config.board_policies.items():
            if channel_id not in names_by_id:
                continue
            policy_name = str(policy.name or "")
            names_by_id[channel_id].add(_normalize_channel_name(policy_name))
            for separator in ("·", "|", "/", "-", "—", ":", "："):
                if separator in policy_name:
                    names_by_id[channel_id].add(
                        _normalize_channel_name(policy_name.rsplit(separator, 1)[-1])
                    )

        exact_matches = [
            channel_id
            for channel_id, names in names_by_id.items()
            if source_name in names
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]

        fuzzy_matches = [
            channel_id
            for channel_id, names in names_by_id.items()
            if any(
                len(name) >= 2 and (name in source_name or source_name in name)
                for name in names
            )
        ]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        return ""

    def _process_channel(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feeds: Sequence[Dict[str, Any]],
        guild_label: str,
        classification_counts: Dict[str, int],
    ) -> Tuple[int, List[DuplicateFinding], List[ModerationFinding]]:
        weekly_missing = 0
        moderation_findings: List[ModerationFinding] = []
        details: Dict[str, Dict[str, Any]] = {}
        by_section: Dict[Section, List[Dict[str, Any]]] = {}
        nearby_items: List[IncomingContent] = []
        for feed in feeds:
            detail = self._detail(settings, channel_id, feed, details)
            classification = self._classify_feed(settings, channel_id, feed, detail)
            classification, feed_findings = self._analyze_finding(
                settings,
                channel_id,
                feed,
                detail,
                classification,
                nearby_items[-3:],
            )
            nearby_items.append(self._incoming_content(settings, channel_id, feed, detail))
            moderation_findings.extend(feed_findings)
            by_section.setdefault(classification.section, []).append(feed)
            key = f"{guild_label}:{classification.section.value}"
            classification_counts[key] = classification_counts.get(key, 0) + 1
            if "missing_weekly_hashtag" in classification.validation_issues:
                weekly_missing += 1
        duplicate_findings: List[DuplicateFinding] = []
        for section, section_feeds in by_section.items():
            duplicate_findings.extend(
                self._find_duplicates(settings, channel_id, section, section_feeds, details)
            )
        return weekly_missing, duplicate_findings, moderation_findings

    def _detail(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        cache: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        feed_id = str(feed.get("feed_id") or "")
        if feed_id not in cache:
            version_key = self._feed_version(feed)
            cached, cache_state = self._cached_detail(
                settings.guild_id, feed_id, version_key
            )
            if cached is not None:
                self._cached_feeds += 1
                cache[feed_id] = cached
            else:
                if cache_state == "updated":
                    self._updated_feeds += 1
                else:
                    self._new_feeds += 1
                cache[feed_id] = self.api.get_feed_detail(
                    settings.guild_id, channel_id, feed_id
                )
                self._store_detail(
                    settings.guild_id,
                    channel_id,
                    feed_id,
                    version_key,
                    cache[feed_id],
                )
        return cache[feed_id]

    @staticmethod
    def _feed_version(feed: Mapping[str, Any]) -> str:
        canonical = {
            "create_time": feed.get("create_time_raw"),
            "update_time": feed.get("update_time_raw"),
            "title": feed.get("title"),
            "snippet": feed.get("content_snippet"),
            "author_id": feed.get("author_id"),
        }
        value = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _cached_detail(
        self,
        guild_id: str,
        feed_id: str,
        version_key: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        with sqlite3.connect(str(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT version_key, detail_json
                FROM tencent_feed_cache
                WHERE guild_id = ? AND feed_id = ?
                """,
                (guild_id, feed_id),
            ).fetchone()
        if not row:
            return None, "new"
        if not str(row[0]) or str(row[1]).strip() in {"", "{}", "null"}:
            return None, "new"
        if str(row[0]) != version_key:
            return None, "updated"
        try:
            return dict(json.loads(row[1])), "cached"
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "updated"

    def _store_summary(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
    ) -> None:
        feed_id = str(feed.get("feed_id") or "")
        if not feed_id:
            return
        channel_name = str(feed.get("channel_name") or "")
        if not channel_name:
            for section, configured_id in settings.channels.items():
                if configured_id == channel_id:
                    channel_name = section.display_name
                    break
        if not channel_name:
            channel_name = next(
                (
                    name
                    for name, configured_id in settings.auto_classify_channels.items()
                    if configured_id == channel_id
                ),
                "未命名栏目",
            )
        now = _utc_now()
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_feed_cache
                (guild_id, channel_id, feed_id, version_key, detail_json, fetched_at,
                 guild_name, channel_name, summary_json, summary_version_key,
                 first_seen_at, last_seen_at, deleted_at)
                VALUES (?, ?, ?, '', '{}', ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(guild_id, feed_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_name = CASE WHEN excluded.guild_name <> '' THEN excluded.guild_name ELSE tencent_feed_cache.guild_name END,
                    channel_name = CASE WHEN excluded.channel_name <> '' THEN excluded.channel_name ELSE tencent_feed_cache.channel_name END,
                    summary_json = excluded.summary_json,
                    summary_version_key = excluded.summary_version_key,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    settings.guild_id,
                    channel_id,
                    feed_id,
                    now,
                    settings.name or settings.guild_id,
                    channel_name,
                    json.dumps(dict(feed), ensure_ascii=False, sort_keys=True),
                    self._feed_version(feed),
                    now,
                    now,
                ),
            )

    def _store_detail(
        self,
        guild_id: str,
        channel_id: str,
        feed_id: str,
        version_key: str,
        detail: Mapping[str, Any],
    ) -> None:
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_feed_cache
                (guild_id, channel_id, feed_id, version_key, detail_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, feed_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    version_key = excluded.version_key,
                    detail_json = excluded.detail_json,
                    fetched_at = excluded.fetched_at,
                    last_seen_at = excluded.fetched_at
                """,
                (
                    guild_id,
                    channel_id,
                    feed_id,
                    version_key,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                ),
            )

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

    def _analyze_finding(
        self,
        settings: TencentChannelSettings,
        channel_id: str,
        feed: Mapping[str, Any],
        detail: Mapping[str, Any],
        classification,
        context_items: Sequence[IncomingContent] = (),
    ) -> Tuple[Any, List[ModerationFinding]]:
        item = self._incoming_content(settings, channel_id, feed, detail)
        rule_assessment = self.moderation_engine.evaluate(item, classification)
        assessment = rule_assessment
        analysis_source = "rules"
        ai_status = "disabled" if not self.config.ai_review.enabled else "fallback"
        ai_model = self.config.ai_review.model if self.config.ai_review.enabled else ""
        ai_confidence: Optional[float] = None
        ai_error = ""
        ai_analysis: Dict[str, Any] = {
            "rule_signals": [
                {
                    "code": reason.code,
                    "message": reason.message,
                    "severity": reason.severity,
                    "evidence": reason.evidence,
                }
                for reason in rule_assessment.reasons
            ]
        }
        if self.config.ai_review.enabled:
            try:
                ai = self.ai_client.review(
                    item,
                    self.config.board_policies.get(channel_id),
                    classification,
                    rule_assessment,
                    context_items,
                )
                classification, assessment = fuse_ai_review(
                    classification, rule_assessment, ai, self.config.ai_review
                )
                self._ai_reviewed += 1
                analysis_source = "ai"
                ai_status = ai.status
                ai_model = ai.model
                ai_confidence = ai.classification_confidence
                ai_error = ai.error
                if ai.vision_status == "failed":
                    self._ai_fallbacks += 1
                ai_analysis.update(
                    {
                        "provider": ai.provider,
                        "model": ai.model,
                        "vision_model": ai.vision_model,
                        "vision_status": ai.vision_status,
                        "vision_analysis": ai.vision_analysis,
                        "prompt_version": ai.prompt_version,
                        "summary": ai.summary,
                        "section": ai.section.value,
                        "classification_confidence": ai.classification_confidence,
                        "risk_level": ai.risk_level.value,
                        "risk_score": ai.risk_score,
                        "recommended_action": ai.recommended_action.value,
                    }
                )
            except AIReviewUnavailable as exc:
                self._ai_fallbacks += 1
                ai_error = str(exc)[:500]
                ai_analysis["error"] = ai_error
        if assessment.action.value == "allow" and not assessment.reasons:
            return classification, []
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
            author_id=item.author_id,
            body=item.body,
            media_urls=item.media_urls,
            source_created_at=item.created_at or "",
            classification_json=json.dumps(
                {
                    "section": classification.section.value,
                    "confidence": classification.confidence,
                    "reasons": list(classification.reasons),
                    "hashtags": list(classification.hashtags),
                    "validation_issues": list(classification.validation_issues),
                    "featured_candidate": classification.featured_candidate,
                },
                ensure_ascii=False,
            ),
            analysis_source=analysis_source,
            ai_status=ai_status,
            ai_model=ai_model,
            ai_confidence=ai_confidence,
            ai_analysis_json=json.dumps(ai_analysis, ensure_ascii=False),
            ai_error=ai_error,
        )
        self._record_moderation(finding)
        return classification, [finding]

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
                    classification_json TEXT NOT NULL DEFAULT '{}',
                    ai_reviewed INTEGER NOT NULL DEFAULT 0,
                    ai_fallbacks INTEGER NOT NULL DEFAULT 0,
                    ai_model TEXT NOT NULL DEFAULT ''
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
                    author_id TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    media_urls_json TEXT NOT NULL DEFAULT '[]',
                    source_created_at TEXT NOT NULL DEFAULT '',
                    classification_json TEXT NOT NULL DEFAULT '{}',
                    delete_status TEXT NOT NULL DEFAULT 'not_requested',
                    delete_error TEXT,
                    delete_attempted_at TEXT,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    review_notes TEXT NOT NULL DEFAULT '',
                    analysis_source TEXT NOT NULL DEFAULT 'rules',
                    ai_status TEXT NOT NULL DEFAULT 'not_requested',
                    ai_model TEXT NOT NULL DEFAULT '',
                    ai_confidence REAL,
                    ai_analysis_json TEXT NOT NULL DEFAULT '{}',
                    ai_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, feed_id, policy_version)
                );
                CREATE TABLE IF NOT EXISTS tencent_feed_cache (
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    feed_id TEXT NOT NULL,
                    version_key TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    guild_name TEXT NOT NULL DEFAULT '',
                    channel_name TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    summary_version_key TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT,
                    PRIMARY KEY(guild_id, feed_id)
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
            self._ensure_column(connection, "tencent_scan_runs", "ai_reviewed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tencent_scan_runs", "ai_fallbacks", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tencent_scan_runs", "ai_model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_scan_runs", "new_feeds", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tencent_scan_runs", "updated_feeds", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tencent_scan_runs", "cached_feeds", "INTEGER NOT NULL DEFAULT 0")
            for column, declaration in {
                "guild_name": "TEXT NOT NULL DEFAULT ''",
                "channel_name": "TEXT NOT NULL DEFAULT ''",
                "summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "summary_version_key": "TEXT NOT NULL DEFAULT ''",
                "first_seen_at": "TEXT NOT NULL DEFAULT ''",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "deleted_at": "TEXT",
            }.items():
                self._ensure_column(connection, "tencent_feed_cache", column, declaration)
            self._ensure_column(
                connection,
                "tencent_duplicate_actions",
                "guild_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(connection, "tencent_moderation_findings", "author_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "body", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "media_urls_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "tencent_moderation_findings", "source_created_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "classification_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "tencent_moderation_findings", "delete_status", "TEXT NOT NULL DEFAULT 'not_requested'")
            self._ensure_column(connection, "tencent_moderation_findings", "delete_error", "TEXT")
            self._ensure_column(connection, "tencent_moderation_findings", "delete_attempted_at", "TEXT")
            self._ensure_column(connection, "tencent_moderation_findings", "reviewed_by", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "reviewed_at", "TEXT")
            self._ensure_column(connection, "tencent_moderation_findings", "review_notes", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "analysis_source", "TEXT NOT NULL DEFAULT 'rules'")
            self._ensure_column(connection, "tencent_moderation_findings", "ai_status", "TEXT NOT NULL DEFAULT 'not_requested'")
            self._ensure_column(connection, "tencent_moderation_findings", "ai_model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "tencent_moderation_findings", "ai_confidence", "REAL")
            self._ensure_column(connection, "tencent_moderation_findings", "ai_analysis_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "tencent_moderation_findings", "ai_error", "TEXT NOT NULL DEFAULT ''")
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
            now = _utc_now()
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET review_status = 'superseded',
                    reviewed_by = 'system',
                    reviewed_at = ?,
                    review_notes = ?
                WHERE guild_id = ?
                  AND feed_id = ?
                  AND policy_version <> ?
                  AND review_status = 'pending'
                """,
                (
                    now,
                    f"由策略版本 {finding.policy_version} 的新检测结果替代",
                    finding.guild_id,
                    finding.feed_id,
                    finding.policy_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO tencent_moderation_findings
                (guild_id, guild_name, channel_id, feed_id, title, section, action,
                 risk_level, risk_score, policy_version, reasons_json, review_status,
                 author_id, body, media_urls_json, source_created_at, classification_json,
                 analysis_source, ai_status, ai_model, ai_confidence, ai_analysis_json,
                 ai_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, feed_id, policy_version) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    channel_id = excluded.channel_id,
                    title = excluded.title,
                    section = excluded.section,
                    action = excluded.action,
                    risk_level = excluded.risk_level,
                    risk_score = excluded.risk_score,
                    reasons_json = excluded.reasons_json,
                    author_id = excluded.author_id,
                    body = excluded.body,
                    media_urls_json = excluded.media_urls_json,
                    source_created_at = excluded.source_created_at,
                    classification_json = excluded.classification_json,
                    analysis_source = excluded.analysis_source,
                    ai_status = excluded.ai_status,
                    ai_model = excluded.ai_model,
                    ai_confidence = excluded.ai_confidence,
                    ai_analysis_json = excluded.ai_analysis_json,
                    ai_error = excluded.ai_error,
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
                    finding.author_id,
                    finding.body,
                    json.dumps(finding.media_urls, ensure_ascii=False),
                    finding.source_created_at,
                    finding.classification_json,
                    finding.analysis_source,
                    finding.ai_status,
                    finding.ai_model,
                    finding.ai_confidence,
                    finding.ai_analysis_json,
                    finding.ai_error,
                    now,
                ),
            )

    def _record_scan(self, report: ScanReport) -> None:
        with sqlite3.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO tencent_scan_runs
                (started_at, finished_at, scanned_feeds, duplicates, weekly_missing_topic,
                 delete_mode, guilds_json, classification_json, ai_reviewed, ai_fallbacks,
                 ai_model, new_feeds, updated_feeds, cached_feeds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    report.ai_reviewed,
                    report.ai_fallbacks,
                    report.ai_model,
                    report.new_feeds,
                    report.updated_feeds,
                    report.cached_feeds,
                ),
            )


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(char for char in text if char not in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff")
    return " ".join(text.split())


def _normalize_channel_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in text
        if unicodedata.category(character)[0] not in {"P", "S", "Z", "C"}
    )


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


def _is_rate_limit_error(exc: Exception) -> bool:
    value = str(exc).casefold()
    return "retcode=153" in value or "频率上限" in value or "rate limit" in value
