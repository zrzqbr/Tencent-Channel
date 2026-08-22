from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .config import GuardConfig
from .models import Section


def move_targets(
    config: GuardConfig,
    discovered_channels: Iterable[Mapping[str, Any]] = (),
) -> List[Dict[str, str]]:
    """Return each physical channel once, preferring its synced display name."""
    targets: Dict[Tuple[str, str], Dict[str, str]] = {}
    order: List[Tuple[str, str]] = []

    for settings in config.tencent_channels:
        guild_name = settings.name or settings.guild_id
        for section, channel_id in settings.channels.items():
            key = (settings.guild_id, channel_id)
            if key not in targets:
                order.append(key)
            targets[key] = _target(
                settings.guild_id,
                guild_name,
                channel_id,
                section,
                section.display_name,
            )
        for channel_name, channel_id in settings.auto_classify_channels.items():
            key = (settings.guild_id, channel_id)
            if key not in targets:
                order.append(key)
            targets[key] = _target(
                settings.guild_id,
                guild_name,
                channel_id,
                Section.UNCLASSIFIED,
                channel_name,
            )

    for raw_channel in discovered_channels:
        guild_id = str(raw_channel.get("guild_id") or "").strip()
        channel_id = str(raw_channel.get("channel_id") or "").strip()
        if not guild_id or not channel_id:
            continue
        key = (guild_id, channel_id)
        if key not in targets:
            order.append(key)
            targets[key] = _target(
                guild_id,
                str(raw_channel.get("guild_name") or guild_id),
                channel_id,
                Section.UNCLASSIFIED,
                str(raw_channel.get("channel_name") or "未命名栏目"),
            )
            continue
        target = targets[key]
        synced_guild_name = str(raw_channel.get("guild_name") or "").strip()
        synced_channel_name = str(raw_channel.get("channel_name") or "").strip()
        if synced_guild_name:
            target["guild_name"] = synced_guild_name
        if synced_channel_name:
            target["label"] = synced_channel_name

    return [targets[key] for key in order]


def placement_review(
    items: Iterable[Mapping[str, Any]],
    config: GuardConfig,
    discovered_channels: Iterable[Mapping[str, Any]] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build explainable, human-approved move suggestions from stored reviews."""
    targets_by_section: Dict[Tuple[str, Section], List[Dict[str, str]]] = defaultdict(list)
    channels: Dict[Tuple[str, str], Dict[str, str]] = {}

    for target in move_targets(config, discovered_channels):
        guild_id = target["guild_id"]
        channel_id = target["channel_id"]
        channels[(guild_id, channel_id)] = target
        board = config.board_policies.get(channel_id)
        accepted_sections = board.expected_sections if board else ()
        if not accepted_sections and target["section"] != Section.UNCLASSIFIED.value:
            accepted_sections = (Section(target["section"]),)
        for section in accepted_sections:
            targets_by_section[(guild_id, section)].append(target)

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

        item["current_label"] = current["label"]
        item["placement_reason"] = _reason(
            config, item, detected, issues, item["current_label"]
        )
        item["detected_label"] = detected.display_name

        missing_topic = "missing_weekly_hashtag" in issues or any(
            str(issue).startswith("missing_required_hashtag:") for issue in issues
        )
        # Without the current required topic, never recommend moving a post into that section.
        if detected is Section.UNCLASSIFIED or (
            detected is Section.WEEKLY_QUESTION and missing_topic
        ):
            if missing_topic:
                item["attention_message"] = _missing_topic_message(config, issues)
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
        "key": f"{guild_id}:{channel_id}",
        "guild_id": guild_id,
        "guild_name": guild_name,
        "channel_id": channel_id,
        "section": section.value,
        "label": label or "未命名栏目",
    }


def _reason(
    config: GuardConfig,
    item: Mapping[str, Any],
    detected: Section,
    issues: set,
    current_label: str,
) -> str:
    classification = item.get("classification") or {}
    reasons = [str(value) for value in classification.get("reasons") or [] if value]
    if "missing_weekly_hashtag" in issues:
        return _missing_topic_message_from_section(config, Section.WEEKLY_QUESTION)
    if any(str(issue).startswith("missing_required_hashtag:") for issue in issues):
        return _missing_topic_message(config, issues)
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


def _missing_topic_message(config: GuardConfig, issues: set) -> str:
    section = Section.WEEKLY_QUESTION
    for issue in issues:
        value = str(issue)
        if not value.startswith("missing_required_hashtag:"):
            continue
        try:
            section = Section(value.partition(":")[2])
        except ValueError:
            pass
        break
    return _missing_topic_message_from_section(config, section)


def _missing_topic_message_from_section(
    config: GuardConfig, section: Section
) -> str:
    policy = config.section_topic_policies.get(section)
    if policy and policy.enabled:
        required = " 或 ".join(f"#{value}" for value in policy.required_hashtags)
        return f"缺少{section.display_name}当前指定话题 {required}；请补充话题后再决定栏目。"
    return f"缺少井号话题，不能按规则归入{section.display_name}；请补充话题后再决定栏目。"
