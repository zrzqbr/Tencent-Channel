import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from .ai_review import AIReviewClient, AIReviewUnavailable, fuse_ai_review
from .admin_store import AdminStore
from .classifier import ContentClassifier
from .config import GuardConfig
from .config_editor import ConfigEditor
from .models import IncomingContent, ItemKind, Section
from .moderation import ModerationEngine
from .official_capabilities import (
    OFFICIAL_SKILL_VERSION,
    grouped_capabilities,
    normalize_index,
    normalize_command_parameters,
    parse_parameters,
    safe_audit_parameters,
    safe_payload,
)
from .placement import group_placement_suggestions, placement_review
from .scan_control import ScanLock, ScanStatusStore
from .tencent_cli import TencentCliClient, TencentCliError
from .tencent_monitor import TencentChannelMonitor


SECTION_LABELS = {section.value: section.display_name for section in Section}
RISK_LABELS = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
ACTION_LABELS = {
    "allow": "没有发现问题",
    "review": "需要人工核对",
    "delete_candidate": "可能违规",
}
STATUS_LABELS = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "ignored": "已忽略",
    "deleted": "已删除",
    "not_required": "无需审核",
    "superseded": "已被新策略替代",
}
AI_STATUS_LABELS = {
    "completed": "已完成",
    "completed_text_only": "文字判断完成，图片需人工核对",
    "cached": "已完成",
    "cached_text_only": "文字判断完成，图片需人工核对",
    "fallback": "仅完成基础检查",
    "disabled": "尚未开启",
    "not_requested": "尚未完成",
    "failed": "暂时不可用",
}
AI_PUBLIC_STATUS_LABELS = {
    "ready": "已连接，可执行",
    "missing_key": "尚未连接",
    "disabled": "未启用",
}
VISION_STATUS_LABELS = {
    "ready": "已连接，有图片时执行",
    "completed": "已完成",
    "cached": "已完成",
    "not_requested": "尚未检查",
    "failed": "暂时不可用，需要人工核对",
    "missing_key": "尚未连接",
    "disabled": "未启用",
}


class LoginLimiter:
    def __init__(self, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._values: Dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            values = self._values[key]
            while values and values[0] < now - self.window_seconds:
                values.popleft()
            return len(values) >= self.attempts

    def fail(self, key: str) -> None:
        with self._lock:
            self._values[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


def create_app(
    config_path: Optional[str] = None,
    *,
    cli_factory: Callable[[], TencentCliClient] = TencentCliClient,
) -> Flask:
    resolved_config = Path(
        config_path or os.environ.get("QQ_GUARD_CONFIG", "config.json")
    ).expanduser().resolve()
    guard_config = GuardConfig.from_file(str(resolved_config))
    app = Flask(__name__)
    app.jinja_env.filters["cn_time"] = _cn_time
    app.jinja_env.filters["public_ai_error"] = _public_ai_error
    app.jinja_env.filters["plain_ai_text"] = _plain_ai_text
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=os.environ.get("QQ_GUARD_SECRET_KEY", ""),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        MAX_CONTENT_LENGTH=512 * 1024,
    )
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("缺少 QQ_GUARD_SECRET_KEY")
    password_hash = os.environ.get("QQ_GUARD_ADMIN_PASSWORD_HASH", "")
    if not password_hash:
        raise RuntimeError("缺少 QQ_GUARD_ADMIN_PASSWORD_HASH")

    store = AdminStore(guard_config.database_path)
    editor = ConfigEditor(resolved_config)
    limiter = LoginLimiter()
    scan_status_store = ScanStatusStore(guard_config.database_path)
    sync_status_store = ScanStatusStore(guard_config.database_path, "sync")

    def remote_ip() -> str:
        return str(request.remote_addr or "")[:80]

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = os.urandom(24).hex()
        return str(session["csrf_token"])

    def authenticated() -> bool:
        return bool(session.get("authenticated") and session.get("actor") == "admin")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not authenticated():
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def verify_csrf() -> None:
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token")
        if not supplied or not expected or not secrets_compare(str(supplied), str(expected)):
            abort(400, "CSRF 校验失败")

    @app.before_request
    def protect_requests():
        if request.method == "POST":
            verify_csrf()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.path not in {"/static/"}:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.context_processor
    def template_context():
        current_config = GuardConfig.from_file(str(resolved_config))
        return {
            "csrf_token": csrf_token,
            "section_labels": SECTION_LABELS,
            "risk_labels": RISK_LABELS,
            "action_labels": ACTION_LABELS,
            "status_labels": STATUS_LABELS,
            "ai_status_labels": AI_STATUS_LABELS,
            "ai_public_status_labels": AI_PUBLIC_STATUS_LABELS,
            "vision_status_labels": VISION_STATUS_LABELS,
            "nav_guilds": [
                {"id": item.guild_id, "name": item.name or item.guild_id}
                for item in current_config.tencent_channels
            ],
            "manual_delete_enabled": os.environ.get(
                "QQ_GUARD_MANUAL_DELETE_ENABLED", "false"
            ).casefold()
            == "true",
        }

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if authenticated():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            key = remote_ip()
            if limiter.blocked(key):
                store.record_audit("anonymous", "auth.blocked", "session", "", {}, key)
                flash("登录尝试过多，请 5 分钟后再试。", "error")
                return render_template("login.html"), 429
            password = request.form.get("password", "")
            if check_password_hash(password_hash, password):
                session.clear()
                session["authenticated"] = True
                session["actor"] = "admin"
                session["csrf_token"] = os.urandom(24).hex()
                session.permanent = True
                limiter.clear(key)
                store.record_audit("admin", "auth.login", "session", "", {}, key)
                return redirect(safe_next(request.form.get("next", "")) or url_for("dashboard"))
            limiter.fail(key)
            store.record_audit("anonymous", "auth.failed", "session", "", {}, key)
            flash("密码不正确。", "error")
        return render_template("login.html", next=request.args.get("next", ""))

    @app.post("/logout")
    @login_required
    def logout():
        store.record_audit("admin", "auth.logout", "session", "", {}, remote_ip())
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        summary = store.dashboard()
        reviews = store.reviews(status="pending", limit=100)
        scans = store.scans(limit=5)
        current = GuardConfig.from_file(str(resolved_config))
        ai_status = AIReviewClient(current.ai_review, current.database_path).public_status()
        _prepare_review_items(reviews, current)
        queue_counts = {"allow": 0, "placement": 0, "review": 0, "delete": 0, "incomplete": 0}
        for item in reviews:
            queue_counts[item["ui_queue"]] += 1

        task_counts = {
            "delete": queue_counts["delete"],
            "placement": queue_counts["placement"],
            "review": queue_counts["review"] + queue_counts["incomplete"],
            "allow": queue_counts["allow"],
        }
        for item in reviews:
            item["dashboard_task"] = {
                "delete": "delete",
                "review": "review",
                "placement": "placement",
                "incomplete": "review",
                "allow": "allow",
            }[item["ui_queue"]]

        selected_task = request.args.get("task", request.args.get("queue", ""))
        legacy_tasks = {
            "action": "delete" if task_counts["delete"] else "review",
            "inspect": "review",
            "incomplete": "review",
            "clear": "allow",
        }
        selected_task = legacy_tasks.get(selected_task, selected_task)
        if selected_task not in task_counts:
            selected_task = next(
                (key for key in ("delete", "placement", "review", "allow") if task_counts[key]),
                "allow",
            )
        visible_reviews = [item for item in reviews if item["dashboard_task"] == selected_task]
        return render_template(
            "dashboard.html",
            summary=summary,
            reviews=visible_reviews,
            all_pending_reviews=reviews,
            selected_task=selected_task,
            queue_counts=queue_counts,
            task_counts=task_counts,
            pending_high_count=queue_counts["delete"],
            scans=scans,
            config=current,
            ai_status=ai_status,
            move_targets=_configured_move_targets(current),
            latest_sync=store.latest_content_sync(),
        )

    @app.get("/contents")
    @login_required
    def contents():
        guild_id = request.args.get("guild", "")
        channel_id = request.args.get("channel", "")
        query = request.args.get("q", "").strip()[:200]
        current = GuardConfig.from_file(str(resolved_config))
        items = store.contents(
            guild_id=guild_id,
            channel_id=channel_id,
            query=query,
            limit=1000,
        )
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 20
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, total_pages)
        channels = sorted(
            {
                (item["channel_id"], item["channel_name"])
                for item in store.contents(guild_id=guild_id, limit=1000)
            },
            key=lambda value: value[1],
        )
        return render_template(
            "contents.html",
            items=items[(page - 1) * page_size : page * page_size],
            total_items=len(items),
            page=page,
            total_pages=total_pages,
            selected_guild=guild_id,
            selected_channel=channel_id,
            query=query,
            channels=channels,
            move_targets=_configured_move_targets(current),
            latest_sync=store.latest_content_sync(),
        )

    @app.post("/contents/<guild_id>/<feed_id>/edit")
    @login_required
    def edit_content(guild_id: str, feed_id: str):
        title = request.form.get("title", "").strip()[:500]
        body = request.form.get("body", "").strip()[:50000]
        try:
            item = store.get_content(guild_id, feed_id)
            if item.get("deleted_at"):
                raise ValueError("这条内容已经删除")
            if not title and not body:
                raise ValueError("标题和正文不能同时为空")
            client = cli_factory()
            client.alter_feed(
                item["guild_id"],
                item["channel_id"],
                item["feed_id"],
                item["create_time_raw"],
                item["feed_type"],
                title,
                body,
                markdown=item["is_markdown"],
            )
            store.record_content_edit(
                guild_id,
                feed_id,
                title,
                body,
                "admin",
                remote_ip(),
            )
        except (TencentCliError, ValueError, OSError, AttributeError) as exc:
            flash(_public_operation_error(exc), "error")
        else:
            flash("帖子已修改，并同步到腾讯频道。", "success")
        return redirect(url_for("contents", guild=guild_id))

    @app.post("/contents/<guild_id>/<feed_id>/delete")
    @login_required
    def delete_content(guild_id: str, feed_id: str):
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("contents", guild=guild_id))
        try:
            item = store.get_content(guild_id, feed_id)
            if item.get("deleted_at"):
                raise ValueError("这条内容已经删除")
            delete_result = _delete_tencent_item(cli_factory(), item)
        except (TencentCliError, ValueError, OSError, AttributeError) as exc:
            store.record_content_delete(guild_id, feed_id, "admin", str(exc)[:500], remote_ip())
            flash(_public_operation_error(exc), "error")
        else:
            store.record_content_delete(guild_id, feed_id, "admin", remote_ip=remote_ip())
            if delete_result == "already_missing":
                flash("帖子在腾讯频道中已不存在，后台记录已同步为已删除。", "success")
            else:
                flash("帖子已从腾讯频道删除。", "success")
        return redirect(url_for("contents", guild=guild_id))

    @app.post("/contents/<guild_id>/<feed_id>/move")
    @login_required
    def move_content(guild_id: str, feed_id: str):
        current = GuardConfig.from_file(str(resolved_config))
        target = _find_move_target(current, request.form.get("move_target", ""))
        try:
            item = store.get_content(guild_id, feed_id)
            if target is None or target["guild_id"] != guild_id:
                raise ValueError("请选择当前频道内的目标栏目")
            cli_factory().move_feed(
                guild_id,
                item["channel_id"],
                target["channel_id"],
                feed_id,
            )
            store.record_content_move(
                guild_id,
                feed_id,
                target["channel_id"],
                target["label"],
                target["section"],
                "admin",
                remote_ip(),
            )
        except (TencentCliError, ValueError, OSError, AttributeError) as exc:
            flash(_public_operation_error(exc), "error")
        else:
            flash(f"帖子已移动到“{target['label']}”。", "success")
        return redirect(url_for("contents", guild=guild_id))

    @app.get("/reviews")
    @login_required
    def reviews():
        status = request.args.get("status", "pending")
        if status not in {"", "pending", "approved", "rejected", "ignored", "deleted"}:
            status = "pending"
        risk = request.args.get("risk", "")
        guild_id = request.args.get("guild", "")
        current = GuardConfig.from_file(str(resolved_config))
        items = store.reviews(status=status, risk_level=risk, guild_id=guild_id)
        _prepare_review_items(items, current)
        return render_template(
            "reviews.html",
            items=items,
            selected_status=status,
            selected_risk=risk,
            selected_guild=guild_id,
            show_bulk_actions=True,
            move_targets=_configured_move_targets(current),
        )

    @app.get("/placements")
    @login_required
    def placements():
        current = GuardConfig.from_file(str(resolved_config))
        selected_guild = request.args.get("guild", "")
        items = store.reviews(status="", guild_id=selected_guild, limit=500)
        suggestions, attention = placement_review(items, current)
        return render_template(
            "placements.html",
            groups=group_placement_suggestions(suggestions),
            attention=attention,
            suggestion_count=len(suggestions),
            selected_guild=selected_guild,
            guilds=[
                {"id": settings.guild_id, "name": settings.name or settings.guild_id}
                for settings in current.tencent_channels
            ],
        )

    @app.get("/ai-analysis")
    @login_required
    def ai_analysis():
        selected_source = request.args.get("source", "")
        if selected_source not in {"", "ai", "rules"}:
            selected_source = ""
        selected_risk = request.args.get("risk", "")
        if selected_risk not in {"", "critical", "high", "medium", "low"}:
            selected_risk = ""
        items = store.reviews(status="", risk_level=selected_risk, limit=300)
        if selected_source:
            items = [item for item in items if item.get("analysis_source") == selected_source]
        current = GuardConfig.from_file(str(resolved_config))
        _prepare_review_items(items, current)
        ai_status = AIReviewClient(current.ai_review, current.database_path).public_status()
        metrics = {
            "total": len(items),
            "ai": sum(item.get("analysis_source") == "ai" for item in items),
            "rules": sum(item.get("analysis_source") != "ai" for item in items),
            "vision": sum(
                item.get("ai_analysis", {}).get("vision_status") in {"completed", "cached"}
                for item in items
            ),
        }
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 8
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, total_pages)
        visible_items = items[(page - 1) * page_size : page * page_size]
        return render_template(
            "ai_analysis.html",
            items=visible_items,
            ai_status=ai_status,
            metrics=metrics,
            selected_source=selected_source,
            selected_risk=selected_risk,
            page=page,
            total_pages=total_pages,
        )

    @app.get("/reviews/<source>/<int:row_id>")
    @login_required
    def review_detail(source: str, row_id: int):
        try:
            item = store.get_review(source, row_id)
        except ValueError:
            abort(404)
        current = GuardConfig.from_file(str(resolved_config))
        _prepare_review_items([item], current)
        return render_template(
            "review_detail.html",
            item=item,
            move_targets=_configured_move_targets(current),
        )

    @app.post("/reviews/<source>/<int:row_id>/resolve")
    @login_required
    def resolve_review(source: str, row_id: int):
        resolution = request.form.get("resolution", "")
        notes = request.form.get("notes", "").strip()[:1000]
        return_to = safe_next(request.form.get("next", ""))
        try:
            store.resolve_review(source, row_id, resolution, "admin", notes, remote_ip())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("处理结果已保存，并进入操作记录。", "success")
        return redirect(return_to or url_for("review_detail", source=source, row_id=row_id))

    @app.post("/reviews/tencent/<int:row_id>/prepare-delete")
    @login_required
    def prepare_delete(row_id: int):
        return execute_delete(row_id)

    @app.get("/reviews/tencent/<int:row_id>/confirm-delete")
    @login_required
    def confirm_delete(row_id: int):
        return redirect(url_for("review_detail", source="tencent", row_id=row_id))

    @app.post("/reviews/tencent/<int:row_id>/delete")
    @login_required
    def execute_delete(row_id: int):
        reason = "管理员在内容审核页直接删除"
        return_to = safe_next(request.form.get("next", ""))
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(return_to or url_for("review_detail", source="tencent", row_id=row_id))
        try:
            store.ensure_current_tencent_review(row_id)
            item = store.get_review("tencent", row_id)
            client = cli_factory()
            delete_result = _delete_tencent_item(client, item)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:500]
            store.record_delete_result(row_id, "admin", "failed", message, reason, remote_ip())
            flash(_public_operation_error(exc), "error")
        else:
            store.record_delete_result(row_id, "admin", "deleted", "", reason, remote_ip())
            if delete_result == "already_missing":
                flash("内容在腾讯频道中已不存在，后台记录已同步为已删除。", "success")
            else:
                flash("内容已从腾讯频道删除。", "success")
        return redirect(return_to or url_for("review_detail", source="tencent", row_id=row_id))

    @app.post("/reviews/bulk-delete/prepare")
    @login_required
    def prepare_bulk_delete():
        return execute_bulk_delete()

    @app.get("/reviews/bulk-delete/confirm")
    @login_required
    def confirm_bulk_delete():
        return redirect(url_for("reviews"))

    @app.post("/reviews/bulk-delete")
    @login_required
    def execute_bulk_delete():
        row_ids = _selected_row_ids(request.form.getlist("review_ids"))
        reason = "管理员在内容审核页批量删除"
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("reviews"))
        if not row_ids:
            flash("请先勾选要删除的内容。", "error")
            return redirect(url_for("reviews"))
        if len(row_ids) > 20:
            flash("每次最多删除 20 条内容。", "error")
            return redirect(url_for("reviews"))

        client = cli_factory()
        deleted = 0
        failed = 0
        stopped_for_rate_limit = False
        for row_id in row_ids:
            try:
                store.ensure_current_tencent_review(row_id)
                item = store.get_review("tencent", row_id)
                _delete_tencent_item(client, item)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                store.record_delete_result(
                    row_id, "admin", "failed", message, reason, remote_ip()
                )
                failed += 1
                if _is_rate_limit_error(exc):
                    stopped_for_rate_limit = True
                    break
            else:
                store.record_delete_result(
                    row_id, "admin", "deleted", "", reason, remote_ip()
                )
                deleted += 1

        untouched = len(row_ids) - deleted - failed
        if stopped_for_rate_limit:
            flash(
                f"批量删除已因平台频率限制停止：成功 {deleted} 条、失败 {failed} 条、未执行 {untouched} 条。",
                "error",
            )
        elif failed:
            flash(f"批量删除完成：成功 {deleted} 条、失败 {failed} 条。", "error")
        else:
            flash(
                f"批量删除完成：成功 {deleted} 条、失败 0 条。",
                "success",
            )
        return redirect(url_for("reviews", status=""))

    @app.post("/reviews/bulk-move/prepare")
    @login_required
    def prepare_bulk_move():
        return execute_bulk_move()

    @app.get("/reviews/bulk-move/confirm")
    @login_required
    def confirm_bulk_move():
        return redirect(url_for("reviews"))

    @app.post("/reviews/bulk-move")
    @login_required
    def execute_bulk_move():
        row_ids = _selected_row_ids(request.form.getlist("review_ids"))
        current = GuardConfig.from_file(str(resolved_config))
        target = _find_move_target(current, request.form.get("move_target", ""))
        reason = "管理员在内容审核页直接调整栏目"
        return_to = safe_next(request.form.get("next", ""))
        if not row_ids or target is None:
            flash("请选择内容和目标栏目。", "error")
            return redirect(return_to or url_for("reviews"))
        try:
            items = [store.get_review("tencent", row_id) for row_id in row_ids]
            if len(row_ids) > 20:
                raise ValueError("每次最多移动 20 条内容")
            if any(item["guild_id"] != target["guild_id"] for item in items):
                raise ValueError("所选内容必须属于目标栏目的同一个频道")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(return_to or url_for("reviews", status=""))

        client = cli_factory()
        moved = 0
        failed = 0
        stopped = False
        for row_id in row_ids:
            item = next(value for value in items if value["id"] == row_id)
            try:
                client.move_feed(
                    item["guild_id"], item["channel_id"], target["channel_id"], item["item_id"]
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                store.record_move_result(
                    row_id, "admin", "failed", target["channel_id"],
                    target["section"], error, reason, remote_ip()
                )
                failed += 1
                if _is_rate_limit_error(exc):
                    stopped = True
                    break
            else:
                store.record_move_result(
                    row_id, "admin", "moved", target["channel_id"],
                    target["section"], "", reason, remote_ip(), target["label"]
                )
                moved += 1
        untouched = len(row_ids) - moved - failed
        if stopped:
            flash(
                f"栏目调整因平台频率限制停止：成功 {moved} 条、失败 {failed} 条、未执行 {untouched} 条。",
                "error",
            )
        elif failed:
            flash(f"栏目调整完成：成功 {moved} 条、失败 {failed} 条。", "error")
        else:
            flash(f"已将 {moved} 条内容移动到“{target['label']}”。", "success")
        return redirect(return_to or url_for("reviews", status=""))

    @app.get("/duplicates")
    @login_required
    def duplicates():
        items = [_duplicate_view(item) for item in store.duplicates(limit=300)]
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 20
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, total_pages)
        return render_template(
            "duplicates.html",
            items=items[(page - 1) * page_size : page * page_size],
            page=page,
            total_pages=total_pages,
        )

    @app.get("/audit")
    @login_required
    def audit():
        selected_group = request.args.get("group", "")
        if selected_group not in {"", "content", "channel", "settings", "system"}:
            selected_group = ""
        items = [_audit_view(item) for item in store.audit_log(limit=300)]
        group_counts = {
            key: sum(item["group"] == key for item in items)
            for key in ("content", "channel", "settings", "system")
        }
        if selected_group:
            items = [item for item in items if item["group"] == selected_group]
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 20
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, total_pages)
        visible_items = items[(page - 1) * page_size : page * page_size]
        return render_template(
            "audit.html",
            items=visible_items,
            selected_group=selected_group,
            group_counts=group_counts,
            page=page,
            total_pages=total_pages,
        )

    @app.get("/official")
    @login_required
    def official_capabilities():
        capabilities = []
        cli_status: Dict[str, Any] = {
            "connected": False,
            "version": "",
            "login": "无法确认",
            "error": "",
        }
        try:
            client = cli_factory()
            capabilities = normalize_index(client.capability_index())
            cli_status["version"] = client.version()
            login_state = client.login_status()
            login_data = login_state.get("data") or {}
            cli_status["connected"] = bool(login_data.get("valid"))
            cli_status["login"] = str(login_data.get("message") or "已连接")
        except (TencentCliError, OSError, AttributeError, ValueError) as exc:
            cli_status["error"] = _public_operation_error(exc)
        current = GuardConfig.from_file(str(resolved_config))
        recent_reviews = store.reviews(status="", limit=250)
        return render_template(
            "official.html",
            groups=grouped_capabilities(capabilities),
            workflows=_official_workflows(capabilities),
            official_sources=_official_sources(current, recent_reviews),
            capability_count=len(capabilities),
            cli_status=cli_status,
            skill_version=OFFICIAL_SKILL_VERSION,
            writes_enabled=official_writes_enabled(),
        )

    @app.route("/official/<domain>/<action>", methods=["GET", "POST"])
    @login_required
    def official_capability(domain: str, action: str):
        result = None
        error = ""
        try:
            client = cli_factory()
            capabilities = normalize_index(client.capability_index())
            capability = next(
                (
                    item
                    for item in capabilities
                    if item["domain"] == domain and item["action"] == action
                ),
                None,
            )
            if capability is None:
                abort(404)
            schema = client.capability_schema(domain, action)
            current = GuardConfig.from_file(str(resolved_config))
            recent_reviews = store.reviews(status="", limit=250)
            submitted_values = request.form if request.method == "POST" else {}
            form_fields = _official_form_fields(schema, current, recent_reviews, submitted_values)
            if request.method == "POST":
                try:
                    parameters = normalize_command_parameters(
                        domain, action, parse_parameters(schema, request.form)
                    )
                    _reject_unsafe_file_parameters(parameters)
                    is_write = bool(capability["is_write"])
                    live = is_write
                    if live:
                        if not official_writes_enabled():
                            raise ValueError("生产环境尚未开启官方写操作")
                    result = safe_payload(
                        client.execute_capability(
                            domain,
                            action,
                            parameters,
                            confirmed=live,
                            dry_run=False,
                        )
                    )
                    store.record_audit(
                        "admin",
                        "official.execute" if live else "official.read",
                        domain,
                        action,
                        {
                            "risk": capability["risk"],
                            "reason": "管理员在频道管理页直接执行" if live else "",
                            "parameters": safe_audit_parameters(parameters),
                            "success": bool(result.get("success", True)) if isinstance(result, dict) else True,
                        },
                        remote_ip(),
                    )
                except (TencentCliError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raw_error = str(exc)[:800]
                    error = _public_operation_error(exc)
                    store.record_audit(
                        "admin",
                        "official.failed",
                        domain,
                        action,
                        {"error": raw_error},
                        remote_ip(),
                    )
            return render_template(
                "official_action.html",
                capability=capability,
                schema=schema,
                form_fields=form_fields,
                result=result,
                result_view=_official_result_view(result),
                error=error,
                writes_enabled=official_writes_enabled(),
                official_sources=_official_sources(current, recent_reviews),
            )
        except (TencentCliError, OSError, AttributeError, ValueError) as exc:
            flash(_public_operation_error(exc), "error")
            return redirect(url_for("official_capabilities"))

    @app.get("/rules")
    @login_required
    def rules():
        raw = editor.snapshot()
        current = GuardConfig.from_file(str(resolved_config))
        return render_template(
            "rules.html",
            raw=raw,
            moderation=raw.get("moderation", {}),
            terms=raw.get("moderation", {}).get("terms", []),
            rules=raw.get("rules", {}),
            ai_review=raw.get("ai_review", {}),
            ai_status=AIReviewClient(current.ai_review, current.database_path).public_status(),
        )

    @app.post("/rules/ai-review")
    @login_required
    def update_ai_review():
        values = dict(request.form)
        values.pop("csrf_token", None)
        values["enabled"] = "enabled" in request.form
        values["include_images"] = "include_images" in request.form
        try:
            version = editor.update_ai_review(values)
            store.record_audit(
                "admin",
                "policy.update",
                "ai_review",
                version,
                {key: value for key, value in values.items() if "key" not in key.casefold()},
                remote_ip(),
            )
            flash("智能判断设置已保存，后续巡检会使用新设置。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.post("/rules/moderation")
    @login_required
    def update_moderation():
        values = dict(request.form)
        values.pop("csrf_token", None)
        for key in {
            "enabled",
            "detect_contact_information",
            "detect_external_links",
            "detect_obfuscated_terms",
        }:
            values[key] = key in request.form
        try:
            version = editor.update_moderation(values)
            store.record_audit("admin", "policy.update", "moderation", version, values, remote_ip())
            flash("处理建议规则已保存，后续巡检会使用新规则。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.post("/rules/keywords")
    @login_required
    def update_keywords():
        values = dict(request.form)
        values.pop("csrf_token", None)
        values["weekly_requires_any_hashtag"] = "weekly_requires_any_hashtag" in request.form
        try:
            version = editor.update_keywords(values)
            store.record_audit("admin", "policy.update", "classification", version, values, remote_ip())
            flash("栏目识别规则已保存，后续巡检会使用新规则。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.post("/rules/terms")
    @login_required
    def add_term():
        values = dict(request.form)
        try:
            version = editor.add_sensitive_term(values)
            safe_values = {key: value for key, value in values.items() if key != "csrf_token"}
            store.record_audit("admin", "term.add", "policy", version, safe_values, remote_ip())
            flash("关注词已添加，后续巡检会自动检查。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.post("/rules/terms/<int:index>/delete")
    @login_required
    def delete_term(index: int):
        try:
            version = editor.delete_sensitive_term(index)
            store.record_audit("admin", "term.delete", "policy", version, {"index": index}, remote_ip())
            flash("关注词已移除。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.get("/channels")
    @login_required
    def channels():
        raw = editor.snapshot()
        return render_template(
            "channels.html",
            boards=raw.get("board_policies", {}),
            channels=raw.get("tencent_channels", []),
            board_options=_board_options(raw),
            sections=[section for section in Section if section is not Section.UNCLASSIFIED],
        )

    @app.post("/channels/boards")
    @login_required
    def upsert_board():
        values: Dict[str, Any] = dict(request.form)
        values["expected_sections"] = request.form.getlist("expected_sections")
        values["require_hashtag"] = "require_hashtag" in request.form
        values["allow_external_links"] = "allow_external_links" in request.form
        try:
            version = editor.upsert_board(values)
            store.record_audit(
                "admin",
                "board.upsert",
                "channel",
                str(values.get("channel_id", "")),
                {"policy_version": version, "name": values.get("name", "")},
                remote_ip(),
            )
            flash("栏目规则已保存，后续巡检会使用新规则。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("channels"))

    @app.post("/channels/boards/<channel_id>/delete")
    @login_required
    def delete_board(channel_id: str):
        try:
            version = editor.delete_board(channel_id)
            store.record_audit("admin", "board.delete", "channel", channel_id, {"policy_version": version}, remote_ip())
            flash("栏目规则已删除。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("channels"))

    @app.route("/test", methods=["GET", "POST"])
    @login_required
    def test_content():
        result = None
        current = GuardConfig.from_file(str(resolved_config))
        if request.method == "POST":
            item = IncomingContent(
                platform_item_id=f"admin-test-{int(time.time() * 1000)}",
                kind=ItemKind.FORUM_THREAD,
                guild_id="admin-test",
                channel_id=request.form.get("channel_id", "admin-test"),
                author_id="admin-test",
                title=request.form.get("title", "")[:500],
                body=request.form.get("body", "")[:20000],
                media_urls=tuple(
                    value.strip()
                    for value in request.form.get("media_urls", "").splitlines()
                    if value.strip()
                )[:20],
            )
            classification = ContentClassifier(current).classify(item)
            rule_moderation = ModerationEngine(current).evaluate(item, classification)
            moderation = rule_moderation
            ai_result = None
            ai_error = ""
            if current.ai_review.enabled:
                try:
                    ai_result = AIReviewClient(
                        current.ai_review, current.database_path
                    ).review(
                        item,
                        current.board_policies.get(item.channel_id),
                        classification,
                        rule_moderation,
                    )
                    classification, moderation = fuse_ai_review(
                        classification, rule_moderation, ai_result, current.ai_review
                    )
                except AIReviewUnavailable as exc:
                    ai_error = str(exc)
            result = {
                "classification": classification,
                "moderation": moderation,
                "rule_moderation": rule_moderation,
                "ai_review": ai_result,
                "ai_error": ai_error,
            }
            store.record_audit(
                "admin",
                "content.test",
                "channel",
                item.channel_id,
                {"section": classification.section.value, "risk_score": moderation.risk_score},
                remote_ip(),
            )
        raw = editor.snapshot()
        return render_template(
            "test.html",
            result=result,
            board_options=_board_options(raw),
        )

    @app.post("/scan")
    @login_required
    def scan_now():
        current = GuardConfig.from_file(str(resolved_config))
        lease = ScanLock(current.database_path)
        if not lease.acquire():
            message = "内容同步或 AI 巡检正在运行，请完成后再试。"
            if _wants_json():
                return jsonify({"ok": False, "status": "busy", "message": message}), 409
            flash(message, "error")
            return redirect(url_for("dashboard"))

        job_id = secrets.token_urlsafe(18)
        requester_ip = remote_ip()
        scan_status_store.start(job_id)

        def run_scan() -> None:
            try:
                monitor = TencentChannelMonitor(
                    current,
                    None,
                    progress_callback=lambda percent, phase, message: scan_status_store.update(
                        job_id,
                        percent=percent,
                        phase=phase,
                        message=message,
                    ),
                )
                report = monitor.review_cached_once()
                summary = report.public_summary()
                store.record_audit(
                    "admin",
                    "scan.run",
                    "tencent",
                    "all",
                    {
                        "job_id": job_id,
                        "scanned_feeds": summary["scanned_feeds"],
                        "new_feeds": summary.get("new_feeds", 0),
                        "updated_feeds": summary.get("updated_feeds", 0),
                        "cached_feeds": summary.get("cached_feeds", 0),
                        "duplicates": summary["duplicates"],
                    },
                    requester_ip,
                )
                scan_status_store.complete(job_id, summary)
            except Exception as exc:
                store.record_audit(
                    "admin",
                    "scan.failed",
                    "tencent",
                    "all",
                    {"job_id": job_id, "error": str(exc)[:500]},
                    requester_ip,
                )
                scan_status_store.fail(job_id, _public_operation_error(exc))
            finally:
                lease.release()

        threading.Thread(
            target=run_scan,
            name=f"qq-guard-scan-{job_id[:8]}",
            daemon=True,
        ).start()
        if _wants_json():
            return jsonify(
                {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "status_url": url_for("scan_status", job_id=job_id),
                }
            ), 202
        flash("巡检已开始，可在工作台查看进度。", "success")
        return redirect(url_for("dashboard", scan_job=job_id))

    @app.post("/sync")
    @login_required
    def sync_now():
        current = GuardConfig.from_file(str(resolved_config))
        lease = ScanLock(current.database_path)
        if not lease.acquire():
            message = "内容同步或 AI 巡检正在运行，请完成后再试。"
            if _wants_json():
                return jsonify({"ok": False, "status": "busy", "message": message}), 409
            flash(message, "error")
            return redirect(url_for("contents"))

        job_id = secrets.token_urlsafe(18)
        requester_ip = remote_ip()
        sync_status_store.start(job_id)

        def run_sync() -> None:
            try:
                monitor = TencentChannelMonitor(
                    current,
                    cli_factory(),
                    progress_callback=lambda percent, phase, message: sync_status_store.update(
                        job_id,
                        percent=percent,
                        phase=phase,
                        message=message,
                    ),
                )
                report = monitor.sync_once()
                summary = report.public_summary()
                store.record_audit(
                    "admin",
                    "content.sync",
                    "tencent",
                    "all",
                    {"job_id": job_id, **summary},
                    requester_ip,
                )
                sync_status_store.complete(job_id, summary)
            except Exception as exc:
                store.record_audit(
                    "admin",
                    "content.sync_failed",
                    "tencent",
                    "all",
                    {"job_id": job_id, "error": str(exc)[:500]},
                    requester_ip,
                )
                sync_status_store.fail(job_id, _public_operation_error(exc))
            finally:
                lease.release()

        threading.Thread(
            target=run_sync,
            name=f"qq-guard-sync-{job_id[:8]}",
            daemon=True,
        ).start()
        if _wants_json():
            return jsonify(
                {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "status_url": url_for("sync_status", job_id=job_id),
                }
            ), 202
        flash("内容同步已开始，本次不会执行 AI 分析。", "success")
        return redirect(url_for("contents", sync_job=job_id))

    @app.get("/scan/status/<job_id>")
    @login_required
    def scan_status(job_id: str):
        if not job_id or len(job_id) > 80:
            abort(404)
        state = scan_status_store.read(job_id)
        if state is None:
            abort(404)
        state["results_url"] = url_for("contents")
        return jsonify(state)

    @app.get("/sync/status/<job_id>")
    @login_required
    def sync_status(job_id: str):
        if not job_id or len(job_id) > 80:
            abort(404)
        state = sync_status_store.read(job_id)
        if state is None:
            abort(404)
        state["results_url"] = url_for("contents")
        return jsonify(state)

    return app


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def safe_next(value: str) -> str:
    value = str(value or "")
    if value.startswith("/") and not value.startswith("//"):
        return value
    return ""


def manual_delete_enabled() -> bool:
    return os.environ.get("QQ_GUARD_MANUAL_DELETE_ENABLED", "false").casefold() == "true"


def official_writes_enabled() -> bool:
    return os.environ.get("QQ_GUARD_OFFICIAL_WRITES_ENABLED", "false").casefold() == "true"


def _reject_unsafe_file_parameters(parameters: Dict[str, Any]) -> None:
    for key in parameters:
        normalized = str(key).casefold()
        if "path" in normalized or normalized.endswith("file") or normalized.endswith("files"):
            raise ValueError("涉及服务器文件的能力暂不接受路径输入，请使用后续受控上传入口")


def _selected_row_ids(values) -> list:
    row_ids = []
    for value in values:
        try:
            row_id = int(value)
        except (TypeError, ValueError):
            continue
        if row_id > 0 and row_id not in row_ids:
            row_ids.append(row_id)
    return row_ids


def _configured_move_targets(config: GuardConfig) -> list:
    targets = []
    seen = set()
    for settings in config.tencent_channels:
        guild_name = settings.name or settings.guild_id
        for section, channel_id in settings.channels.items():
            key = f"{settings.guild_id}:{channel_id}:{section.value}"
            if key not in seen:
                targets.append(
                    {
                        "key": key,
                        "guild_id": settings.guild_id,
                        "guild_name": guild_name,
                        "channel_id": channel_id,
                        "section": section.value,
                        "label": section.display_name,
                    }
                )
                seen.add(key)
        for channel_name, channel_id in settings.auto_classify_channels.items():
            board = config.board_policies.get(channel_id)
            sections = board.expected_sections if board and board.expected_sections else (Section.UNCLASSIFIED,)
            for section in sections:
                key = f"{settings.guild_id}:{channel_id}:{section.value}"
                if key not in seen:
                    targets.append(
                        {
                            "key": key,
                            "guild_id": settings.guild_id,
                            "guild_name": guild_name,
                            "channel_id": channel_id,
                            "section": section.value,
                            "label": f"{channel_name} · {section.display_name}",
                        }
                    )
                    seen.add(key)
    return targets


def _official_workflows(capabilities) -> list:
    by_action = {item.get("action"): item for item in capabilities}
    definitions = [
        (
            "review",
            "查清楚",
            "查看帖子、评论和频道资料，先把事实核对完整。",
            ("get-feed-detail", "get-channel-timeline-feeds", "get-feed-comments", "get-guild-channel-list"),
        ),
        (
            "move",
            "改内容",
            "编辑、移动、置顶、设精华，适合处理栏目和内容呈现问题。",
            ("move-feed", "alter-feed", "top-feed", "set-feed-essence", "push-essence-feed"),
        ),
        (
            "publish",
            "发内容",
            "发布帖子、评论或回复，填写所需内容后直接提交并保留记录。",
            ("publish-feed", "do-comment", "do-reply", "quick-publish"),
        ),
        (
            "risk",
            "处理风险",
            "删除、禁言、移出成员等操作提交后直接执行，并保留操作记录。",
            ("del-feed", "delete-and-mute", "modify-member-shut-up", "kick-guild-member"),
        ),
        (
            "member",
            "管成员",
            "查询成员、管理员和角色权限，用于人工审核前后补充判断。",
            ("get-user-info", "get-guild-member-list", "guild-member-search", "add-admin", "remove-admin"),
        ),
        (
            "notice",
            "看通知",
            "查看通知和私信状态，日常内容仍会通过巡检自动同步。",
            ("get-recent-notices", "check-new-notices", "push-group-dm-msg", "notices-status"),
        ),
    ]
    workflows = []
    for key, title, description, actions in definitions:
        items = [by_action[action] for action in actions if action in by_action]
        if items:
            workflows.append(
                {
                    "key": key,
                    "title": title,
                    "description": description,
                    "items": items[:4],
                }
            )
    return workflows


def _official_sources(config: GuardConfig, recent_reviews) -> Dict[str, int]:
    guilds = 0
    channels = 0
    feeds = 0
    authors = 0
    seen_guilds = set()
    for settings in config.tencent_channels:
        if settings.guild_id not in seen_guilds:
            seen_guilds.add(settings.guild_id)
            guilds += 1
        channels += len(settings.channels) + len(settings.auto_classify_channels)
    seen_feeds = set()
    seen_authors = set()
    for item in recent_reviews:
        if str(item.get("source") or "") != "tencent":
            continue
        feed_id = str(item.get("item_id") or "").strip()
        if feed_id and feed_id not in seen_feeds:
            seen_feeds.add(feed_id)
            feeds += 1
        author_id = str(item.get("author_id") or "").strip()
        if author_id and author_id not in seen_authors:
            seen_authors.add(author_id)
            authors += 1
    return {"guilds": guilds, "channels": channels, "feeds": feeds, "authors": authors}


def _prepare_review_items(items: list, config: GuardConfig) -> None:
    suggestions, _ = placement_review(items, config)
    suggestion_by_id = {item["id"]: item for item in suggestions}
    for item in items:
        item["author_display"] = "频道成员" if item.get("author_id") else "未返回作者信息"
        item["placement_suggestion"] = suggestion_by_id.get(item["id"])
        item["has_conflict"] = (
            item.get("risk_level") in {"high", "critical"}
            and item.get("action") == "allow"
        ) or (
            item.get("risk_level") == "low"
            and item.get("action") == "delete_candidate"
        )
        if item["has_conflict"]:
            item["ui_queue"] = "review"
        elif item.get("placement_suggestion"):
            item["ui_queue"] = "placement"
        elif item.get("action") == "delete_candidate" and _has_deletion_evidence(item):
            item["ui_queue"] = "delete"
        elif item.get("action") == "delete_candidate":
            item["ui_queue"] = "review"
        elif config.ai_review.enabled and (
            item.get("analysis_source") != "ai"
            or item.get("ai_status") in {"fallback", "failed", "disabled", "not_requested"}
        ):
            item["ui_queue"] = "incomplete"
        elif item.get("action") == "review":
            item["ui_queue"] = "review"
        else:
            item["ui_queue"] = "allow"

        summary = str((item.get("ai_analysis") or {}).get("summary") or "").strip()
        first_reason = next(
            (
                str(reason.get("message") or "").strip()
                for reason in item.get("reasons", [])
                if isinstance(reason, dict) and reason.get("message")
            ),
            "",
        )
        if item["has_conflict"]:
            item["ui_summary"] = "系统自己拿不准，先查看完整内容"
        elif item["ui_queue"] == "incomplete":
            item["ui_summary"] = _public_ai_error(item.get("ai_error"))
        elif item["ui_queue"] == "placement":
            target = item["placement_suggestion"]["move_target"]["label"]
            item["ui_summary"] = f"内容更适合放到“{target}”"
        elif item["ui_queue"] == "delete":
            item["ui_summary"] = summary or first_reason or "发现明确高风险信号"
        else:
            item["ui_summary"] = summary or first_reason or "没有发现明显问题"
        item["guidance"] = _review_guidance(item)
        _set_review_risk_display(item)


def _board_options(raw: Dict[str, Any]) -> list:
    policies = raw.get("board_policies", {}) or {}
    options = []
    seen = set()
    for channel in raw.get("tencent_channels", []) or []:
        guild_name = str(channel.get("name") or "未命名频道")
        for section, channel_id in (channel.get("channels", {}) or {}).items():
            value = str(channel_id)
            if not value or value in seen:
                continue
            seen.add(value)
            board = policies.get(value, {}) or {}
            label = str(board.get("name") or f"{guild_name} · {SECTION_LABELS.get(section, section)}")
            options.append({"value": value, "label": label})
        for name, channel_id in (channel.get("auto_classify_channels", {}) or {}).items():
            value = str(channel_id)
            if not value or value in seen:
                continue
            seen.add(value)
            board = policies.get(value, {}) or {}
            label = str(board.get("name") or f"{guild_name} · {name}")
            options.append({"value": value, "label": label})
    for channel_id, board in policies.items():
        value = str(channel_id)
        if value in seen:
            continue
        options.append({"value": value, "label": str((board or {}).get("name") or "未命名栏目")})
    return options


def _duplicate_view(item: Dict[str, Any]) -> Dict[str, str]:
    status = str(item.get("delete_status") or "")
    result = {
        "detected_only": "已发现，等待人工处理",
        "deleted": "已删除后发布的重复内容",
        "failed": "删除未完成",
    }.get(status, "已记录")
    return {
        "guild_name": str(item.get("guild_name") or "未命名频道"),
        "section": str(item.get("section") or "unclassified"),
        "result": result,
        "error": _public_operation_error(item.get("error")) if status == "failed" else "",
        "created_at": str(item.get("created_at") or ""),
    }


def _audit_view(item: Dict[str, Any]) -> Dict[str, str]:
    action = str(item.get("action") or "")
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    group = _audit_group(action)
    title = {
        "auth.login": "登录后台",
        "auth.logout": "退出后台",
        "auth.failed": "登录失败",
        "auth.blocked": "登录暂时受限",
        "review.resolve": "完成内容审核",
        "delete.prepare": "旧版删除准备记录",
        "bulk_delete.prepare": "旧版批量删除准备记录",
        "delete.reauth_failed": "删除密码验证失败",
        "delete.bulk_reauth_failed": "批量删除密码验证失败",
        "delete.execute": "删除内容",
        "move.execute": "调整内容栏目",
        "move.bulk_reauth_failed": "栏目调整密码验证失败",
        "official.read": "查看频道资料",
        "official.preview": "检查频道操作",
        "official.execute": "执行频道操作",
        "official.failed": "频道操作失败",
        "policy.update": "更新审核规则",
        "term.add": "添加敏感内容词",
        "term.delete": "移除敏感内容词",
        "board.upsert": "保存栏目规则",
        "board.delete": "删除栏目规则",
        "content.test": "检查一条内容",
        "scan.run": "完成频道巡检",
        "scan.failed": "频道巡检未完成",
        "delete.duplicate_result_ignored": "忽略重复删除结果",
    }.get(action, "后台操作")

    summary = "操作记录已保存"
    result = "已记录"
    tone = "neutral"
    if action == "review.resolve":
        resolution = {
            "approved": "保留内容",
            "rejected": "记录为不通过",
            "ignored": "稍后处理",
            "deleted": "已删除",
        }.get(str(details.get("resolution") or ""), "完成处理")
        note = str(details.get("notes") or "").strip()
        summary = f"处理结果：{resolution}" + (f"；备注：{note}" if note else "")
        result = "已完成"
        tone = "success"
    elif action in {"delete.prepare", "bulk_delete.prepare"}:
        summary = "旧版流程留下的准备记录，当时没有修改频道内容"
        result = "历史记录"
    elif action == "delete.execute":
        if details.get("status") == "deleted":
            summary = "频道内容已删除，删除原因已留存"
            result = "已删除"
            tone = "danger"
        else:
            summary = _public_operation_error(details.get("error"))
            result = "未完成"
            tone = "warning"
    elif action == "move.execute":
        if details.get("status") == "moved":
            summary = "内容已移动到管理员确认的目标栏目"
            result = "已完成"
            tone = "success"
        else:
            summary = _public_operation_error(details.get("error"))
            result = "未完成"
            tone = "warning"
    elif action.startswith("official."):
        operation = _official_action_label(str(item.get("target_id") or ""))
        if action == "official.read":
            summary = f"已通过腾讯频道连接查看：{operation}"
            result = "已查看"
            tone = "success"
        elif action == "official.preview":
            summary = f"已检查“{operation}”的填写内容，没有修改频道"
            result = "仅检查"
        elif action == "official.execute":
            summary = f"已执行频道操作：{operation}"
            result = "已完成"
            tone = "success"
        else:
            summary = _public_operation_error(details.get("error"))
            result = "未完成"
            tone = "warning"
    elif action == "scan.run":
        summary = (
            f"读取 {int(details.get('scanned_feeds') or 0)} 条内容，"
            f"新发现 {int(details.get('new_feeds') or 0)} 条，"
            f"更新 {int(details.get('updated_feeds') or 0)} 条"
        )
        result = "已完成"
        tone = "success"
    elif action == "scan.failed":
        summary = _public_operation_error(details.get("error"))
        result = "未完成"
        tone = "warning"
    elif action in {"auth.failed", "auth.blocked", "delete.reauth_failed", "delete.bulk_reauth_failed", "move.bulk_reauth_failed"}:
        summary = "密码或登录验证没有通过，没有执行后续操作"
        result = "未执行"
        tone = "warning"
    elif action == "auth.login":
        summary = "管理员已进入后台"
        result = "成功"
        tone = "success"
    elif action == "auth.logout":
        summary = "管理员已安全退出后台"
        result = "成功"
    elif group == "settings":
        summary = "设置已保存，后续巡检会使用新规则"
        result = "已保存"
        tone = "success"

    return {
        "created_at": str(item.get("created_at") or ""),
        "actor": "管理员" if item.get("actor") == "admin" else "系统",
        "title": title,
        "summary": summary,
        "result": result,
        "tone": tone,
        "group": group,
    }


def _audit_group(action: str) -> str:
    if action.startswith(("review.", "delete.", "move.")) or action == "bulk_delete.prepare":
        return "content"
    if action.startswith("official."):
        return "channel"
    if action.startswith(("policy.", "term.", "board.", "content.test")):
        return "settings"
    return "system"


def _official_action_label(action: str) -> str:
    return {
        "get-feed-detail": "查看帖子详情",
        "get-channel-timeline-feeds": "查看栏目帖子",
        "get-feed-comments": "查看帖子评论",
        "get-guild-channel-list": "查看栏目列表",
        "move-feed": "移动帖子",
        "alter-feed": "编辑帖子",
        "top-feed": "设置帖子置顶",
        "set-feed-essence": "设置精华",
        "publish-feed": "发布帖子",
        "do-comment": "处理评论",
        "do-reply": "处理回复",
        "del-feed": "删除帖子",
        "modify-member-shut-up": "设置成员禁言",
        "kick-guild-member": "移出成员",
    }.get(action, "频道管理操作")


def _public_operation_error(value: Any) -> str:
    message = str(value or "").strip().casefold()
    if _is_missing_content_error(value):
        return "这条内容在腾讯频道中已不存在"
    if any(token in message for token in ("retcode=153", "频率上限", "rate limit")):
        return "腾讯平台当前请求较多，本次操作未完成，请稍后再试"
    if any(token in message for token in ("invalid ai token", "retcode=8011", "100051")):
        return "QQ 频道连接已失效，本次操作未完成，请联系管理员重新连接"
    if "password" in message or "密码" in message:
        return "管理员密码验证没有通过，没有执行操作"
    return "本次操作没有完成，请稍后重试"


def _official_result_view(result: Any) -> Dict[str, Any]:
    if result is None:
        return {"success": False, "title": "", "rows": [], "count": None}
    if not isinstance(result, dict):
        return {"success": True, "title": "操作已完成", "rows": [], "count": None}
    success = bool(result.get("success", True))
    data = result.get("data", result)
    rows = []
    count = None
    if isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        for key, value in data.items():
            if key.casefold().replace("-", "_") in {
                "raw",
                "token",
                "cookie",
                "cursor",
                "attach_info",
                "error",
                "retcode",
                "ret_code",
                "code",
                "request_id",
                "trace_id",
            }:
                continue
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                rows.append({"label": _public_result_label(str(key)), "value": str(value)})
            elif isinstance(value, list):
                rows.append({"label": _public_result_label(str(key)), "value": f"{len(value)} 条"})
            if len(rows) >= 10:
                break
    message = str(result.get("message") or "").strip()
    title = "操作已完成" if success else "操作未完成"
    if not success:
        title = _public_operation_error(message or data)
        rows = []
        count = None
    elif message and len(message) <= 120 and not any(
        token in message.casefold() for token in ("retcode", "request_id", "trace_id")
    ):
        title = message
    return {"success": success, "title": title, "rows": rows, "count": count}


def _public_result_label(value: str) -> str:
    normalized = value.casefold().replace("-", "_")
    return {
        "title": "标题",
        "name": "名称",
        "nickname": "昵称",
        "content": "内容",
        "text": "文字",
        "description": "说明",
        "guild_name": "频道",
        "channel_name": "栏目",
        "status": "状态",
        "message": "提示",
        "total": "总数",
        "count": "数量",
        "items": "内容数量",
        "feeds": "帖子数量",
        "comments": "评论数量",
        "members": "成员数量",
    }.get(normalized, "返回信息")


def _official_form_fields(schema: Dict[str, Any], config: GuardConfig, recent_reviews, submitted_values) -> list:
    guild_options = _official_guild_options(config, recent_reviews)
    channel_options = _official_channel_options(config, recent_reviews)
    feed_options = _official_feed_options(recent_reviews)
    author_options = _official_author_options(recent_reviews)
    fields = []
    for flag in schema.get("flags") or []:
        if not isinstance(flag, dict):
            continue
        flag_name = str(flag.get("name") or "").strip()
        if not flag_name or flag_name in {"yes", "json", "dry-run", "verbose", "log-level"}:
            continue
        form_name = f"param__{flag_name}"
        value = str(submitted_values.get(form_name, flag.get("default", "")) or "").strip()
        options = []
        source_hint = ""
        input_kind = "text"
        if flag.get("enum"):
            options = [{"value": str(option), "label": str(option)} for option in flag.get("enum") or []]
            input_kind = "select"
            source_hint = "从可用选项中选择"
        else:
            normalized = flag_name.casefold().replace("_", "-")
            if "guild" in normalized:
                options = guild_options
                input_kind = "select"
                source_hint = "来自当前接入频道和最近巡检"
            elif "channel" in normalized or "board" in normalized:
                options = channel_options
                input_kind = "select"
                source_hint = "来自当前栏目配置和最近巡检"
            elif any(token in normalized for token in ("feed", "post", "thread", "content")):
                options = feed_options
                input_kind = "select"
                source_hint = "来自最近巡检记录"
            elif any(token in normalized for token in ("user", "member", "author", "admin", "role")):
                options = author_options
                input_kind = "select"
                source_hint = "来自最近帖子作者和频道成员候选"
            elif any(token in normalized for token in ("comment", "reply")):
                source_hint = "这类互动项当前没有后台缓存，先手填，后续可继续补自动同步"
        value_type = str(flag.get("type") or "string").casefold()
        if not options and value_type in {"json", "object", "array", "stringarray", "strings"}:
            input_kind = "textarea"
        fields.append(
            {
                "name": flag_name,
                "required": bool(flag.get("required")),
                "description": _official_field_label(
                    flag_name, str(flag.get("description") or flag_name)
                ),
                "value": value,
                "input_kind": input_kind,
                "options": options,
                "source_hint": source_hint,
                "type": value_type,
                "placeholder": _official_placeholder(flag_name, value_type),
            }
        )
    return fields


def _official_field_label(name: str, description: str) -> str:
    normalized = str(name or "").casefold().replace("_", "-")
    if "guild-id" in normalized:
        return "频道"
    if "channel-id" in normalized or "board-id" in normalized:
        if any(token in normalized for token in ("target", "to-", "dest")):
            return "目标栏目"
        if any(token in normalized for token in ("source", "from-", "current", "original")):
            return "当前栏目"
        return "栏目"
    if any(token in normalized for token in ("feed-id", "post-id", "thread-id")):
        return "帖子"
    if "comment-id" in normalized:
        return "评论"
    if "reply-id" in normalized:
        return "回复"
    if "author-id" in normalized:
        return "作者"
    if "user-id" in normalized or "member-id" in normalized:
        return "成员"
    return description.replace(" ID", "编号")


def _official_guild_options(config: GuardConfig, recent_reviews) -> list:
    options = []
    seen = set()
    for settings in config.tencent_channels:
        if settings.guild_id in seen:
            continue
        seen.add(settings.guild_id)
        options.append(
            {
                "value": settings.guild_id,
                "label": settings.name or f"已接入频道 {len(options) + 1}",
            }
        )
    for item in recent_reviews:
        if str(item.get("source") or "") != "tencent":
            continue
        guild_id = str(item.get("guild_id") or "").strip()
        if not guild_id or guild_id in seen:
            continue
        seen.add(guild_id)
        options.append(
            {
                "value": guild_id,
                "label": item.get("guild_name") or f"已同步频道 {len(options) + 1}",
            }
        )
    return options


def _official_channel_options(config: GuardConfig, recent_reviews) -> list:
    options = []
    seen = set()
    for target in _configured_move_targets(config):
        key = (target["guild_id"], target["channel_id"])
        if key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "value": target["channel_id"],
                "label": f"{target['guild_name']} · {target['label']}",
            }
        )
    for item in recent_reviews:
        if str(item.get("source") or "") != "tencent":
            continue
        key = (str(item.get("guild_id") or ""), str(item.get("channel_id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "value": key[1],
                "label": f"{item.get('guild_name') or '未命名频道'} · {SECTION_LABELS.get(item.get('section'), item.get('section'))}",
            }
        )
    return options


def _official_feed_options(recent_reviews) -> list:
    options = []
    seen = set()
    for item in recent_reviews:
        if str(item.get("source") or "") != "tencent":
            continue
        feed_id = str(item.get("item_id") or "").strip()
        if not feed_id or feed_id in seen:
            continue
        seen.add(feed_id)
        title = str(item.get("title") or item.get("body") or feed_id).strip()
        if len(title) > 42:
            title = title[:42] + "…"
        section_name = SECTION_LABELS.get(item.get("section"), item.get("section"))
        options.append(
            {
                "value": feed_id,
                "label": f"{item.get('guild_name') or '未命名频道'} · {section_name} · {title}",
            }
        )
    return options


def _official_author_options(recent_reviews) -> list:
    options = []
    seen = set()
    for item in recent_reviews:
        if str(item.get("source") or "") != "tencent":
            continue
        author_id = str(item.get("author_id") or "").strip()
        if not author_id or author_id in seen:
            continue
        seen.add(author_id)
        title = str(item.get("title") or item.get("item_id") or "").strip()
        if len(title) > 30:
            title = title[:30] + "…"
        options.append(
            {
                "value": author_id,
                "label": f"《{title}》的发布者" if title else f"最近内容发布者 {len(options) + 1}",
            }
        )
    return options


def _official_placeholder(name: str, value_type: str) -> str:
    normalized = str(name or "").casefold().replace("_", "-")
    if "guild" in normalized:
        return "请选择频道"
    if "channel" in normalized or "board" in normalized:
        return "请选择栏目"
    if any(token in normalized for token in ("feed", "post", "thread", "content")):
        return "请选择帖子"
    if any(token in normalized for token in ("user", "member", "author")):
        return "请选择成员"
    if any(token in normalized for token in ("comment", "reply")):
        return "请选择或填写评论编号"
    if value_type in {"int", "integer"}:
        return "请输入数字编号"
    return "请输入值"


def _public_ai_error(value: Any) -> str:
    message = str(value or "").strip()
    if not message:
        return "智能判断尚未完成，需要管理员人工核对"
    normalized = message.casefold()
    if any(token in normalized for token in ("api_key", "api 密钥", "tokenhub", "缺少腾讯云")):
        return "智能判断服务尚未连接，需要管理员人工核对"
    if "尚未启用" in message or "未启用" in message:
        return "智能判断服务尚未开启，需要管理员人工核对"
    return "智能判断服务暂时不可用，需要管理员人工核对"


def _plain_ai_text(value: Any) -> str:
    """把模型常见的 Markdown 列表整理成直接可读的短句。"""
    text = str(value or "").strip()
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*+•]\s+|\d+[.)]\s+|#+\s+)", "", line)
        line = re.sub(r"(?:\*\*|__|`)", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _review_guidance(item: Dict[str, Any]) -> Dict[str, str]:
    reasons = [reason for reason in item.get("reasons", []) if isinstance(reason, dict)]
    scored = [reason for reason in reasons if int(reason.get("score") or 0) > 0]
    primary = next(
        (
            reason
            for reason in reasons
            if str(reason.get("code") or "").casefold()
            in {
                "sensitive_term_zh",
                "sensitive_term_en",
                "repeated_characters",
                "possible_gibberish",
                "contact_information_detected",
                "external_link_not_allowed",
            }
        ),
        None,
    ) or next(
        (
            reason
            for reason in reasons
            if reason.get("code")
            not in {
                "ai_score_normalized",
                "ai_action_normalized",
                "section_mismatch",
                "classification_uncertain",
            }
        ),
        reasons[0] if reasons else {},
    )
    issue_type = _reason_type(primary)
    message = str(primary.get("message") or "").strip()
    evidence = str(primary.get("evidence") or "").strip()
    score_parts = [_reason_type(reason) for reason in scored[:3]]
    score_detail = (
        f"主要关注：{'、'.join(score_parts)}"
        if score_parts
        else "当前没有发现需要重点关注的具体问题"
    )

    if item.get("has_conflict"):
        return {
            "status": "需要人工核对",
            "action": "人工核对",
            "issue": "AI发现了需要确认的风险信号",
            "why": message or evidence or "AI分析结果需要管理员确认。",
            "evidence": evidence or message or "未提供与高风险分数相匹配的具体证据",
            "score": score_detail,
        }
    suggestion = item.get("placement_suggestion")
    if suggestion:
        target = suggestion["move_target"]["label"]
        classification_reasons = list((item.get("classification") or {}).get("reasons") or [])
        return {
            "status": "建议调整栏目",
            "action": f"调整到“{target}”",
            "issue": "内容和当前栏目不匹配",
            "why": suggestion.get("placement_reason") or message or "内容语义与当前栏目定位不一致",
            "evidence": evidence or str(classification_reasons[0] if classification_reasons else "AI 分类结果与当前栏目不同"),
            "score": "栏目调整与违规删除分开判断；错投本身不等于违禁内容",
        }
    if item.get("ui_queue") == "incomplete":
        return {
            "status": "需要人工核对",
            "action": "人工核对",
            "issue": "AI分析没有完成",
            "why": _public_ai_error(item.get("ai_error")) if item.get("ai_error") else message or "当前只有基础检查结果，需要管理员核对。",
            "evidence": evidence or "暂无完整的文字和图片判断依据",
            "score": score_detail,
        }
    if item.get("action") == "delete_candidate" and not _has_deletion_evidence(item):
        return {
            "status": "需要人工核对",
            "action": "人工核对",
            "issue": issue_type,
            "why": message or "累计风险分较高，但没有足以直接建议删除的高风险证据",
            "evidence": evidence or "当前只有栏目、内容质量或分类不确定等中等风险信号",
            "score": f"累计分数较高，但删除必须有明确高风险证据；{score_detail}",
        }
    if item.get("action") == "delete_candidate":
        return {
            "status": "建议删除",
            "action": "删除帖子",
            "issue": issue_type,
            "why": message or "检测到高风险违规信号",
            "evidence": evidence or message or "检测到高风险违规信号",
            "score": score_detail,
        }
    if item.get("action") == "review":
        return {
            "status": "需要人工核对",
            "action": "人工核对",
            "issue": issue_type,
            "why": message or "存在需要管理员确认的具体信号",
            "evidence": evidence or message or "存在需要管理员确认的具体信号",
            "score": score_detail,
        }
    return {
        "status": "没有发现问题",
        "action": "确认保留",
        "issue": "未发现明确违规",
        "why": message or str((item.get("ai_analysis") or {}).get("summary") or "规则与智能判断均未发现需要处置的问题"),
        "evidence": evidence or "未命中敏感词、违禁推广、联系方式泄露或栏目硬规则",
        "score": score_detail,
    }


def _has_deletion_evidence(item: Dict[str, Any]) -> bool:
    legacy_high_risk_codes = (
        "sensitive_term",
        "scam",
        "porn",
        "gambl",
        "malware",
        "violence",
    )
    for reason in item.get("reasons", []):
        if not isinstance(reason, dict):
            continue
        if bool(reason.get("auto_delete_eligible")):
            return True
        if str(reason.get("severity") or "").casefold() in {"high", "critical"}:
            return True
        code = str(reason.get("code") or "").casefold()
        if any(token in code for token in legacy_high_risk_codes):
            return True
    return False


def _set_review_risk_display(item: Dict[str, Any]) -> None:
    if item.get("action") == "delete_candidate" and not _has_deletion_evidence(item):
        item["ui_risk_level"] = "medium"
        item["risk_text"] = "需要人工核对"
        return
    risk_level = str(item.get("risk_level") or "low")
    item["ui_risk_level"] = risk_level
    item["risk_text"] = f"{RISK_LABELS.get(risk_level, risk_level)}风险"


def _cn_time(value: Any, format_string: str = "%Y-%m-%d %H:%M") -> str:
    text = str(value or "").strip()
    if not text:
        return "尚未巡检"
    try:
        if text.replace(".", "", 1).isdigit():
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
            return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(format_string)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(format_string)
    except (ValueError, TypeError):
        return text


def _reason_type(reason: Dict[str, Any]) -> str:
    code = str(reason.get("code") or "").casefold()
    category = str(reason.get("category") or "").casefold()
    message = str(reason.get("message") or "").casefold()
    value = f"{code} {category} {message}"
    mappings = (
        (("sensitive_term", "敏感词", "违禁词"), "敏感词/违禁词"),
        (("repeated_characters", "spam", "灌水", "垃圾"), "重复或灌水内容"),
        (("possible_gibberish", "无语义", "乱码"), "疑似无效内容"),
        (("low_information", "quality", "有效信息"), "内容质量"),
        (("scam", "诈骗"), "诈骗风险"),
        (("porn", "色情"), "色情内容"),
        (("gambl", "赌博"), "赌博内容"),
        (("contact", "privacy", "联系方式", "隐私"), "联系方式/隐私"),
        (("external_link", "traffic", "引流", "外链"), "外链/引流"),
        (("section", "hashtag", "栏目", "话题"), "栏目规则"),
        (("duplicate", "重复"), "连续重复"),
        (("vision", "图片"), "图片识别"),
        (("confidence", "置信度"), "分类置信度"),
        (("conflict", "不一致"), "结论冲突"),
    )
    for needles, label in mappings:
        if any(needle in value for needle in needles):
            return label
    return str(reason.get("category") or "其他待核对问题")


def _find_move_target(config: GuardConfig, key: str):
    return next((target for target in _configured_move_targets(config) if target["key"] == key), None)


def _delete_tencent_item(client: TencentCliClient, item: Dict[str, Any]) -> str:
    feed_id = str(item.get("item_id") or item.get("feed_id") or "").strip()
    create_time = str(
        item.get("create_time_raw") or item.get("source_created_at") or ""
    ).strip()
    if not create_time.isdigit():
        try:
            detail = client.get_feed_detail(
                item["guild_id"], item["channel_id"], feed_id
            )
        except TencentCliError as exc:
            if _is_missing_content_error(exc):
                return "already_missing"
            raise
        create_time = str(detail.get("create_time_raw") or detail.get("create_time") or "")
    if not create_time.isdigit():
        raise ValueError("无法确认原帖发布时间，为防止误删已停止操作")
    try:
        client.delete_feed(
            item["guild_id"],
            item["channel_id"],
            feed_id,
            create_time,
            live=True,
        )
    except TencentCliError as delete_error:
        if _is_missing_content_error(delete_error):
            return "already_missing"
        try:
            client.get_feed_detail(item["guild_id"], item["channel_id"], feed_id)
        except TencentCliError as detail_error:
            if _is_missing_content_error(detail_error):
                return "already_missing"
        raise delete_error
    return "deleted"


def _is_missing_content_error(value: Any) -> bool:
    message = str(value or "").casefold()
    return "retcode=10014" in message or "数据已被删除" in message


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "retcode=153" in message or "频率上限" in message or "rate limit" in message


def _wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )


def main() -> None:
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8787")), debug=False)


if __name__ == "__main__":
    main()
