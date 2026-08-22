import re
import unicodedata
from typing import List, Optional, Tuple

from .config import GuardConfig, SensitiveTermRule
from .models import (
    ClassificationResult,
    IncomingContent,
    ModerationAction,
    ModerationAssessment,
    PolicyReason,
    RiskLevel,
    Section,
)
from .normalization import extract_plain_text, normalize_text


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_CONTACT_ID_RE = re.compile(
    r"(?:微信|wechat|weixin|vx|qq)\s*[:：号]?\s*[a-z0-9_-]{5,}",
    re.IGNORECASE,
)
_ONLY_LATIN_TOKEN_RE = re.compile(r"^[a-z]{6,20}$", re.IGNORECASE)

_SEVERITY_SCORE = {"low": 5, "medium": 20, "high": 45, "critical": 70}


class ModerationEngine:
    """Explainable content screening. It recommends actions but never deletes content."""

    def __init__(self, config: GuardConfig) -> None:
        self.config = config
        self.settings = config.moderation

    def evaluate(
        self,
        item: IncomingContent,
        classification: ClassificationResult,
    ) -> ModerationAssessment:
        if not self.settings.enabled:
            return ModerationAssessment(
                action=ModerationAction.ALLOW,
                risk_level=RiskLevel.LOW,
                risk_score=0,
                policy_version=self.settings.policy_version,
            )

        title = normalize_text(extract_plain_text(item.title))
        body = normalize_text(extract_plain_text(item.body))
        # Some QQ forum details repeat the title in the content field. Treating
        # that as two copies inflates length and creates false repeat signals.
        text = title if title and body == title else normalize_text(f"{title}\n{body}")
        compact_text = re.sub(r"\s+", "", text)
        reasons: List[PolicyReason] = []
        delete_candidate_hit = False

        for policy in self.config.content_policies:
            if not policy.enabled:
                continue
            matched_keywords = [
                keyword
                for keyword in policy.keywords
                if normalize_text(keyword) in text
            ]
            if not matched_keywords:
                continue
            severity, score = {
                "notice": ("low", 0),
                "review": ("medium", max(25, self.settings.review_threshold)),
                "delete_candidate": (
                    "high",
                    max(80, self.settings.delete_candidate_threshold),
                ),
            }[policy.action]
            reasons.append(
                PolicyReason(
                    code=f"content_policy_{policy.action}",
                    category="content_policy",
                    severity=severity,
                    message=f"涉及“{policy.name}”：{policy.guidance}",
                    evidence="触发内容：" + "、".join(matched_keywords[:5]),
                    score=score,
                    auto_delete_eligible=False,
                )
            )
            delete_candidate_hit = (
                delete_candidate_hit or policy.action == "delete_candidate"
            )

        for rule in self.settings.terms:
            evidence = self._match_sensitive_term(text, rule)
            if evidence is None:
                continue
            score = _SEVERITY_SCORE[rule.severity]
            language_name = {"zh": "中文", "en": "英文"}.get(
                rule.language.casefold(), rule.language
            )
            reasons.append(
                PolicyReason(
                    code=f"sensitive_term_{rule.language}",
                    category=rule.category,
                    severity=rule.severity,
                    message=f"命中{language_name}敏感词规则",
                    evidence=evidence,
                    score=score,
                    auto_delete_eligible=rule.action == "delete_candidate",
                )
            )
            delete_candidate_hit = delete_candidate_hit or rule.action == "delete_candidate"

        urls = _URL_RE.findall(text)
        if self.settings.detect_external_links and urls:
            reasons.append(
                PolicyReason(
                    code="external_link_detected",
                    category="information_screening",
                    severity="low",
                    message="内容包含外部链接，需要按所在栏目规则判断是否允许",
                    evidence=urls[0][:120],
                    score=5,
                )
            )

        if self.settings.detect_contact_information:
            contact_type = self._contact_type(text)
            if contact_type:
                reasons.append(
                    PolicyReason(
                        code="contact_information_detected",
                        category="information_screening",
                        severity="medium",
                        message="检测到联系方式或可识别的联系账号",
                        evidence=contact_type,
                        score=25,
                    )
                )

        board = self.config.board_policies.get(item.channel_id)
        min_length = self.settings.min_meaningful_length
        if board is not None:
            min_length = max(min_length, board.min_text_length)
            if board.expected_sections and classification.section not in board.expected_sections:
                expected = "、".join(section.display_name for section in board.expected_sections)
                reasons.append(
                    PolicyReason(
                        code="section_mismatch",
                        category="board_policy",
                        severity="medium",
                        message=f"内容分类与“{board.name}”栏目的发布要求不一致",
                        evidence=f"当前：{classification.section.display_name}；允许：{expected}",
                        score=30,
                    )
                )
            if board.require_hashtag and not classification.hashtags:
                reasons.append(
                    PolicyReason(
                        code="required_hashtag_missing",
                        category="board_policy",
                        severity="medium",
                        message=f"“{board.name}”栏目要求至少包含一个井号话题",
                        score=25,
                    )
                )
            if not board.allow_external_links and urls:
                reasons.append(
                    PolicyReason(
                        code="external_link_not_allowed",
                        category="board_policy",
                        severity="medium",
                        message=f"“{board.name}”栏目不允许外部链接",
                        evidence=urls[0][:120],
                        score=30,
                    )
                )

        meaningful_length = len(re.sub(r"[#\W_]+", "", compact_text, flags=re.UNICODE))
        if meaningful_length < min_length:
            reasons.append(
                PolicyReason(
                    code="low_information_content",
                    category="quality",
                    severity="medium",
                    message="正文有效信息量低于当前栏目最低要求",
                    evidence=f"有效字符 {meaningful_length}，最低要求 {min_length}",
                    score=25,
                )
            )

        if self._is_repeated_content(compact_text):
            reasons.append(
                PolicyReason(
                    code="repeated_characters",
                    category="quality",
                    severity="medium",
                    message="内容主要由重复字符组成，疑似灌水或测试内容",
                    score=25,
                )
            )

        if self._is_latin_gibberish(compact_text):
            reasons.append(
                PolicyReason(
                    code="possible_gibberish",
                    category="quality",
                    severity="medium",
                    message="内容疑似无语义的英文字母组合",
                    evidence=compact_text[:30],
                    score=25,
                )
            )

        if classification.section is Section.UNCLASSIFIED:
            reasons.append(
                PolicyReason(
                    code="classification_uncertain",
                    category="classification",
                    severity="medium",
                    message="现有证据不足以稳定归入任何栏目，需要人工复核",
                    score=25,
                )
            )
        for issue in classification.validation_issues:
            reasons.append(self._validation_reason(issue))

        risk_score = min(sum(reason.score for reason in reasons), 100)
        risk_level = self._risk_level(risk_score)
        has_high_risk_evidence = any(
            reason.severity in {"high", "critical"} for reason in reasons
        )
        if delete_candidate_hit or (
            risk_score >= self.settings.delete_candidate_threshold
            and has_high_risk_evidence
        ):
            action = ModerationAction.DELETE_CANDIDATE
        elif risk_score >= self.settings.review_threshold:
            action = ModerationAction.REVIEW
        else:
            action = ModerationAction.ALLOW
        return ModerationAssessment(
            action=action,
            risk_level=risk_level,
            risk_score=risk_score,
            policy_version=self.settings.policy_version,
            reasons=tuple(self._deduplicate_reasons(reasons)),
        )

    def _match_sensitive_term(self, text: str, rule: SensitiveTermRule) -> Optional[str]:
        term = normalize_text(rule.term)
        match_type = rule.match_type
        if match_type == "auto":
            match_type = "word" if rule.language.casefold() == "en" else "substring"
        if match_type == "regex":
            match = re.search(rule.term, text, re.IGNORECASE)
            return match.group(0) if match else None
        if match_type == "substring":
            if term in text:
                return rule.term
            if self.settings.detect_obfuscated_terms and len(term) >= 2:
                pattern = r"[\W_]*".join(re.escape(char) for char in term)
                if re.search(pattern, text, re.IGNORECASE):
                    return rule.term
            return None

        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        if re.search(pattern, text, re.IGNORECASE):
            return rule.term
        if self.settings.detect_obfuscated_terms and len(term) >= 2:
            spaced = r"[\W_]*".join(re.escape(char) for char in term)
            pattern = rf"(?<![a-z0-9]){spaced}(?![a-z0-9])"
            if re.search(pattern, text, re.IGNORECASE):
                return rule.term
        return None

    @staticmethod
    def _contact_type(text: str) -> str:
        if _PHONE_RE.search(text):
            return "中国大陆手机号"
        if _EMAIL_RE.search(text):
            return "电子邮箱"
        if _CONTACT_ID_RE.search(text):
            return "QQ/微信等联系账号"
        return ""

    @staticmethod
    def _is_repeated_content(text: str) -> bool:
        cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text, flags=re.IGNORECASE)
        if len(cleaned) < 4:
            return False
        return any(cleaned == unit * (len(cleaned) // len(unit)) for unit in (cleaned[:1], cleaned[:2]))

    @staticmethod
    def _is_latin_gibberish(text: str) -> bool:
        if not _ONLY_LATIN_TOKEN_RE.fullmatch(text):
            return False
        vowel_count = sum(char in "aeiou" for char in text.casefold())
        return vowel_count / len(text) < 0.2

    def _validation_reason(self, issue: str) -> PolicyReason:
        if issue == "missing_weekly_hashtag":
            policy = self.config.section_topic_policies.get(Section.WEEKLY_QUESTION)
            if policy and policy.enabled:
                required = " 或 ".join(f"#{value}" for value in policy.required_hashtags)
                return PolicyReason(
                    code=issue,
                    category="classification",
                    severity="medium",
                    message=f"每周一问缺少当前指定话题，请补充 {required}",
                    evidence=f"当前必须使用：{required}",
                    score=30,
                )
            return PolicyReason(
                code=issue,
                category="classification",
                severity="medium",
                message="内容具有每周一问语义，但缺少必需的井号话题",
                score=30,
            )
        if issue.startswith("missing_required_hashtag:"):
            section_value = issue.partition(":")[2]
            try:
                section = Section(section_value)
            except ValueError:
                section = Section.UNCLASSIFIED
            policy = self.config.section_topic_policies.get(section)
            required = " 或 ".join(
                f"#{value}" for value in policy.required_hashtags
            ) if policy else "规定话题"
            return PolicyReason(
                code=issue,
                category="classification",
                severity="medium",
                message=f"{section.display_name}缺少当前指定话题，请补充 {required}",
                evidence=f"当前必须使用：{required}",
                score=30,
            )
        return PolicyReason(
            code=issue,
            category="classification",
            severity="medium",
            message=f"分类校验未通过：{issue}",
            score=25,
        )

    @staticmethod
    def _risk_level(score: int) -> RiskLevel:
        if score >= 80:
            return RiskLevel.CRITICAL
        if score >= 50:
            return RiskLevel.HIGH
        if score >= 25:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _deduplicate_reasons(reasons: List[PolicyReason]) -> Tuple[PolicyReason, ...]:
        result: List[PolicyReason] = []
        seen = set()
        for reason in reasons:
            key = (reason.code, reason.evidence)
            if key not in seen:
                seen.add(key)
                result.append(reason)
        return tuple(result)


def duplicate_policy_reason(previous_platform_item_id: str) -> PolicyReason:
    return PolicyReason(
        code="exact_consecutive_duplicate",
        category="duplicate",
        severity="high",
        message="同一作者在同一频道、同一栏目内连续发布完全相同内容",
        evidence="已与上一条连续发布内容核对为完全相同",
        score=60,
        auto_delete_eligible=True,
    )
