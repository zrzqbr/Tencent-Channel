import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import ClassificationResult, DuplicateCheck, IncomingContent, ModerationAssessment, PolicyReason
from .normalization import content_fingerprint


class AuditStore:
    """SQLite 审计库；同一进程内用锁保证“查询上一条+写入”原子化。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def record_and_check(
        self,
        item: IncomingContent,
        classification: ClassificationResult,
        moderation: ModerationAssessment,
    ) -> DuplicateCheck:
        fingerprint = content_fingerprint(item.title, item.body, item.media_urls)
        received_at = datetime.now(timezone.utc).isoformat()
        scope = self._scope(item, classification)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM content_events WHERE platform_item_id = ?",
                (item.platform_item_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return DuplicateCheck(
                    event_row_id=int(existing["id"]),
                    is_duplicate=False,
                    previous_platform_item_id=None,
                    is_redelivery=True,
                )

            previous = connection.execute(
                """
                SELECT platform_item_id, author_id, fingerprint
                FROM content_events
                WHERE guild_id = ? AND section_scope = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (item.guild_id, scope),
            ).fetchone()
            is_duplicate = bool(
                previous
                and previous["author_id"] == item.author_id
                and previous["fingerprint"] == fingerprint
            )
            previous_id: Optional[str] = previous["platform_item_id"] if is_duplicate else None

            cursor = connection.execute(
                """
                INSERT INTO content_events (
                    platform_item_id, item_kind, guild_id, channel_id, section,
                    section_scope, author_id, title, body, media_urls_json,
                    fingerprint, classification_json, is_duplicate,
                    previous_platform_item_id, delete_status, received_at, source_created_at,
                    moderation_json, policy_version, risk_level, risk_score,
                    recommended_action, decision_reasons_json, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.platform_item_id,
                    item.kind.value,
                    item.guild_id,
                    item.channel_id,
                    classification.section.value,
                    scope,
                    item.author_id,
                    item.title,
                    item.body,
                    json.dumps(item.media_urls, ensure_ascii=False),
                    fingerprint,
                    json.dumps(asdict(classification), ensure_ascii=False, default=str),
                    int(is_duplicate),
                    previous_id,
                    "pending" if is_duplicate else "not_needed",
                    received_at,
                    item.created_at,
                    json.dumps(asdict(moderation), ensure_ascii=False, default=str),
                    moderation.policy_version,
                    moderation.risk_level.value,
                    moderation.risk_score,
                    moderation.action.value,
                    json.dumps([asdict(reason) for reason in moderation.reasons], ensure_ascii=False),
                    "pending" if moderation.action.value != "allow" else "not_required",
                ),
            )
            connection.commit()
            return DuplicateCheck(
                event_row_id=int(cursor.lastrowid),
                is_duplicate=is_duplicate,
                previous_platform_item_id=previous_id,
            )

    def update_decision(
        self,
        row_id: int,
        recommended_action: str,
        reasons: Tuple[PolicyReason, ...],
        review_status: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE content_events
                SET recommended_action = ?, decision_reasons_json = ?, review_status = ?
                WHERE id = ?
                """,
                (
                    recommended_action,
                    json.dumps([asdict(reason) for reason in reasons], ensure_ascii=False),
                    review_status,
                    row_id,
                ),
            )
            connection.commit()

    def update_delete_result(self, row_id: int, status: str, error: Optional[str] = None) -> None:
        attempted_at = (
            datetime.now(timezone.utc).isoformat()
            if status in {"deleted", "failed", "dry_run"}
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE content_events
                SET delete_status = ?, delete_error = ?, delete_attempted_at = ?
                WHERE id = ?
                """,
                (status, error, attempted_at, row_id),
            )
            connection.commit()

    def recent_events(self, limit: int = 50, duplicates_only: bool = False) -> List[Dict[str, object]]:
        where = "WHERE is_duplicate = 1" if duplicates_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, platform_item_id, item_kind, guild_id, channel_id,
                       section, title, author_id, is_duplicate, previous_platform_item_id,
                       policy_version, risk_level, risk_score, recommended_action,
                       decision_reasons_json, review_status,
                       delete_status, delete_error, received_at
                FROM content_events
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_queue(self, limit: int = 50) -> List[Dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, platform_item_id, guild_id, channel_id, section, title,
                       risk_level, risk_score, recommended_action,
                       decision_reasons_json, review_status, received_at
                FROM content_events
                WHERE review_status = 'pending'
                ORDER BY risk_score DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_summary(self) -> Dict[str, object]:
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) AS duplicates,
                       SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_reviews,
                       SUM(CASE WHEN delete_status = 'deleted' THEN 1 ELSE 0 END) AS deleted,
                       SUM(CASE WHEN delete_status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM content_events
                """
            ).fetchone()
            by_section = {
                row["section"]: row["count"]
                for row in connection.execute(
                    "SELECT section, COUNT(*) AS count FROM content_events GROUP BY section"
                ).fetchall()
            }
            by_risk = {
                row["risk_level"]: row["count"]
                for row in connection.execute(
                    "SELECT risk_level, COUNT(*) AS count FROM content_events GROUP BY risk_level"
                ).fetchall()
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            tencent_pending = 0
            if "tencent_moderation_findings" in tables:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM tencent_moderation_findings
                    WHERE review_status = 'pending'
                    """
                ).fetchone()
                tencent_pending = int(row[0])
        return {
            "events": {
                "total": int(totals["total"] or 0),
                "duplicates": int(totals["duplicates"] or 0),
                "pending_reviews": int(totals["pending_reviews"] or 0),
                "deleted": int(totals["deleted"] or 0),
                "failed": int(totals["failed"] or 0),
            },
            "tencent_scan_pending_reviews": tencent_pending,
            "by_section": by_section,
            "by_risk": by_risk,
        }

    def resolve_review(
        self,
        row_id: int,
        resolution: str,
        reviewer: str,
        notes: str = "",
    ) -> None:
        if resolution not in {"approved", "rejected", "deleted", "ignored"}:
            raise ValueError("resolution 必须是 approved/rejected/deleted/ignored")
        resolved_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM content_events WHERE id = ?", (row_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("审核记录不存在")
            connection.execute(
                "UPDATE content_events SET review_status = ? WHERE id = ?",
                (resolution, row_id),
            )
            connection.execute(
                """
                INSERT INTO moderation_review_actions
                (event_row_id, resolution, reviewer, notes, resolved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row_id, resolution, reviewer, notes, resolved_at),
            )
            connection.commit()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_item_id TEXT NOT NULL UNIQUE,
                    item_kind TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    section TEXT NOT NULL,
                    section_scope TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    media_urls_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    classification_json TEXT NOT NULL,
                    is_duplicate INTEGER NOT NULL DEFAULT 0,
                    previous_platform_item_id TEXT,
                    delete_status TEXT NOT NULL,
                    delete_error TEXT,
                    received_at TEXT NOT NULL,
                    source_created_at TEXT,
                    delete_attempted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS moderation_review_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_row_id INTEGER NOT NULL,
                    resolution TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT NOT NULL,
                    FOREIGN KEY(event_row_id) REFERENCES content_events(id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_scope_latest
                ON content_events(guild_id, section_scope, id DESC);

                CREATE INDEX IF NOT EXISTS idx_events_duplicates
                ON content_events(is_duplicate, id DESC);
                """
            )
            self._ensure_column(connection, "content_events", "moderation_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "content_events", "policy_version", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "content_events", "risk_level", "TEXT NOT NULL DEFAULT 'low'")
            self._ensure_column(connection, "content_events", "risk_score", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "content_events", "recommended_action", "TEXT NOT NULL DEFAULT 'allow'")
            self._ensure_column(connection, "content_events", "decision_reasons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "content_events", "review_status", "TEXT NOT NULL DEFAULT 'not_required'")

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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _scope(item: IncomingContent, classification: ClassificationResult) -> str:
        if classification.section.value == "unclassified":
            return f"unclassified:{item.channel_id}"
        return classification.section.value
