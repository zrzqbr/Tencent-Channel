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

    def upsert_board(self, values: Dict[str, Any]) -> str:
        channel_id = str(values.get("channel_id", "")).strip()
        if not channel_id.isdigit():
            raise ValueError("版块 ID 必须为数字")
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("版块名称长度必须为 1–80 个字符")
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
                raise ValueError("版块规则不存在")
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
    def _next_version(current: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        prefix, dot, suffix = current.rpartition(".")
        if dot and prefix == today and suffix.isdigit():
            return f"{today}.{int(suffix) + 1}"
        return f"{today}.1"
