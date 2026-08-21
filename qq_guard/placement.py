from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .config import GuardConfig
from .models import Section


def placement_review(
    items: Iterable[Mapping[str, Any]], config: GuardConfig
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build explainable, human-approved move suggestions from stored reviews."""
    targets_by_section: Dict[Tuple[str, Section], List[Dict[str, str]]] = defaultdict(list)
    channels: Dict[Tuple[str, str], Dict[str, str]] = {}

    for settings in config.tencent_channels:
        guild_name = settings.name or settings.guild_id
        for section, channel_id in settings.channels.items():
            target = _target(settings.guild_id, guild_name, channel_id, section, section.display_name)
            channels[(settings.guild_id, channel_id)] = target
            targets_by_section[(settings.guild_id, section)].append(target)
            board = config.board_policies.get(channel_id)
            for accepted in board.expected_sections if board else ():
                if accepted is not section:
                    targets_by_section[(settings.guild_id, accepted)].append(target)
        for channel_name, channel_id in settings.auto_classify_channels.items():
            board = config.board_policies.get(channel_id)
            channels[(settings.guild_id, channel_id)] = {
                "guild_id": settings.guild_id,
                "guild_name": guild_name,
                "channel_id": channel_id,
                "section": Section.UNCLASSIFIED.value,
                "label": channel_name,
                "key": "",
            }
            for section in board.expected_sections if board else ():
                targets_by_section[(settings.guild_id, section)].append(
                    _target(settings.guild_id, guild_name, channel_id, section,
                            f"{channel_name} · {section.display_name}")
                )

    suggestions: List[Dict[str, Any]] = []
    attention: List[Dict[str, Any]] = []
    for raw_item in items:
        item = dict(raw_item)
        if item.get("source") != "tencent" or item.get("delete_status") == "deleted":
            continue
        guild_id = str(item.get("guild_id") or "")
        channel_id = str(item.get("channel_id") or "")
        current = channels.get((guild_id, channel_id))
        if current is None:
            continue
        classification = item.get("classification") or {}
        issues = set(classification.get("validation_issues") or [])
        try:
            detected = Section(str(item.get("section") or classification.get("section") or ""))
        except ValueError:
            detected = Section.UNCLASSIFIED

        item["current_label"] = _current_label(config, channel_id, current["label"])
        item["placement_reason"] = _reason(
            item, detected, issues, item["current_label"]
        )
        item["detected_label"] = detected.display_name

        # Without #topic, the system must never recommend moving a post into 每周一问.
        if detected is Section.UNCLASSIFIED or (
            detected is Section.WEEKLY_QUESTION and "missing_weekly_hashtag" in issues
        ):
            if "missing_weekly_hashtag" in issues:
                item["attention_message"] = "缺少井号话题，不能按规则归入每周一问；请补话题或人工选择其他栏目。"
                attention.append(item)
            continue

        target = next(
            (candidate for candidate in targets_by_section.get((guild_id, detected), [])
             if candidate["channel_id"] != channel_id),
            None,
        )
        if target is None:
            continue
        item["move_target"] = target
        item["move_target_key"] = target["key"]
        suggestions.append(item)

    return suggestions, attention


def group_placement_suggestions(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = str(item["move_target_key"])
        if key not in grouped:
            grouped[key] = {"target": item["move_target"], "items": []}
        grouped[key]["items"].append(item)
    return list(grouped.values())


def _target(guild_id: str, guild_name: str, channel_id: str,
            section: Section, label: str) -> Dict[str, str]:
    return {
        "key": f"{guild_id}:{channel_id}:{section.value}",
        "guild_id": guild_id,
        "guild_name": guild_name,
        "channel_id": channel_id,
        "section": section.value,
        "label": label,
    }


def _current_label(config: GuardConfig, channel_id: str, fallback: str) -> str:
    board = config.board_policies.get(channel_id)
    return board.name if board and board.name else fallback


def _reason(
    item: Mapping[str, Any], detected: Section, issues: set, current_label: str
) -> str:
    classification = item.get("classification") or {}
    reasons = [str(value) for value in classification.get("reasons") or [] if value]
    if "missing_weekly_hashtag" in issues:
        return "这条内容没有带规定的话题标签，不能直接归入“每周一问”。"
    if reasons:
        reason = reasons[0].strip()
        technical_markers = (
            "ai语义分类",
            "board_policy",
            "require_hashtag",
            "validation_issue",
            "置信度",
            "模型",
        )
        if len(reason) <= 120 and not any(marker in reason.casefold() for marker in technical_markers):
            return reason
    summary = str((item.get("ai_analysis") or {}).get("summary") or "").strip()
    if summary and len(summary) <= 120 and not any(
        marker in summary.casefold()
        for marker in ("board_policy", "require_hashtag", "validation_issue", "模型")
    ):
        return summary
    return (
        f"内容表现为“{detected.display_name}”，与当前“{current_label}”的发布要求不一致。"
        "请查看完整内容后确认是否移动。"
    )
