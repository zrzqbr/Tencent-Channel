import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

from .models import Section


_HASHTAG_VALUE_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,30}$", re.UNICODE)


@dataclass(frozen=True)
class ClassificationRules:
    weekly_phrase_keywords: List[str] = field(
        default_factory=lambda: ["每周一问", "本周问题", "本周一问"]
    )
    question_keywords: List[str] = field(
        default_factory=lambda: ["请问", "如何", "怎么", "为什么", "求助", "大家觉得", "讨论一下", "交流一下"]
    )
    practical_keywords: List[str] = field(
        default_factory=lambda: ["案例", "教程", "步骤", "实战", "指南", "经验", "复盘", "解决方案", "技巧", "最佳实践"]
    )
    min_practical_text_length: int = 100
    min_featured_text_length: int = 220
    weekly_requires_any_hashtag: bool = True


@dataclass(frozen=True)
class TencentChannelSettings:
    guild_id: str
    name: str = ""
    channels: Mapping[Section, str] = field(default_factory=dict)
    auto_classify_channels: Mapping[str, str] = field(default_factory=dict)
    scan_count: int = 20
    poll_interval_seconds: int = 300


@dataclass(frozen=True)
class SensitiveTermRule:
    term: str
    language: str
    category: str
    severity: str = "medium"
    action: str = "review"
    match_type: str = "auto"


@dataclass(frozen=True)
class BoardPolicy:
    name: str
    expected_sections: Tuple[Section, ...] = field(default_factory=tuple)
    require_hashtag: bool = False
    min_text_length: int = 1
    allow_external_links: bool = True


@dataclass(frozen=True)
class SectionTopicPolicy:
    section: Section
    required_hashtags: Tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True


@dataclass(frozen=True)
class ContentPolicy:
    name: str
    keywords: Tuple[str, ...] = field(default_factory=tuple)
    guidance: str = ""
    action: str = "review"
    enabled: bool = True


@dataclass(frozen=True)
class ModerationSettings:
    enabled: bool = True
    policy_version: str = "2026-08-19.1"
    review_threshold: int = 25
    delete_candidate_threshold: int = 80
    min_meaningful_length: int = 4
    detect_contact_information: bool = True
    detect_external_links: bool = True
    detect_obfuscated_terms: bool = True
    terms: Tuple[SensitiveTermRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AIReviewSettings:
    enabled: bool = False
    provider: str = "tencent_tokenhub"
    model: str = "hy3"
    vision_model: str = "youtu-vita"
    prompt_version: str = "2026-08-23.ai4"
    vision_prompt_version: str = "2026-08-23.vision2"
    timeout_seconds: int = 30
    vision_timeout_seconds: int = 45
    max_input_chars: int = 12000
    minimum_allow_confidence: float = 0.80
    include_images: bool = True
    max_images: int = 3


@dataclass(frozen=True)
class GuardConfig:
    database_path: Path
    delete_mode: str = "dry_run"
    auto_delete_duplicates: bool = False
    auto_delete_policy_violations: bool = False
    hide_recall_tip: bool = False
    channel_sections: Mapping[str, Section] = field(default_factory=dict)
    official_author_ids: Set[str] = field(default_factory=set)
    section_hashtags: Mapping[Section, Set[str]] = field(default_factory=dict)
    rules: ClassificationRules = field(default_factory=ClassificationRules)
    tencent_channel: Optional[TencentChannelSettings] = None
    tencent_channels: Tuple[TencentChannelSettings, ...] = field(default_factory=tuple)
    board_policies: Mapping[str, BoardPolicy] = field(default_factory=dict)
    section_topic_policies: Mapping[Section, SectionTopicPolicy] = field(default_factory=dict)
    content_policies: Tuple[ContentPolicy, ...] = field(default_factory=tuple)
    moderation: ModerationSettings = field(default_factory=ModerationSettings)
    ai_review: AIReviewSettings = field(default_factory=AIReviewSettings)

    @classmethod
    def from_file(cls, path: str) -> "GuardConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        delete_mode = raw.get("delete_mode", "dry_run")
        if delete_mode not in {"dry_run", "live"}:
            raise ValueError("delete_mode 只能是 dry_run 或 live")

        db_path = Path(raw.get("database_path", "./data/guard.sqlite3"))
        if not db_path.is_absolute():
            db_path = (config_path.parent / db_path).resolve()

        channel_sections: Dict[str, Section] = {
            str(channel_id): Section(section)
            for channel_id, section in raw.get("channel_sections", {}).items()
        }
        section_hashtags: Dict[Section, Set[str]] = {}
        for section, hashtags in raw.get("section_hashtags", {}).items():
            section_hashtags[Section(section)] = {
                _normalize_hashtag(tag) for tag in hashtags if _normalize_hashtag(tag)
            }

        rule_values = raw.get("rules", {})
        rules = ClassificationRules(
            weekly_phrase_keywords=list(
                rule_values.get("weekly_phrase_keywords", ClassificationRules().weekly_phrase_keywords)
            ),
            question_keywords=list(
                rule_values.get("question_keywords", ClassificationRules().question_keywords)
            ),
            practical_keywords=list(
                rule_values.get("practical_keywords", ClassificationRules().practical_keywords)
            ),
            min_practical_text_length=int(rule_values.get("min_practical_text_length", 100)),
            min_featured_text_length=int(rule_values.get("min_featured_text_length", 220)),
            weekly_requires_any_hashtag=bool(rule_values.get("weekly_requires_any_hashtag", True)),
        )

        configured_tencent_channels: List[TencentChannelSettings] = []
        multi_values = raw.get("tencent_channels")
        if multi_values is not None:
            if not isinstance(multi_values, list):
                raise ValueError("tencent_channels 必须是数组")
            for index, values in enumerate(multi_values):
                if values and values.get("enabled", True):
                    configured_tencent_channels.append(
                        _parse_tencent_settings(values, f"tencent_channels[{index}]")
                    )
        else:
            legacy_values = raw.get("tencent_channel")
            if legacy_values and legacy_values.get("enabled", True):
                configured_tencent_channels.append(
                    _parse_tencent_settings(legacy_values, "tencent_channel")
                )

        tencent_channels = tuple(configured_tencent_channels)
        tencent_channel = tencent_channels[0] if tencent_channels else None

        board_policies = _parse_board_policies(raw.get("board_policies", {}))
        section_topic_policies = _parse_section_topic_policies(
            raw.get("section_topic_policies", {})
        )
        content_policies = _parse_content_policies(raw.get("content_policies", []))
        moderation = _parse_moderation(raw.get("moderation", {}))
        ai_review = _parse_ai_review(raw.get("ai_review", {}))

        return cls(
            database_path=db_path,
            delete_mode=delete_mode,
            auto_delete_duplicates=bool(raw.get("auto_delete_duplicates", False)),
            auto_delete_policy_violations=bool(
                raw.get("auto_delete_policy_violations", False)
            ),
            hide_recall_tip=bool(raw.get("hide_recall_tip", False)),
            channel_sections=channel_sections,
            official_author_ids={str(value) for value in raw.get("official_author_ids", [])},
            section_hashtags=section_hashtags,
            rules=rules,
            tencent_channel=tencent_channel,
            tencent_channels=tencent_channels,
            board_policies=board_policies,
            section_topic_policies=section_topic_policies,
            content_policies=content_policies,
            moderation=moderation,
            ai_review=ai_review,
        )


def _normalize_hashtag(tag: str) -> str:
    return unicodedata.normalize("NFKC", str(tag)).strip().lstrip("#").casefold()


def _parse_tencent_settings(values: Mapping[str, object], path: str) -> TencentChannelSettings:
    guild_id = str(values.get("guild_id", "")).strip()
    if not guild_id.isdigit():
        raise ValueError(f"{path}.guild_id 必须是数字字符串")

    raw_channels = values.get("channels", {})
    if not isinstance(raw_channels, Mapping):
        raise ValueError(f"{path}.channels 必须是对象")
    configured_channels = {
        Section(str(section)): str(channel_id).strip()
        for section, channel_id in raw_channels.items()
    }

    raw_auto_channels = values.get("auto_classify_channels", {})
    if not isinstance(raw_auto_channels, Mapping):
        raise ValueError(f"{path}.auto_classify_channels 必须是对象")
    auto_channels = {
        str(name).strip(): str(channel_id).strip()
        for name, channel_id in raw_auto_channels.items()
        if str(name).strip()
    }

    all_channel_ids = list(configured_channels.values()) + list(auto_channels.values())
    if not all_channel_ids or any(not channel_id.isdigit() for channel_id in all_channel_ids):
        raise ValueError(f"{path} 必须配置至少一个有效的数字版块 ID")
    if len(all_channel_ids) != len(set(all_channel_ids)):
        raise ValueError(f"{path} 中同一版块不能同时重复配置")

    return TencentChannelSettings(
        guild_id=guild_id,
        name=str(values.get("name", "")).strip(),
        channels=configured_channels,
        auto_classify_channels=auto_channels,
        scan_count=max(2, min(int(values.get("scan_count", 20)), 100)),
        poll_interval_seconds=max(30, int(values.get("poll_interval_seconds", 300))),
    )


def _parse_board_policies(values: object) -> Mapping[str, BoardPolicy]:
    if not isinstance(values, Mapping):
        raise ValueError("board_policies 必须是对象")
    policies: Dict[str, BoardPolicy] = {}
    for channel_id, raw_policy in values.items():
        if not str(channel_id).isdigit() or not isinstance(raw_policy, Mapping):
            raise ValueError("board_policies 必须使用数字版块 ID 和规则对象")
        expected = tuple(
            Section(str(section)) for section in raw_policy.get("expected_sections", [])
        )
        policies[str(channel_id)] = BoardPolicy(
            name=str(raw_policy.get("name", channel_id)).strip(),
            expected_sections=expected,
            require_hashtag=bool(raw_policy.get("require_hashtag", False)),
            min_text_length=max(0, int(raw_policy.get("min_text_length", 1))),
            allow_external_links=bool(raw_policy.get("allow_external_links", True)),
        )
    return policies


def _parse_section_topic_policies(
    values: object,
) -> Mapping[Section, SectionTopicPolicy]:
    if not isinstance(values, Mapping):
        raise ValueError("section_topic_policies 必须是对象")
    policies: Dict[Section, SectionTopicPolicy] = {}
    for raw_section, raw_policy in values.items():
        try:
            section = Section(str(raw_section))
        except ValueError as exc:
            raise ValueError(f"section_topic_policies.{raw_section} 栏目无效") from exc
        if section is Section.UNCLASSIFIED or not isinstance(raw_policy, Mapping):
            raise ValueError(f"section_topic_policies.{raw_section} 规则无效")
        raw_hashtags = raw_policy.get("required_hashtags", [])
        if not isinstance(raw_hashtags, list):
            raise ValueError(
                f"section_topic_policies.{raw_section}.required_hashtags 必须是数组"
            )
        hashtags: List[str] = []
        seen = set()
        for value in raw_hashtags:
            display_value = str(value).strip().lstrip("#").strip()
            normalized = _normalize_hashtag(display_value)
            if display_value and not _HASHTAG_VALUE_RE.fullmatch(display_value):
                raise ValueError(
                    f"section_topic_policies.{raw_section} 的指定话题不能包含空格或特殊符号"
                )
            if normalized and normalized not in seen:
                hashtags.append(display_value[:80])
                seen.add(normalized)
        enabled = bool(raw_policy.get("enabled", True))
        if enabled and not hashtags:
            raise ValueError(
                f"section_topic_policies.{raw_section} 启用时必须填写指定话题"
            )
        policies[section] = SectionTopicPolicy(
            section=section,
            required_hashtags=tuple(hashtags),
            enabled=enabled,
        )
    return policies


def _parse_content_policies(values: object) -> Tuple[ContentPolicy, ...]:
    if not isinstance(values, list):
        raise ValueError("content_policies 必须是数组")
    policies: List[ContentPolicy] = []
    for index, raw_policy in enumerate(values):
        if not isinstance(raw_policy, Mapping):
            raise ValueError(f"content_policies[{index}] 规则无效")
        name = str(raw_policy.get("name", "")).strip()
        guidance = str(raw_policy.get("guidance", "")).strip()
        raw_keywords = raw_policy.get("keywords", [])
        action = str(raw_policy.get("action", "review"))
        if not name or len(name) > 80:
            raise ValueError(f"content_policies[{index}].name 无效")
        if not guidance or len(guidance) > 500:
            raise ValueError(f"content_policies[{index}].guidance 无效")
        if not isinstance(raw_keywords, list):
            raise ValueError(f"content_policies[{index}].keywords 必须是数组")
        keywords: List[str] = []
        seen = set()
        for value in raw_keywords:
            keyword = str(value).strip()
            normalized = normalize_policy_text(keyword)
            if normalized and normalized not in seen:
                keywords.append(keyword[:80])
                seen.add(normalized)
        if not keywords:
            raise ValueError(f"content_policies[{index}] 至少需要一个触发词")
        if action not in {"notice", "review", "delete_candidate"}:
            raise ValueError(f"content_policies[{index}].action 无效")
        policies.append(
            ContentPolicy(
                name=name,
                keywords=tuple(keywords),
                guidance=guidance,
                action=action,
                enabled=bool(raw_policy.get("enabled", True)),
            )
        )
    return tuple(policies)


def normalize_policy_text(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def _parse_moderation(values: object) -> ModerationSettings:
    if not isinstance(values, Mapping):
        raise ValueError("moderation 必须是对象")
    raw_terms = values.get("terms", _default_sensitive_terms())
    if not isinstance(raw_terms, list):
        raise ValueError("moderation.terms 必须是数组")
    terms: List[SensitiveTermRule] = []
    for index, raw_rule in enumerate(raw_terms):
        if not isinstance(raw_rule, Mapping) or not str(raw_rule.get("term", "")).strip():
            raise ValueError(f"moderation.terms[{index}] 缺少 term")
        severity = str(raw_rule.get("severity", "medium"))
        action = str(raw_rule.get("action", "review"))
        match_type = str(raw_rule.get("match_type", "auto"))
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"moderation.terms[{index}].severity 无效")
        if action not in {"review", "delete_candidate"}:
            raise ValueError(f"moderation.terms[{index}].action 无效")
        if match_type not in {"auto", "word", "substring", "regex"}:
            raise ValueError(f"moderation.terms[{index}].match_type 无效")
        terms.append(
            SensitiveTermRule(
                term=str(raw_rule["term"]).strip(),
                language=str(raw_rule.get("language", "unknown")),
                category=str(raw_rule.get("category", "sensitive")),
                severity=severity,
                action=action,
                match_type=match_type,
            )
        )

    review_threshold = max(0, min(int(values.get("review_threshold", 25)), 100))
    delete_threshold = max(
        review_threshold,
        min(int(values.get("delete_candidate_threshold", 80)), 100),
    )
    return ModerationSettings(
        enabled=bool(values.get("enabled", True)),
        policy_version=str(values.get("policy_version", "2026-08-19.1")),
        review_threshold=review_threshold,
        delete_candidate_threshold=delete_threshold,
        min_meaningful_length=max(0, int(values.get("min_meaningful_length", 4))),
        detect_contact_information=bool(values.get("detect_contact_information", True)),
        detect_external_links=bool(values.get("detect_external_links", True)),
        detect_obfuscated_terms=bool(values.get("detect_obfuscated_terms", True)),
        terms=tuple(terms),
    )


def _parse_ai_review(values: object) -> AIReviewSettings:
    if not isinstance(values, Mapping):
        raise ValueError("ai_review 必须是对象")
    provider = str(values.get("provider", "tencent_tokenhub")).strip().casefold()
    if provider != "tencent_tokenhub":
        raise ValueError("ai_review.provider 当前仅支持 tencent_tokenhub")
    model = str(values.get("model", "hy3")).strip()
    if not model or len(model) > 100:
        raise ValueError("ai_review.model 无效")
    vision_model = str(values.get("vision_model", "youtu-vita")).strip()
    if not vision_model or len(vision_model) > 100:
        raise ValueError("ai_review.vision_model 无效")
    confidence = float(values.get("minimum_allow_confidence", 0.80))
    return AIReviewSettings(
        enabled=bool(values.get("enabled", False)),
        provider=provider,
        model=model,
        vision_model=vision_model,
        prompt_version=str(values.get("prompt_version", "2026-08-23.ai4"))[:80],
        vision_prompt_version=str(
            values.get("vision_prompt_version", "2026-08-23.vision2")
        )[:80],
        timeout_seconds=max(5, min(int(values.get("timeout_seconds", 30)), 120)),
        vision_timeout_seconds=max(
            5, min(int(values.get("vision_timeout_seconds", 45)), 120)
        ),
        max_input_chars=max(500, min(int(values.get("max_input_chars", 12000)), 50000)),
        minimum_allow_confidence=max(0.0, min(confidence, 1.0)),
        include_images=bool(values.get("include_images", True)),
        max_images=max(0, min(int(values.get("max_images", 3)), 5)),
    )


def _default_sensitive_terms() -> List[Mapping[str, str]]:
    return [
        {"term": "傻逼", "language": "zh", "category": "abuse", "severity": "high", "action": "review"},
        {"term": "赌博", "language": "zh", "category": "prohibited", "severity": "high", "action": "delete_candidate"},
        {"term": "博彩", "language": "zh", "category": "prohibited", "severity": "high", "action": "delete_candidate"},
        {"term": "裸聊", "language": "zh", "category": "prohibited", "severity": "critical", "action": "delete_candidate"},
        {"term": "刷单", "language": "zh", "category": "fraud", "severity": "high", "action": "delete_candidate"},
        {"term": "代开发票", "language": "zh", "category": "fraud", "severity": "high", "action": "delete_candidate"},
        {"term": "加微信", "language": "zh", "category": "promotion", "severity": "medium", "action": "review"},
        {"term": "sb", "language": "en", "category": "abuse", "severity": "medium", "action": "review", "match_type": "word"},
        {"term": "fuck", "language": "en", "category": "abuse", "severity": "high", "action": "review", "match_type": "word"},
        {"term": "bitch", "language": "en", "category": "abuse", "severity": "high", "action": "review", "match_type": "word"},
        {"term": "casino", "language": "en", "category": "prohibited", "severity": "high", "action": "delete_candidate", "match_type": "word"},
        {"term": "porn", "language": "en", "category": "prohibited", "severity": "critical", "action": "delete_candidate", "match_type": "word"},
    ]
