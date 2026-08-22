import json
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

from .config import GuardConfig
from .models import Section


class ConfigEditor:
    """只允许后台修改审核相关白名单字段，并在替换前执行完整校验。"""

    _lock = threading.RLock()

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path).expanduser().resolve()

    def snapshot(self) -> Dict[str, Any]:
        with self.config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def add_sensitive_term(self, values: Dict[str, str]) -> str:
        term = str(values.get("term", "")).strip()
        if not term or len(term) > 120:
            raise ValueError("敏感词长度必须为 1–120 个字符")
        language = str(values.get("language", "zh")).strip().casefold()
        if language not in {"zh", "en", "mixed", "unknown"}:
            raise ValueError("语言类型无效")
        severity = str(values.get("severity", "medium"))
        action = str(values.get("action", "review"))
        match_type = str(values.get("match_type", "auto"))
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("风险等级无效")
        if action not in {"review", "delete_candidate"}:
            raise ValueError("建议动作无效")
        if match_type not in {"auto", "word", "substring", "regex"}:
            raise ValueError("匹配方式无效")

        def mutate(raw: Dict[str, Any]) -> None:
            moderation = raw.setdefault("moderation", {})
            terms = moderation.setdefault("terms", [])
            if any(str(item.get("term", "")).casefold() == term.casefold() for item in terms):
                raise ValueError("该敏感词已经存在")
            terms.append(
                {
                    "term": term,
                    "language": language,
                    "category": str(values.get("category", "custom")).strip()[:40] or "custom",
                    "severity": severity,
                    "action": action,
                    "match_type": match_type,
                }
            )

        return self._mutate(mutate, bump_policy=True)

    def delete_sensitive_term(self, index: int) -> str:
        def mutate(raw: Dict[str, Any]) -> None:
            terms = raw.setdefault("moderation", {}).setdefault("terms", [])
            if index < 0 or index >= len(terms):
                raise ValueError("敏感词不存在")
            terms.pop(index)

        return self._mutate(mutate, bump_policy=True)

    def update_moderation(self, values: Dict[str, Any]) -> str:
        review = max(0, min(int(values.get("review_threshold", 25)), 100))
        delete = max(review, min(int(values.get("delete_candidate_threshold", 80)), 100))
        minimum = max(0, min(int(values.get("min_meaningful_length", 4)), 1000))

        def mutate(raw: Dict[str, Any]) -> None:
            moderation = raw.setdefault("moderation", {})
            moderation.update(
                {
                    "enabled": bool(values.get("enabled")),
                    "review_threshold": review,
                    "delete_candidate_threshold": delete,
                    "min_meaningful_length": minimum,
                    "detect_contact_information": bool(values.get("detect_contact_information")),
                    "detect_external_links": bool(values.get("detect_external_links")),
                    "detect_obfuscated_terms": bool(values.get("detect_obfuscated_terms")),
                }
            )

        return self._mutate(mutate, bump_policy=True)

    def update_ai_review(self, values: Dict[str, Any]) -> str:
        model = str(values.get("model", "hy3")).strip()
        if not model or len(model) > 100:
            raise ValueError("模型名称无效")
        vision_model = str(values.get("vision_model", "youtu-vita")).strip()
        if not vision_model or len(vision_model) > 100:
            raise ValueError("视觉模型名称无效")
        confidence = max(
            0.0, min(float(values.get("minimum_allow_confidence", 0.80)), 1.0)
        )

        def mutate(raw: Dict[str, Any]) -> None:
            ai_review = raw.setdefault("ai_review", {})
            ai_review.update(
                {
                    "enabled": bool(values.get("enabled")),
                    "provider": "tencent_tokenhub",
                    "model": model,
                    "vision_model": vision_model,
                    "prompt_version": str(
                        ai_review.get("prompt_version", "2026-08-23.ai4")
                    ),
                    "vision_prompt_version": str(
                        ai_review.get(
                            "vision_prompt_version", "2026-08-23.vision2"
                        )
                    ),
                    "timeout_seconds": max(
                        5, min(int(values.get("timeout_seconds", 30)), 120)
                    ),
                    "vision_timeout_seconds": max(
                        5, min(int(values.get("vision_timeout_seconds", 45)), 120)
                    ),
                    "max_input_chars": max(
                        500, min(int(values.get("max_input_chars", 12000)), 50000)
                    ),
                    "minimum_allow_confidence": confidence,
                    "include_images": bool(values.get("include_images")),
                    "max_images": max(0, min(int(values.get("max_images", 3)), 5)),
                }
            )

        return self._mutate(mutate, bump_policy=True)

    def update_keywords(self, values: Dict[str, Any]) -> str:
        def parse(name: str) -> List[str]:
            result: List[str] = []
            for item in str(values.get(name, "")).replace("，", ",").split(","):
                value = item.strip()
                if value and value not in result:
                    result.append(value)
            if not result:
                raise ValueError(f"{name} 不能留空")
            return result[:100]

        def mutate(raw: Dict[str, Any]) -> None:
            rules = raw.setdefault("rules", {})
            rules.update(
                {
                    "weekly_phrase_keywords": parse("weekly_phrase_keywords"),
                    "question_keywords": parse("question_keywords"),
                    "practical_keywords": parse("practical_keywords"),
                    "min_practical_text_length": max(
                        1, min(int(values.get("min_practical_text_length", 100)), 10000)
                    ),
                    "min_featured_text_length": max(
                        1, min(int(values.get("min_featured_text_length", 220)), 10000)
                    ),
                    "weekly_requires_any_hashtag": bool(values.get("weekly_requires_any_hashtag")),
                }
            )

        return self._mutate(mutate, bump_policy=True)

    def upsert_section_topic_policy(self, values: Dict[str, Any]) -> str:
        section_value = str(values.get("section", "")).strip()
        try:
            section = Section(section_value)
        except ValueError as exc:
            raise ValueError("请选择有效的适用栏目") from exc
        if section is Section.UNCLASSIFIED:
            raise ValueError("待管理员确认不能设置指定话题")
        hashtags = self._parse_list(values.get("required_hashtags", ""), 20, 80, strip_hash=True)
        if not hashtags:
            raise ValueError("请至少填写一个指定话题")
        enabled = bool(values.get("enabled"))
        original_section = str(values.get("original_section", "")).strip()

        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("section_topic_policies", {})
            if original_section and original_section != section.value:
                policies.pop(original_section, None)
            policies[section.value] = {
                "enabled": enabled,
                "required_hashtags": hashtags,
            }

        return self._mutate(mutate, bump_policy=True)

    def delete_section_topic_policy(self, section_value: str) -> str:
        try:
            section = Section(str(section_value))
        except ValueError as exc:
            raise ValueError("指定话题规则不存在") from exc

        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("section_topic_policies", {})
            if section.value not in policies:
                raise ValueError("指定话题规则不存在")
            policies.pop(section.value)

        return self._mutate(mutate, bump_policy=True)

    def upsert_content_policy(self, values: Dict[str, Any]) -> str:
        name = str(values.get("name", "")).strip()
        guidance = str(values.get("guidance", "")).strip()
        keywords = self._parse_list(values.get("keywords", ""), 30, 80)
        action = str(values.get("action", "review")).strip()
        if not name or len(name) > 80:
            raise ValueError("策略名称长度必须为 1–80 个字符")
        if not keywords:
            raise ValueError("请至少填写一个触发词")
        if not guidance or len(guidance) > 500:
            raise ValueError("检查要求长度必须为 1–500 个字符")
        if action not in {"notice", "review", "delete_candidate"}:
            raise ValueError("发现后的处理方式无效")
        index_value = str(values.get("index", "")).strip()
        index = int(index_value) if index_value else None
        policy = {
            "name": name,
            "keywords": keywords,
            "guidance": guidance,
            "action": action,
            "enabled": bool(values.get("enabled")),
        }

        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("content_policies", [])
            if not isinstance(policies, list):
                raise ValueError("主题策略配置无效")
            if index is None:
                if any(str(item.get("name", "")).casefold() == name.casefold() for item in policies):
                    raise ValueError("同名主题策略已经存在")
                policies.append(policy)
                return
            if index < 0 or index >= len(policies):
                raise ValueError("主题策略不存在")
            if any(
                position != index
                and str(item.get("name", "")).casefold() == name.casefold()
                for position, item in enumerate(policies)
            ):
                raise ValueError("同名主题策略已经存在")
            policies[index] = policy

        return self._mutate(mutate, bump_policy=True)

    def delete_content_policy(self, index: int) -> str:
        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("content_policies", [])
            if not isinstance(policies, list) or index < 0 or index >= len(policies):
                raise ValueError("主题策略不存在")
            policies.pop(index)

        return self._mutate(mutate, bump_policy=True)

    def upsert_board(self, values: Dict[str, Any]) -> str:
        channel_id = str(values.get("channel_id", "")).strip()
        if not channel_id.isdigit():
            raise ValueError("请选择有效的栏目")
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("栏目名称长度必须为 1–80 个字符")
        expected = [str(value) for value in values.get("expected_sections", [])]
        allowed = {section.value for section in Section}
        if not expected or any(value not in allowed for value in expected):
            raise ValueError("至少选择一个有效栏目")

        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("board_policies", {})
            policies[channel_id] = {
                "name": name,
                "expected_sections": expected,
                "require_hashtag": bool(values.get("require_hashtag")),
                "min_text_length": max(0, min(int(values.get("min_text_length", 1)), 10000)),
                "allow_external_links": bool(values.get("allow_external_links")),
            }
            channel_sections = raw.setdefault("channel_sections", {})
            if len(expected) == 1 and expected[0] != Section.UNCLASSIFIED.value:
                channel_sections[channel_id] = expected[0]
            else:
                channel_sections.pop(channel_id, None)

        return self._mutate(mutate, bump_policy=True)

    def delete_board(self, channel_id: str) -> str:
        channel_id = str(channel_id).strip()

        def mutate(raw: Dict[str, Any]) -> None:
            policies = raw.setdefault("board_policies", {})
            if channel_id not in policies:
                raise ValueError("栏目规则不存在")
            policies.pop(channel_id)
            raw.setdefault("channel_sections", {}).pop(channel_id, None)

        return self._mutate(mutate, bump_policy=True)

    def _mutate(self, callback: Callable[[Dict[str, Any]], None], bump_policy: bool) -> str:
        with self._lock:
            raw = self.snapshot()
            callback(raw)
            if bump_policy:
                raw.setdefault("moderation", {})["policy_version"] = self._next_version(
                    str(raw.setdefault("moderation", {}).get("policy_version", ""))
                )
            payload = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
            self._validate_payload(payload)
            backup_path = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            shutil.copy2(self.config_path, backup_path)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.config_path.name}.",
                dir=str(self.config_path.parent),
                text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.config_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return str(raw["moderation"]["policy_version"])

    def _validate_payload(self, payload: str) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".config-validation-",
            suffix=".json",
            dir=str(self.config_path.parent),
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            GuardConfig.from_file(temporary)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _parse_list(
        raw_value: Any,
        max_items: int,
        max_length: int,
        *,
        strip_hash: bool = False,
    ) -> List[str]:
        result: List[str] = []
        seen = set()
        normalized_value = str(raw_value).replace("，", ",").replace("\n", ",")
        for item in normalized_value.split(","):
            value = item.strip()
            if strip_hash:
                value = value.lstrip("#").strip()
            key = value.casefold()
            if value and key not in seen:
                result.append(value[:max_length])
                seen.add(key)
        return result[:max_items]

    @staticmethod
    def _next_version(current: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        prefix, dot, suffix = current.rpartition(".")
        if dot and prefix == today and suffix.isdigit():
            return f"{today}.{int(suffix) + 1}"
        return f"{today}.1"
