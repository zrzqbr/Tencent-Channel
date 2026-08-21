import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import AuditStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class AdminStore:
    """后台查询、人工审核、内容操作和不可变操作审计。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        AuditStore(self.database_path)
        self._initialize()

    def dashboard(self) -> Dict[str, Any]:
        with self._connect() as connection:
            content = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(review_status = 'pending') AS pending,
                       SUM(is_duplicate = 1) AS duplicates,
                       SUM(delete_status = 'deleted') AS deleted,
                       SUM(delete_status = 'failed') AS failed
                FROM content_events
                """
            ).fetchone()
            tencent = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(review_status = 'pending') AS pending,
                       SUM(delete_status = 'deleted') AS deleted,
                       SUM(delete_status = 'failed') AS failed,
                       SUM(analysis_source = 'ai') AS ai_analyzed,
                       SUM(ai_status = 'fallback') AS ai_fallbacks
                FROM tencent_moderation_findings
                WHERE review_status <> 'superseded'
                """
            ).fetchone()
            duplicate = connection.execute(
                "SELECT COUNT(*) AS total FROM tencent_duplicate_actions"
            ).fetchone()
            by_section = {
                row["section"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT section, COUNT(*) AS count
                    FROM (
                        SELECT section FROM content_events
                        UNION ALL
                        SELECT section FROM tencent_moderation_findings
                        WHERE review_status <> 'superseded'
                    )
                    GROUP BY section
                    ORDER BY count DESC
                    """
                ).fetchall()
            }
            by_risk = {
                row["risk_level"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT risk_level, COUNT(*) AS count
                    FROM (
                        SELECT risk_level FROM content_events
                        UNION ALL
                        SELECT risk_level FROM tencent_moderation_findings
                        WHERE review_status <> 'superseded'
                    )
                    GROUP BY risk_level
                    """
                ).fetchall()
            }
            latest_scan = connection.execute(
                "SELECT * FROM tencent_scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            discovered = connection.execute(
                "SELECT COUNT(*) AS total FROM tencent_feed_cache"
            ).fetchone()
        total = int(content["total"] or 0) + int(tencent["total"] or 0)
        pending = int(content["pending"] or 0) + int(tencent["pending"] or 0)
        deleted = int(content["deleted"] or 0) + int(tencent["deleted"] or 0)
        failed = int(content["failed"] or 0) + int(tencent["failed"] or 0)
        return {
            "total": total,
            "pending": pending,
            "duplicates": int(content["duplicates"] or 0) + int(duplicate["total"] or 0),
            "deleted": deleted,
            "failed": failed,
            "by_section": by_section,
            "by_risk": by_risk,
            "latest_scan": self._scan_row(latest_scan) if latest_scan else None,
            "discovered_feeds": int(discovered["total"] or 0),
            "ai_analyzed": int(tencent["ai_analyzed"] or 0),
            "ai_fallbacks": int(tencent["ai_fallbacks"] or 0),
        }

    def reviews(
        self,
        status: str = "pending",
        risk_level: str = "",
        guild_id: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            content_rows = connection.execute(
                """
                SELECT id, platform_item_id AS item_id, guild_id, '' AS guild_name,
                       channel_id, section, title, body, author_id, risk_level,
                       risk_score, recommended_action AS action, policy_version,
                       decision_reasons_json AS reasons_json, review_status,
                       delete_status, delete_error, received_at AS created_at,
                       source_created_at, classification_json, media_urls_json,
                       'rules' AS analysis_source, 'not_requested' AS ai_status,
                       '' AS ai_model, NULL AS ai_confidence, '{}' AS ai_analysis_json,
                       '' AS ai_error
                FROM content_events
                WHERE (? = '' OR review_status = ?)
                  AND (? = '' OR risk_level = ?)
                  AND (? = '' OR guild_id = ?)
                ORDER BY risk_score DESC, id DESC
                LIMIT ?
                """,
                (status, status, risk_level, risk_level, guild_id, guild_id, limit),
            ).fetchall()
            tencent_rows = connection.execute(
                """
                SELECT id, feed_id AS item_id, guild_id, guild_name, channel_id,
                       section, title, body, author_id, risk_level, risk_score,
                       action, policy_version, reasons_json, review_status,
                       delete_status, delete_error, created_at, source_created_at,
                       classification_json, media_urls_json, analysis_source,
                       ai_status, ai_model, ai_confidence, ai_analysis_json, ai_error
                FROM tencent_moderation_findings
                WHERE (? = '' OR review_status = ?)
                  AND (? = '' OR risk_level = ?)
                  AND (? = '' OR guild_id = ?)
                  AND review_status <> 'superseded'
                  AND id = (
                      SELECT MAX(current.id)
                      FROM tencent_moderation_findings AS current
                      WHERE current.guild_id = tencent_moderation_findings.guild_id
                        AND current.feed_id = tencent_moderation_findings.feed_id
                  )
                ORDER BY risk_score DESC, id DESC
                LIMIT ?
                """,
                (status, status, risk_level, risk_level, guild_id, guild_id, limit),
            ).fetchall()
        items = [self._review_row(row, "event") for row in content_rows]
        items.extend(self._review_row(row, "tencent") for row in tencent_rows)
        return sorted(
            items,
            key=lambda item: (int(item.get("risk_score") or 0), str(item.get("created_at") or "")),
            reverse=True,
        )[:limit]

    def contents(
        self,
        guild_id: str = "",
        channel_id: str = "",
        query: str = "",
        *,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT cache.*,
                       finding.id AS finding_id,
                       finding.action, finding.risk_level, finding.risk_score,
                       finding.review_status, finding.delete_status, finding.delete_error,
                       finding.section, finding.reasons_json
                FROM tencent_feed_cache AS cache
                LEFT JOIN tencent_moderation_findings AS finding
                  ON finding.id = (
                    SELECT MAX(current.id)
                    FROM tencent_moderation_findings AS current
                    WHERE current.guild_id = cache.guild_id
                      AND current.feed_id = cache.feed_id
                  )
                WHERE (? = '' OR cache.guild_id = ?)
                  AND (? = '' OR cache.channel_id = ?)
                  AND (? = 1 OR cache.deleted_at IS NULL)
                ORDER BY COALESCE(
                    CAST(json_extract(cache.detail_json, '$.create_time_raw') AS INTEGER),
                    CAST(json_extract(cache.summary_json, '$.create_time_raw') AS INTEGER),
                    0
                ) DESC, cache.last_seen_at DESC
                LIMIT ?
                """,
                (
                    guild_id,
                    guild_id,
                    channel_id,
                    channel_id,
                    1 if include_deleted else 0,
                    limit,
                ),
            ).fetchall()
        items = [self._content_row(row) for row in rows]
        normalized_query = str(query or "").strip().casefold()
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("title", "body", "author_name", "channel_name", "guild_name")
                ).casefold()
            ]
        return items

    def get_content(self, guild_id: str, feed_id: str) -> Dict[str, Any]:
        items = self.contents(guild_id=guild_id, include_deleted=True, limit=1000)
        item = next((value for value in items if value["feed_id"] == feed_id), None)
        if item is None:
            raise ValueError("频道内容不存在，请先执行一次巡检")
        return item

    def record_content_edit(
        self,
        guild_id: str,
        feed_id: str,
        title: str,
        body: str,
        actor: str,
        remote_ip: str = "",
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT detail_json, summary_json FROM tencent_feed_cache WHERE guild_id = ? AND feed_id = ?",
                (guild_id, feed_id),
            ).fetchone()
            if row is None:
                raise ValueError("频道内容不存在")
            detail = _json(row["detail_json"], {})
            summary = _json(row["summary_json"], {})
            detail.update({"title": title, "content": body})
            summary.update({"title": title, "content_snippet": body[:300]})
            connection.execute(
                """
                UPDATE tencent_feed_cache
                SET detail_json = ?, summary_json = ?, fetched_at = ?, last_seen_at = ?
                WHERE guild_id = ? AND feed_id = ?
                """,
                (
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    guild_id,
                    feed_id,
                ),
            )
            connection.execute(
                "UPDATE tencent_moderation_findings SET title = ?, body = ? WHERE guild_id = ? AND feed_id = ?",
                (title, body, guild_id, feed_id),
            )
            self._insert_audit(
                connection,
                actor,
                "content.edit",
                "tencent",
                feed_id,
                {"guild_id": guild_id, "title": title},
                remote_ip,
            )
            connection.commit()

    def record_content_delete(
        self,
        guild_id: str,
        feed_id: str,
        actor: str,
        error: str = "",
        remote_ip: str = "",
    ) -> None:
        now = _utc_now()
        status = "failed" if error else "deleted"
        with self._lock, self._connect() as connection:
            if not error:
                connection.execute(
                    "UPDATE tencent_feed_cache SET deleted_at = ? WHERE guild_id = ? AND feed_id = ?",
                    (now, guild_id, feed_id),
                )
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET delete_status = ?, delete_error = ?, delete_attempted_at = ?,
                    review_status = CASE WHEN ? = 'deleted' THEN 'deleted' ELSE review_status END
                WHERE guild_id = ? AND feed_id = ?
                """,
                (status, error or None, now, status, guild_id, feed_id),
            )
            self._insert_audit(
                connection,
                actor,
                "delete.execute" if not error else "delete.failed",
                "tencent",
                feed_id,
                {"guild_id": guild_id, "status": status, "error": error},
                remote_ip,
            )
            connection.commit()

    def record_content_move(
        self,
        guild_id: str,
        feed_id: str,
        channel_id: str,
        channel_name: str,
        section: str,
        actor: str,
        remote_ip: str = "",
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tencent_feed_cache
                SET channel_id = ?, channel_name = ?, last_seen_at = ?
                WHERE guild_id = ? AND feed_id = ?
                """,
                (channel_id, channel_name, now, guild_id, feed_id),
            )
            connection.execute(
                """
                UPDATE tencent_moderation_findings
                SET channel_id = ?, section = ?, review_status = 'approved'
                WHERE guild_id = ? AND feed_id = ?
                """,
                (channel_id, section, guild_id, feed_id),
            )
            self._insert_audit(
                connection,
                actor,
                "move.execute",
                "tencent",
                feed_id,
                {"guild_id": guild_id, "target_channel_id": channel_id, "target_section": section},
                remote_ip,
            )
            connection.commit()

    def get_review(self, source: str, row_id: int) -> Dict[str, Any]:
        if source not in {"event", "tencent"}:
            raise ValueError("审核来源无效")
        with self._connect() as connection:
            if source == "event":
                row = connection.execute(
                    """
                    SELECT id, platform_item_id AS item_id, guild_id, '' AS guild_name,
                           channel_id, section, title, body, author_id, risk_level,
                           risk_score, recommended_action AS action, policy_version,
                           decision_reasons_json AS reasons_json, review_status,
                           delete_status, delete_error, received_at AS created_at,
                           source_created_at, classification_json, media_urls_json,
                           'rules' AS analysis_source, 'not_requested' AS ai_status,
                           '' AS ai_model, NULL AS ai_confidence, '{}' AS ai_analysis_json,
                           '' AS ai_error
                    FROM content_events WHERE id = ?
                    """,
                    (int(row_id),),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT id, feed_id AS item_id, guild_id, guild_name, channel_id,
                           section, title, body, author_id, risk_level, risk_score,
                           action, policy_version, reasons_json, review_status,
                           delete_status, delete_error, created_at, source_created_at,
                           classification_json, media_urls_json, analysis_source,
                           ai_status, ai_model, ai_confidence, ai_analysis_json, ai_error
                    FROM tencent_moderation_findings WHERE id = ?
                    """,
                    (int(row_id),),
                ).fetchone()
        if row is None:
            raise ValueError("审核记录不存在")
        return self._review_row(row, source)

    def resolve_review(
        self,
        source: str,
        row_id: int,
        resolution: str,
        actor: str,
        notes: str,
        remote_ip: str = "",
    ) -> None:
        if resolution not in {"approved", "rejected", "ignored"}:
            raise ValueError("审核结论无效")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if source == "event":
                cursor = connection.execute(
                    "UPDATE content_events SET review_status = ? WHERE id = ?",
                    (resolution, int(row_id)),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ValueError("审核记录不存在")
                connection.execute(
                    """
                    INSERT INTO moderation_review_actions
                    (event_row_id, resolution, reviewer, notes, resolved_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (int(row_id), resolution, actor, notes, now),
                )
            elif source == "tencent":
                cursor = connection.execute(
                    """
                    UPDATE tencent_moderation_findings
                    SET review_status = ?, reviewed_by = ?, reviewed_at = ?, review_notes = ?
                    WHERE id = ?
                    """,
                    (resolution, actor, now, notes, int(row_id)),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ValueError("审核记录不存在")
            else:
                connection.rollback()
                raise ValueError("审核来源无效")
            self._insert_audit(
                connection,
                actor,
                "review.resolve",
                source,
                str(row_id),
                {"resolution": resolution, "notes": notes},
                remote_ip,
            )
            connection.commit()

    def duplicates(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, guild_id, guild_name, channel_id, section,
                       newer_feed_id, older_feed_id, delete_status, error, created_at
                FROM tencent_duplicate_actions
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def scans(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tencent_scan_runs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._scan_row(row) for row in rows]

    def audit_log(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_audit_actions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = _json(item.pop("details_json", "{}"), {})
            result.append(item)
        return result

    def record_audit(
        self,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Optional[Dict[str, Any]] = None,
        remote_ip: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            self._insert_audit(
                connection,
                actor,
                action,
                target_type,
                target_id,
                details or {},
                remote_ip,
            )
            connection.commit()

    def create_delete_challenge(
        self,
        row_id: int,
        actor: str,
        remote_ip: str = "",
    ) -> str:
        review = self.get_review("tencent", row_id)
        self.ensure_current_tencent_review(row_id)
        if review["delete_status"] == "deleted":
            raise ValueError("该内容已经删除")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_delete_requests
                (token_hash, source, target_id, actor, created_at, expires_at)
                VALUES (?, 'tencent', ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    int(row_id),
                    actor,
                    now.isoformat(),
                    (now + timedelta(minutes=10)).isoformat(),
                ),
            )
            self._insert_audit(
                connection,
                actor,
                "delete.prepare",
                "tencent",
                str(row_id),
                {"feed_id": review["item_id"]},
                remote_ip,
            )
            connection.commit()
        return token

    def ensure_current_tencent_review(self, row_id: int) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, guild_id, feed_id, delete_status
                FROM tencent_moderation_findings WHERE id = ?
                """,
                (int(row_id),),
            ).fetchone()
            if row is None:
                raise ValueError("审核记录不存在")
            latest = connection.execute(
                """
                SELECT id, delete_status
                FROM tencent_moderation_findings
                WHERE guild_id = ? AND feed_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (row["guild_id"], row["feed_id"]),
            ).fetchone()
            deleted = connection.execute(
                """
                SELECT 1 FROM tencent_moderation_findings
                WHERE guild_id = ? AND feed_id = ? AND delete_status = 'deleted'
                LIMIT 1
                """,
                (row["guild_id"], row["feed_id"]),
            ).fetchone()
        if deleted:
            raise ValueError("该内容已经删除")
        if latest is None or int(latest["id"]) != int(row_id):
            raise ValueError("这是同一帖子的历史审核记录，请刷新页面后操作最新记录")

    def consume_delete_challenge(self, row_id: int, actor: str, token: str) -> None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, expires_at, used_at
                FROM manual_delete_requests
                WHERE token_hash = ? AND source = 'tencent' AND target_id = ? AND actor = ?
                ORDER BY id DESC LIMIT 1
                """,
                (token_hash, int(row_id), actor),
            ).fetchone()
            if row is None or row["used_at"]:
                connection.rollback()
                raise ValueError("删除确认已失效，请重新发起")
            if datetime.fromisoformat(row["expires_at"]) < now:
                connection.rollback()
                raise ValueError("删除确认已过期，请重新发起")
            connection.execute(
                "UPDATE manual_delete_requests SET used_at = ? WHERE id = ?",
                (now.isoformat(), int(row["id"])),
            )
            connection.commit()

    def create_action_challenge(
        self,
        action: str,
        actor: str,
        remote_ip: str = "",
    ) -> str:
        if action not in {"bulk_delete", "bulk_move"}:
            raise ValueError("操作确认类型无效")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_delete_requests
                (token_hash, source, target_id, actor, created_at, expires_at)
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (
                    token_hash,
                    action,
                    actor,
                    now.isoformat(),
                    (now + timedelta(minutes=10)).isoformat(),
                ),
            )
            self._insert_audit(
                connection,
                actor,
                f"{action}.prepare",
                "tencent_batch",
                "0",
                {},
                remote_ip,
            )
            connection.commit()
        return token

    def consume_action_challenge(self, action: str, actor: str, token: str) -> None:
        if action not in {"bulk_delete", "bulk_move"}:
            raise ValueError("操作确认类型无效")
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, expires_at, used_at
                FROM manual_delete_requests
                WHERE token_hash = ? AND source = ? AND target_id = 0 AND actor = ?
                ORDER BY id DESC LIMIT 1
                """,
                (token_hash, action, actor),
            ).fetchone()
            if row is None or row["used_at"]:
                connection.rollback()
                raise ValueError("该批操作已经提交，请刷新页面查看最终结果")
            if datetime.fromisoformat(row["expires_at"]) < now:
                connection.rollback()
                raise ValueError("批量操作确认已过期，请重新发起")
            connection.execute(
                "UPDATE manual_delete_requests SET used_at = ? WHERE id = ?",
                (now.isoformat(), int(row["id"])),
            )
            connection.commit()

    def record_delete_result(
        self,
        row_id: int,
        actor: str,
        status: str,
        error: str,
        reason: str,
        remote_ip: str = "",
    ) -> None:
        now = _utc_now()
        review_status = "deleted" if status == "deleted" else "pending"
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = connection.execute(
                "SELECT guild_id, feed_id FROM tencent_moderation_findings WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if review is None:
                connection.rollback()
                raise ValueError("审核记录不存在")
            if status == "failed":
                cursor = connection.execute(
                    """
                    UPDATE tencent_moderation_findings
                    SET delete_status = ?, delete_error = ?, delete_attempted_at = ?,
                        review_status = ?, reviewed_by = ?, reviewed_at = ?, review_notes = ?
                    WHERE id = ? AND delete_status <> 'deleted'
                      AND NOT EXISTS (
                          SELECT 1 FROM tencent_moderation_findings AS sibling
                          WHERE sibling.guild_id = ? AND sibling.feed_id = ?
                            AND sibling.delete_status = 'deleted'
                      )
                    """,
                    (
                        status, error or None, now, review_status, actor, now, reason,
                        int(row_id), review["guild_id"], review["feed_id"],
                    ),
                )
                if cursor.rowcount == 0:
                    existing = connection.execute(
                        """
                        SELECT 1 FROM tencent_moderation_findings
                        WHERE guild_id = ? AND feed_id = ? AND delete_status = 'deleted'
                        LIMIT 1
                        """,
                        (review["guild_id"], review["feed_id"]),
                    ).fetchone()
                    if existing:
                        self._insert_audit(
                            connection,
                            actor,
                            "delete.duplicate_result_ignored",
                            "tencent",
                            str(row_id),
                            {"attempted_status": status, "error": error},
                            remote_ip,
                        )
                        connection.commit()
                        return
            else:
                cursor = connection.execute(
                    """
                    UPDATE tencent_moderation_findings
                    SET delete_status = ?, delete_error = ?, delete_attempted_at = ?,
                        review_status = ?, reviewed_by = ?, reviewed_at = ?, review_notes = ?
                    WHERE guild_id = ? AND feed_id = ?
                    """,
                    (
                        status, error or None, now, review_status, actor, now, reason,
                        review["guild_id"], review["feed_id"],
                    ),
                )
                connection.execute(
                    "UPDATE tencent_feed_cache SET deleted_at = ? WHERE guild_id = ? AND feed_id = ?",
                    (now, review["guild_id"], review["feed_id"]),
                )
            if cursor.rowcount < 1:
                connection.rollback()
                raise ValueError("审核记录不存在")
            self._insert_audit(
                connection,
                actor,
                "delete.execute",
                "tencent",
                str(row_id),
                {"status": status, "error": error, "reason": reason},
                remote_ip,
            )
            connection.commit()

    def record_move_result(
        self,
        row_id: int,
        actor: str,
        status: str,
        target_channel_id: str,
        target_section: str,
        error: str,
        reason: str,
        remote_ip: str = "",
        target_channel_name: str = "",
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT guild_id, channel_id, section, feed_id FROM tencent_moderation_findings WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("审核记录不存在")
            if status == "moved":
                connection.execute(
                    """
                    UPDATE tencent_moderation_findings
                    SET channel_id = ?, section = ?, review_status = 'approved',
                        reviewed_by = ?, reviewed_at = ?, review_notes = ?
                    WHERE id = ?
                    """,
                    (
                        target_channel_id,
                        target_section,
                        actor,
                        now,
                        reason,
                        int(row_id),
                    ),
                )
                connection.execute(
                    """
                    UPDATE tencent_feed_cache
                    SET channel_id = ?, channel_name = CASE WHEN ? <> '' THEN ? ELSE channel_name END,
                        last_seen_at = ?
                    WHERE guild_id = ? AND feed_id = ?
                    """,
                    (
                        target_channel_id,
                        target_channel_name,
                        target_channel_name,
                        now,
                        row["guild_id"],
                        row["feed_id"],
                    ),
                )
            self._insert_audit(
                connection,
                actor,
                "move.execute",
                "tencent",
                str(row_id),
                {
                    "status": status,
                    "feed_id": row["feed_id"],
                    "original_channel_id": row["channel_id"],
                    "original_section": row["section"],
                    "target_channel_id": target_channel_id,
                    "target_section": target_section,
                    "error": error,
                    "reason": reason,
                },
                remote_ip,
            )
            connection.commit()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
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

                CREATE TABLE IF NOT EXISTS admin_audit_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    remote_ip TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_delete_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
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

                CREATE INDEX IF NOT EXISTS idx_tencent_review_status
                ON tencent_moderation_findings(review_status, risk_score DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_admin_audit_latest
                ON admin_audit_actions(id DESC);
                """
            )
            for column, declaration in {
                "author_id": "TEXT NOT NULL DEFAULT ''",
                "body": "TEXT NOT NULL DEFAULT ''",
                "media_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_created_at": "TEXT NOT NULL DEFAULT ''",
                "classification_json": "TEXT NOT NULL DEFAULT '{}'",
                "delete_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "delete_error": "TEXT",
                "delete_attempted_at": "TEXT",
                "reviewed_by": "TEXT NOT NULL DEFAULT ''",
                "reviewed_at": "TEXT",
                "review_notes": "TEXT NOT NULL DEFAULT ''",
                "analysis_source": "TEXT NOT NULL DEFAULT 'rules'",
                "ai_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "ai_model": "TEXT NOT NULL DEFAULT ''",
                "ai_confidence": "REAL",
                "ai_analysis_json": "TEXT NOT NULL DEFAULT '{}'",
                "ai_error": "TEXT NOT NULL DEFAULT ''",
            }.items():
                self._ensure_column(connection, "tencent_moderation_findings", column, declaration)
            for column, declaration in {
                "ai_reviewed": "INTEGER NOT NULL DEFAULT 0",
                "ai_fallbacks": "INTEGER NOT NULL DEFAULT 0",
                "ai_model": "TEXT NOT NULL DEFAULT ''",
                "new_feeds": "INTEGER NOT NULL DEFAULT 0",
                "updated_feeds": "INTEGER NOT NULL DEFAULT 0",
                "cached_feeds": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                self._ensure_column(connection, "tencent_scan_runs", column, declaration)
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _review_row(row: sqlite3.Row, source: str) -> Dict[str, Any]:
        item = dict(row)
        item["source"] = source
        item["reasons"] = _json(item.pop("reasons_json", "[]"), [])
        item["classification"] = _json(item.pop("classification_json", "{}"), {})
        item["media_urls"] = _json(item.pop("media_urls_json", "[]"), [])
        item["ai_analysis"] = _json(item.pop("ai_analysis_json", "{}"), {})
        return item

    @staticmethod
    def _scan_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["guilds"] = _json(item.pop("guilds_json", "[]"), [])
        item["classification"] = _json(item.pop("classification_json", "{}"), {})
        return item

    @staticmethod
    def _content_row(row: sqlite3.Row) -> Dict[str, Any]:
        raw = dict(row)
        detail = _json(raw.pop("detail_json", "{}"), {})
        summary = _json(raw.pop("summary_json", "{}"), {})
        merged = dict(summary)
        merged.update({key: value for key, value in detail.items() if value not in (None, "", [], {})})
        content_richtext = merged.get("content_richtext") or {}
        reasons = _json(raw.pop("reasons_json", "[]"), [])
        return {
            **raw,
            "feed_id": raw.get("feed_id", ""),
            "guild_name": raw.get("guild_name") or merged.get("guild_name") or raw.get("guild_id", ""),
            "channel_name": raw.get("channel_name") or merged.get("channel_name") or "未命名栏目",
            "title": str(merged.get("title") or "无标题内容"),
            "body": str(merged.get("content") or merged.get("content_snippet") or ""),
            "author_id": str(merged.get("author_id") or ""),
            "author_name": str(merged.get("author") or merged.get("author_name") or "频道用户"),
            "feed_type": int(merged.get("feed_type") or 1),
            "is_markdown": bool(merged.get("is_markdown") or content_richtext.get("is_markdown")),
            "create_time_raw": str(merged.get("create_time_raw") or ""),
            "source_created_at": str(merged.get("create_time") or merged.get("create_time_raw") or ""),
            "share_url": str(merged.get("share_url") or ""),
            "images": list(merged.get("images") or []),
            "reasons": reasons,
            "action": raw.get("action") or "allow",
            "risk_level": raw.get("risk_level") or "low",
            "risk_score": int(raw.get("risk_score") or 0),
            "review_status": raw.get("review_status") or "not_required",
            "delete_status": raw.get("delete_status") or ("deleted" if raw.get("deleted_at") else "not_requested"),
            "sync_complete": bool(detail),
        }

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Dict[str, Any],
        remote_ip: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO admin_audit_actions
            (actor, action, target_type, target_id, details_json, request_id, remote_ip, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                target_type,
                target_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                secrets.token_hex(12),
                remote_ip,
                _utc_now(),
            ),
        )
