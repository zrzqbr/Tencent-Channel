import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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
from .placement import group_placement_suggestions, placement_review
from .scan_control import ScanLock, ScanStatusStore
from .tencent_cli import TencentCliClient
from .tencent_monitor import TencentChannelMonitor


SECTION_LABELS = {section.value: section.display_name for section in Section}
RISK_LABELS = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
ACTION_LABELS = {
    "allow": "放行",
    "review": "人工复核",
    "delete_candidate": "删除候选",
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
    "completed_text_only": "文字已完成，图片降级",
    "cached": "已完成（缓存）",
    "cached_text_only": "文字已完成（缓存），图片降级",
    "fallback": "已安全降级",
    "disabled": "未启用",
    "not_requested": "未请求",
    "failed": "执行失败",
}
AI_PUBLIC_STATUS_LABELS = {
    "ready": "已连接，可执行",
    "missing_key": "等待配置 API 密钥",
    "disabled": "未启用",
}
VISION_STATUS_LABELS = {
    "ready": "已连接，有图片时执行",
    "completed": "已完成",
    "cached": "已完成（缓存）",
    "not_requested": "未请求",
    "failed": "执行失败，已转人工",
    "missing_key": "等待配置 API 密钥",
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
        return {
            "csrf_token": csrf_token,
            "section_labels": SECTION_LABELS,
            "risk_labels": RISK_LABELS,
            "action_labels": ACTION_LABELS,
            "status_labels": STATUS_LABELS,
            "ai_status_labels": AI_STATUS_LABELS,
            "ai_public_status_labels": AI_PUBLIC_STATUS_LABELS,
            "vision_status_labels": VISION_STATUS_LABELS,
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
        reviews = store.reviews(status="pending", limit=8)
        scans = store.scans(limit=5)
        current = GuardConfig.from_file(str(resolved_config))
        ai_status = AIReviewClient(current.ai_review, current.database_path).public_status()
        return render_template(
            "dashboard.html",
            summary=summary,
            reviews=reviews,
            scans=scans,
            config=current,
            ai_status=ai_status,
        )

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
        suggestions, _ = placement_review(items, current)
        suggestion_by_id = {item["id"]: item for item in suggestions}
        for item in items:
            if item["id"] in suggestion_by_id:
                item["placement_suggestion"] = suggestion_by_id[item["id"]]
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
        return render_template(
            "ai_analysis.html",
            items=items,
            ai_status=ai_status,
            metrics=metrics,
            selected_source=selected_source,
            selected_risk=selected_risk,
        )

    @app.get("/reviews/<source>/<int:row_id>")
    @login_required
    def review_detail(source: str, row_id: int):
        try:
            item = store.get_review(source, row_id)
        except ValueError:
            abort(404)
        return render_template("review_detail.html", item=item)

    @app.post("/reviews/<source>/<int:row_id>/resolve")
    @login_required
    def resolve_review(source: str, row_id: int):
        resolution = request.form.get("resolution", "")
        notes = request.form.get("notes", "").strip()[:1000]
        try:
            store.resolve_review(source, row_id, resolution, "admin", notes, remote_ip())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("审核结论已保存，并写入操作审计。", "success")
        return redirect(url_for("review_detail", source=source, row_id=row_id))

    @app.post("/reviews/tencent/<int:row_id>/prepare-delete")
    @login_required
    def prepare_delete(row_id: int):
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        try:
            token = store.create_delete_challenge(row_id, "admin", remote_ip())
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        session["delete_challenge"] = {"row_id": row_id, "token": token}
        return redirect(url_for("confirm_delete", row_id=row_id))

    @app.get("/reviews/tencent/<int:row_id>/confirm-delete")
    @login_required
    def confirm_delete(row_id: int):
        challenge = session.get("delete_challenge") or {}
        if int(challenge.get("row_id") or 0) != row_id:
            flash("删除确认已失效，请重新发起。", "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        try:
            item = store.get_review("tencent", row_id)
        except ValueError:
            abort(404)
        return render_template("confirm_delete.html", item=item)

    @app.post("/reviews/tencent/<int:row_id>/delete")
    @login_required
    def execute_delete(row_id: int):
        challenge = session.get("delete_challenge", {}) or {}
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "").strip()
        reason = request.form.get("reason", "").strip()[:1000]
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        if not check_password_hash(password_hash, password):
            store.record_audit("admin", "delete.reauth_failed", "tencent", str(row_id), {}, remote_ip())
            flash("管理员密码验证失败，未执行删除。", "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        if confirmation != "confirmed" or len(reason) < 4:
            flash("请勾选删除确认，并填写至少 4 个字符的删除理由。", "error")
            return redirect(url_for("review_detail", source="tencent", row_id=row_id))
        session.pop("delete_challenge", None)
        try:
            if int(challenge.get("row_id") or 0) != row_id:
                raise ValueError("删除确认已失效，请重新发起")
            store.consume_delete_challenge(row_id, "admin", str(challenge.get("token") or ""))
            item = store.get_review("tencent", row_id)
            client = cli_factory()
            _delete_tencent_item(client, item)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:500]
            store.record_delete_result(row_id, "admin", "failed", message, reason, remote_ip())
            flash(f"删除失败：{exc}", "error")
        else:
            store.record_delete_result(row_id, "admin", "deleted", "", reason, remote_ip())
            flash("内容已删除，平台结果和删除理由均已记录。", "success")
        return redirect(url_for("review_detail", source="tencent", row_id=row_id))

    @app.post("/reviews/bulk-delete/prepare")
    @login_required
    def prepare_bulk_delete():
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("reviews"))
        row_ids = _selected_row_ids(request.form.getlist("review_ids"))
        if not row_ids:
            flash("请先勾选要删除的内容。", "error")
            return redirect(url_for("reviews"))
        if len(row_ids) > 20:
            flash("为避免误操作，每次最多删除 20 条内容。", "error")
            return redirect(url_for("reviews"))
        try:
            items = [store.get_review("tencent", row_id) for row_id in row_ids]
            for item in items:
                if item.get("delete_status") == "deleted":
                    raise ValueError(f"内容 {item['item_id']} 已经删除")
                store.ensure_current_tencent_review(item["id"])
            challenges = {
                str(row_id): store.create_delete_challenge(row_id, "admin", remote_ip())
                for row_id in row_ids
            }
            batch_token = store.create_action_challenge("bulk_delete", "admin", remote_ip())
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reviews"))
        session["bulk_delete_challenge"] = {
            "row_ids": row_ids,
            "tokens": challenges,
            "batch_token": batch_token,
            "created_at": int(time.time()),
        }
        return redirect(url_for("confirm_bulk_delete"))

    @app.get("/reviews/bulk-delete/confirm")
    @login_required
    def confirm_bulk_delete():
        challenge = session.get("bulk_delete_challenge") or {}
        row_ids = _selected_row_ids(challenge.get("row_ids") or [])
        if not row_ids or int(challenge.get("created_at") or 0) < int(time.time()) - 600:
            session.pop("bulk_delete_challenge", None)
            flash("批量删除确认已失效，请重新勾选。", "error")
            return redirect(url_for("reviews"))
        try:
            items = [store.get_review("tencent", row_id) for row_id in row_ids]
        except ValueError:
            abort(404)
        return render_template(
            "bulk_confirm_delete.html",
            items=items,
            confirmation_text=f"删除 {len(items)} 条",
        )

    @app.post("/reviews/bulk-delete")
    @login_required
    def execute_bulk_delete():
        challenge = session.get("bulk_delete_challenge", {}) or {}
        row_ids = _selected_row_ids(challenge.get("row_ids") or [])
        tokens = challenge.get("tokens") or {}
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "").strip()
        reason = request.form.get("reason", "").strip()[:1000]
        if not manual_delete_enabled():
            flash("服务器尚未启用人工删除能力。", "error")
            return redirect(url_for("reviews"))
        if not row_ids or int(challenge.get("created_at") or 0) < int(time.time()) - 600:
            flash("批量删除确认已失效，请重新勾选。", "error")
            return redirect(url_for("reviews"))
        if not check_password_hash(password_hash, password):
            store.record_audit(
                "admin",
                "delete.bulk_reauth_failed",
                "tencent",
                ",".join(str(value) for value in row_ids),
                {"count": len(row_ids)},
                remote_ip(),
            )
            flash("管理员密码验证失败，未执行任何删除。", "error")
            return redirect(url_for("reviews"))
        if confirmation != "confirmed" or len(reason) < 4:
            flash("请勾选批量删除确认，并填写至少 4 个字符的删除理由。", "error")
            return redirect(url_for("reviews"))
        session.pop("bulk_delete_challenge", None)

        try:
            store.consume_action_challenge(
                "bulk_delete", "admin", str(challenge.get("batch_token") or "")
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reviews", status=""))

        client = cli_factory()
        deleted = 0
        failed = 0
        stopped_for_rate_limit = False
        for row_id in row_ids:
            try:
                store.consume_delete_challenge(
                    row_id, "admin", str(tokens.get(str(row_id)) or "")
                )
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
                f"批量删除完成：成功 {deleted} 条、失败 0 条。理由和平台结果均已记录。",
                "success",
            )
        return redirect(url_for("reviews", status=""))

    @app.post("/reviews/bulk-move/prepare")
    @login_required
    def prepare_bulk_move():
        row_ids = _selected_row_ids(request.form.getlist("review_ids"))
        if not row_ids:
            flash("请先勾选要调整栏目的内容。", "error")
            return redirect(url_for("reviews"))
        if len(row_ids) > 20:
            flash("每次最多移动 20 条内容。", "error")
            return redirect(url_for("reviews"))
        current = GuardConfig.from_file(str(resolved_config))
        target = _find_move_target(current, request.form.get("move_target", ""))
        if target is None:
            flash("请选择有效的目标栏目。", "error")
            return redirect(url_for("reviews"))
        try:
            items = [store.get_review("tencent", row_id) for row_id in row_ids]
            if any(item["guild_id"] != target["guild_id"] for item in items):
                raise ValueError("所选内容必须属于目标栏目的同一个频道")
            if any(item["channel_id"] == target["channel_id"] for item in items):
                raise ValueError("所选内容中已有帖子位于目标版块，请取消这些帖子后重试")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reviews"))
        session["bulk_move_challenge"] = {
            "row_ids": row_ids,
            "target_key": target["key"],
            "token": store.create_action_challenge("bulk_move", "admin", remote_ip()),
            "created_at": int(time.time()),
        }
        return redirect(url_for("confirm_bulk_move"))

    @app.get("/reviews/bulk-move/confirm")
    @login_required
    def confirm_bulk_move():
        challenge = session.get("bulk_move_challenge") or {}
        row_ids = _selected_row_ids(challenge.get("row_ids") or [])
        current = GuardConfig.from_file(str(resolved_config))
        target = _find_move_target(current, challenge.get("target_key", ""))
        if (
            not row_ids
            or target is None
            or int(challenge.get("created_at") or 0) < int(time.time()) - 600
        ):
            session.pop("bulk_move_challenge", None)
            flash("栏目调整确认已失效，请重新勾选。", "error")
            return redirect(url_for("reviews"))
        try:
            items = [store.get_review("tencent", row_id) for row_id in row_ids]
        except ValueError:
            abort(404)
        return render_template("bulk_confirm_move.html", items=items, target=target)

    @app.post("/reviews/bulk-move")
    @login_required
    def execute_bulk_move():
        challenge = session.get("bulk_move_challenge") or {}
        row_ids = _selected_row_ids(challenge.get("row_ids") or [])
        current = GuardConfig.from_file(str(resolved_config))
        target = _find_move_target(current, challenge.get("target_key", ""))
        reason = request.form.get("reason", "").strip()[:1000]
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        if (
            not row_ids
            or target is None
            or int(challenge.get("created_at") or 0) < int(time.time()) - 600
        ):
            flash("栏目调整确认已失效，请重新勾选。", "error")
            return redirect(url_for("reviews"))
        if not check_password_hash(password_hash, password):
            store.record_audit(
                "admin", "move.bulk_reauth_failed", "tencent", ",".join(map(str, row_ids)),
                {"count": len(row_ids)}, remote_ip()
            )
            flash("管理员密码验证失败，未移动任何内容。", "error")
            return redirect(url_for("confirm_bulk_move"))
        if confirmation != "confirmed" or len(reason) < 4:
            flash("请勾选栏目调整确认，并填写至少 4 个字符的调整理由。", "error")
            return redirect(url_for("confirm_bulk_move"))
        session.pop("bulk_move_challenge", None)

        try:
            store.consume_action_challenge(
                "bulk_move", "admin", str(challenge.get("token") or "")
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reviews", status=""))

        client = cli_factory()
        moved = 0
        failed = 0
        stopped = False
        for row_id in row_ids:
            item = store.get_review("tencent", row_id)
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
                    target["section"], "", reason, remote_ip()
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
        return redirect(url_for("reviews", status=""))

    @app.get("/duplicates")
    @login_required
    def duplicates():
        return render_template("duplicates.html", items=store.duplicates())

    @app.get("/audit")
    @login_required
    def audit():
        return render_template("audit.html", items=store.audit_log(), scans=store.scans())

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
            flash(f"AI 审核设置已更新，策略版本为 {version}。", "success")
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
            flash(f"审核参数已更新，策略版本为 {version}。", "success")
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
            flash(f"分类规则已更新，策略版本为 {version}。", "success")
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
            flash(f"敏感词已添加，策略版本为 {version}。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("rules"))

    @app.post("/rules/terms/<int:index>/delete")
    @login_required
    def delete_term(index: int):
        try:
            version = editor.delete_sensitive_term(index)
            store.record_audit("admin", "term.delete", "policy", version, {"index": index}, remote_ip())
            flash(f"敏感词已移除，策略版本为 {version}。", "success")
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
            flash(f"版块规则已保存，策略版本为 {version}。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("channels"))

    @app.post("/channels/boards/<channel_id>/delete")
    @login_required
    def delete_board(channel_id: str):
        try:
            version = editor.delete_board(channel_id)
            store.record_audit("admin", "board.delete", "channel", channel_id, {"policy_version": version}, remote_ip())
            flash(f"版块规则已删除，策略版本为 {version}。", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("channels"))

    @app.route("/test", methods=["GET", "POST"])
    @login_required
    def test_content():
        result = None
        if request.method == "POST":
            current = GuardConfig.from_file(str(resolved_config))
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
        return render_template("test.html", result=result)

    @app.post("/scan")
    @login_required
    def scan_now():
        current = GuardConfig.from_file(str(resolved_config))
        lease = ScanLock(current.database_path)
        if not lease.acquire():
            message = "已有一轮巡检正在运行，请等待进度完成后再试。"
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
                    cli_factory(),
                    progress_callback=lambda percent, phase, message: scan_status_store.update(
                        job_id,
                        percent=percent,
                        phase=phase,
                        message=message,
                    ),
                )
                report = monitor.scan_once()
                summary = report.public_summary()
                store.record_audit(
                    "admin",
                    "scan.run",
                    "tencent",
                    "all",
                    {
                        "job_id": job_id,
                        "scanned_feeds": summary["scanned_feeds"],
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
                scan_status_store.fail(job_id, str(exc))
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

    @app.get("/scan/status/<job_id>")
    @login_required
    def scan_status(job_id: str):
        if not job_id or len(job_id) > 80:
            abort(404)
        state = scan_status_store.read(job_id)
        if state is None:
            abort(404)
        state["results_url"] = url_for("ai_analysis")
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


def _find_move_target(config: GuardConfig, key: str):
    return next((target for target in _configured_move_targets(config) if target["key"] == key), None)


def _delete_tencent_item(client: TencentCliClient, item: Dict[str, Any]) -> None:
    create_time = str(item.get("source_created_at") or "").strip()
    if not create_time.isdigit():
        detail = client.get_feed_detail(
            item["guild_id"], item["channel_id"], item["item_id"]
        )
        create_time = str(detail.get("create_time_raw") or detail.get("create_time") or "")
    if not create_time.isdigit():
        raise ValueError("无法确认原帖发布时间，为防止误删已停止操作")
    client.delete_feed(
        item["guild_id"],
        item["channel_id"],
        item["item_id"],
        create_time,
        live=True,
    )


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
