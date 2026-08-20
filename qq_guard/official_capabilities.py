"""Presentation and safety helpers for Tencent's official community Skill."""

import json
import re
from typing import Any, Dict, Iterable, List, Mapping


OFFICIAL_SKILL_VERSION = "1.1.5"

CATEGORY_LABELS = {
    "channel": "频道与栏目",
    "content": "帖子与内容",
    "interaction": "评论与互动",
    "member": "成员与权限",
    "notification": "通知与私信",
    "operation": "运营快捷工具",
}

ACTION_CATEGORIES = {
    "get-guild-info": "channel",
    "get-my-join-guild-info": "channel",
    "get-guild-channel-list": "channel",
    "search-guild-content": "channel",
    "get-join-guild-setting": "channel",
    "get-guild-share-url": "channel",
    "get-share-info": "channel",
    "join-guild": "channel",
    "create-channel": "channel",
    "delete-channel": "channel",
    "modify-channel": "channel",
    "update-guild-info": "channel",
    "modify-guild-number": "channel",
    "upload-guild-avatar": "channel",
    "create-theme-private-guild": "channel",
    "update-join-guild-setting": "channel",
    "leave-guild": "channel",
    "get-guild-feeds": "content",
    "get-channel-timeline-feeds": "content",
    "get-feed-detail": "content",
    "search-guild-feeds": "content",
    "get-feed-share-url": "content",
    "publish-feed": "content",
    "del-feed": "content",
    "alter-feed": "content",
    "move-feed": "content",
    "top-feed": "content",
    "set-feed-essence": "content",
    "push-essence-feed": "content",
    "get-feed-comments": "interaction",
    "get-next-page-replies": "interaction",
    "get-notices": "interaction",
    "do-comment": "interaction",
    "do-reply": "interaction",
    "do-like": "interaction",
    "do-feed-prefer": "interaction",
    "get-user-info": "member",
    "get-guild-member-list": "member",
    "guild-member-search": "member",
    "kick-guild-member": "member",
    "modify-member-shut-up": "member",
    "add-admin": "member",
    "remove-admin": "member",
    "create-guild-role-group": "member",
    "modify-guild-role-group": "member",
    "add-role-members": "member",
    "remove-role-members": "member",
    "notices-status": "notification",
    "check-notices": "notification",
    "check-new-notices": "notification",
    "get-recent-notices": "notification",
    "notices-on": "notification",
    "notices-off": "notification",
    "subscribe-notices": "notification",
    "unsubscribe-notices": "notification",
    "notify-daemon": "notification",
    "deal-notice": "notification",
    "push-group-dm-msg": "notification",
    "quick-publish": "operation",
    "search-and-comment": "operation",
    "delete-and-mute": "operation",
    "latest-feeds-detail": "operation",
    "hot-feeds-detail": "operation",
    "search-and-join": "operation",
}

SENSITIVE_KEY = re.compile(
    r"(token|cookie|secret|password|credential|attach_info|attch_info|session_key|raw)$",
    re.IGNORECASE,
)

WRITE_SHORTCUTS = {"quick-publish", "search-and-comment", "search-and-join"}
HIGH_RISK_SHORTCUTS = {"delete-and-mute"}

ACTION_NOTES = {
    "publish-feed": "短贴不支持 Markdown；长贴需要标题且不支持话题标签。涉及 @用户时必须先搜索得到用户ID，不能填写QQ号或猜测值。",
    "alter-feed": "编辑媒体默认会保留原图片和视频；如需替换，必须使用官方清除参数后再添加。",
    "move-feed": "移动前必须确认当前账号是目标频道的频道主或超级管理员。",
    "do-comment": "评论帖子与回复已有评论是两种不同操作；删除评论属于不可逆操作。",
    "do-reply": "回复需要完整的评论和作者信息；删除回复属于不可逆操作。",
    "push-essence-feed": "每天最多推送3次，每个帖子只能推送一次，并且帖子必须先设为精华。",
    "modify-member-shut-up": "禁言时间必须填写绝对时间戳；填写0表示立即解除禁言。",
    "notices-on": "官方自动推送仅支持 OpenClaw，当前网站服务器不能把它当作网页实时通知。",
    "notify-daemon": "官方通知守护进程仅适用于 OpenClaw，不替代本平台的增量巡检服务。",
    "upload-guild-avatar": "服务器文件路径输入已被平台禁用，后续应通过受控媒体上传区处理。",
}


def normalize_index(index: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    capabilities: List[Dict[str, Any]] = []
    for domain_item in index:
        domain = str(domain_item.get("domain") or "")
        if domain not in {"feed", "manage"}:
            continue
        for command in domain_item.get("commands") or []:
            if not isinstance(command, Mapping):
                continue
            action = str(command.get("use") or "")
            if not action:
                continue
            if action in HIGH_RISK_SHORTCUTS:
                risk = "high-risk-write"
            elif action in WRITE_SHORTCUTS:
                risk = "write"
            else:
                risk = str(command.get("risk") or "read")
            capabilities.append(
                {
                    "domain": domain,
                    "action": action,
                    "path": f"{domain}.{action}",
                    "title": str(command.get("short") or action),
                    "group": str(command.get("group") or ""),
                    "risk": risk,
                    "is_write": risk != "read",
                    "is_high_risk": risk == "high-risk-write",
                    "category": ACTION_CATEGORIES.get(
                        action, "content" if domain == "feed" else "channel"
                    ),
                    "note": ACTION_NOTES.get(action, ""),
                }
            )
    return capabilities


def grouped_capabilities(capabilities: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = {
        key: [] for key in CATEGORY_LABELS
    }
    for capability in capabilities:
        grouped.setdefault(str(capability.get("category") or "operation"), []).append(
            capability
        )
    return [
        {"key": key, "label": label, "items": grouped.get(key, [])}
        for key, label in CATEGORY_LABELS.items()
        if grouped.get(key)
    ]


def parse_parameters(schema: Mapping[str, Any], values: Mapping[str, str]) -> Dict[str, Any]:
    parameters: Dict[str, Any] = {}
    for flag in schema.get("flags") or []:
        if not isinstance(flag, Mapping):
            continue
        flag_name = str(flag.get("name") or "")
        if not flag_name:
            continue
        form_name = f"param__{flag_name}"
        raw = str(values.get(form_name) or "").strip()
        if not raw:
            continue
        key = flag_name.replace("-", "_")
        value_type = str(flag.get("type") or "string").casefold()
        if value_type in {"int", "integer"}:
            parameters[key] = int(raw)
        elif value_type in {"bool", "boolean"}:
            parameters[key] = raw.casefold() in {"1", "true", "yes", "on"}
        elif value_type in {"json", "object", "array", "stringarray", "strings"}:
            parameters[key] = json.loads(raw)
        else:
            parameters[key] = raw
    advanced = str(values.get("advanced_json") or "").strip()
    if advanced:
        extra = json.loads(advanced)
        if not isinstance(extra, dict):
            raise ValueError("高级参数必须是 JSON 对象")
        parameters.update(extra)
    return parameters


def normalize_command_parameters(
    domain: str, action: str, parameters: Mapping[str, Any]
) -> Dict[str, Any]:
    result = dict(parameters)
    if domain == "feed" and action == "get-channel-timeline-feeds":
        if "feed_attach_info" in result:
            result["feed_attch_info"] = result.pop("feed_attach_info")
    if domain == "feed" and action == "search-guild-feeds":
        if "next_page_cookie" in result:
            result["cookie"] = result.pop("next_page_cookie")
    return result


def safe_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if SENSITIVE_KEY.search(name):
                result[name] = "[已隐藏]"
            else:
                result[name] = safe_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_payload(item) for item in value]
    if isinstance(value, str) and len(value) > 8000:
        return value[:8000] + "…"
    return value


def safe_audit_parameters(parameters: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(safe_payload(parameters))
