from typing import List, Optional, Sequence, Set

from .config import GuardConfig
from .models import ClassificationResult, IncomingContent, Section
from .normalization import extract_hashtags, extract_media_urls, extract_plain_text, normalize_text


_EXPLICIT_PRIORITY: Sequence[Section] = (
    Section.OFFICIAL_NEWS,
    Section.WEEKLY_QUESTION,
    Section.FEATURED,
    Section.PRACTICAL_ARTICLE,
    Section.QA_DISCUSSION,
)


class ContentClassifier:
    def __init__(self, config: GuardConfig) -> None:
        self.config = config

    def classify(self, item: IncomingContent) -> ClassificationResult:
        title = extract_plain_text(item.title)
        body = extract_plain_text(item.body)
        normalized_title = normalize_text(title)
        normalized_body = normalize_text(body)
        plain_text = normalize_text(f"{title}\n{body}")
        hashtags = extract_hashtags(f"{title}\n{body}")
        hashtag_set = set(hashtags)
        all_media = tuple(item.media_urls) + extract_media_urls(item.title) + extract_media_urls(item.body)
        has_media = bool(all_media)
        reasons: List[str] = []
        issues: List[str] = []

        mapped_section = self.config.channel_sections.get(item.channel_id)
        weekly_words_present = self._contains_any(plain_text, self.config.rules.weekly_phrase_keywords)
        topic_section = self._section_from_required_topic(hashtag_set)
        if topic_section is not None:
            topic = self._matching_required_topic(topic_section, hashtag_set)
            reasons.append(
                f"内容带有指定话题 #{topic}，按当前规则应归入{topic_section.display_name}"
            )
            return self._result(
                topic_section,
                1.0,
                reasons,
                hashtags,
                has_media,
                plain_text,
            )

        if mapped_section is Section.WEEKLY_QUESTION:
            if self._required_topic_missing(mapped_section, hashtag_set):
                return self._required_topic_missing_result(
                    mapped_section, hashtags, has_media, plain_text
                )
            if self.config.rules.weekly_requires_any_hashtag and not hashtags:
                return self._weekly_missing_topic(hashtags, has_media, plain_text)
            reasons.append("当前栏目是每周一问，且内容符合已设置的话题要求")
            return self._result(Section.WEEKLY_QUESTION, 1.0, reasons, hashtags, has_media, plain_text)

        if mapped_section is not None:
            if self._required_topic_missing(mapped_section, hashtag_set):
                return self._required_topic_missing_result(
                    mapped_section, hashtags, has_media, plain_text
                )
            reasons.append(f"子频道固定映射为{mapped_section.display_name}")
            return self._result(mapped_section, 1.0, reasons, hashtags, has_media, plain_text)

        explicit_section = self._section_from_hashtags(hashtag_set)
        if explicit_section is not None:
            if self._required_topic_missing(explicit_section, hashtag_set):
                return self._required_topic_missing_result(
                    explicit_section, hashtags, has_media, plain_text
                )
            reasons.append(f"命中栏目井号话题：#{self._matching_hashtag(explicit_section, hashtag_set)}")
            return self._result(explicit_section, 0.99, reasons, hashtags, has_media, plain_text)

        if weekly_words_present:
            if self._required_topic_missing(Section.WEEKLY_QUESTION, hashtag_set):
                return self._required_topic_missing_result(
                    Section.WEEKLY_QUESTION, hashtags, has_media, plain_text
                )
            if self.config.rules.weekly_requires_any_hashtag and not hashtags:
                return self._weekly_missing_topic(hashtags, has_media, plain_text)
            reasons.append("正文出现每周一问语义，并带有井号话题")
            return self._result(Section.WEEKLY_QUESTION, 0.92, reasons, hashtags, has_media, plain_text)

        if item.author_id in self.config.official_author_ids:
            reasons.append("作者在官方账号白名单中")
            return self._result(Section.OFFICIAL_NEWS, 0.98, reasons, hashtags, has_media, plain_text)

        practical_words_present = self._contains_any(plain_text, self.config.rules.practical_keywords)
        if has_media and (
            practical_words_present or len(plain_text) >= self.config.rules.min_practical_text_length
        ):
            reasons.append("内容为图文结合，并包含案例/教程特征或达到文章长度；文章内部问号不作为问答优先依据")
            return self._result(Section.PRACTICAL_ARTICLE, 0.78, reasons, hashtags, has_media, plain_text)

        title_is_question = self._is_question_or_discussion(normalized_title)
        short_body_is_question = (
            len(normalized_body) < self.config.rules.min_practical_text_length
            and self._is_question_or_discussion(normalized_body)
        )
        if title_is_question or short_body_is_question:
            reasons.append("标题呈提问语气，或短正文包含求助/讨论表达")
            return self._result(Section.QA_DISCUSSION, 0.82, reasons, hashtags, has_media, plain_text)

        if has_media:
            reasons.append("检测到图文内容，但信息不足以确定是实用文章还是精华")
        else:
            reasons.append("未命中栏目话题、互动语气或图文文章规则")
        return self._result(
            Section.UNCLASSIFIED,
            0.35,
            reasons,
            hashtags,
            has_media,
            plain_text,
            issues=issues,
        )

    def _weekly_missing_topic(
        self, hashtags: Sequence[str], has_media: bool, plain_text: str
    ) -> ClassificationResult:
        return self._result(
            Section.UNCLASSIFIED,
            0.95,
            ["出现每周一问语义，但没有井号话题，按规则不能进入每周一问"],
            hashtags,
            has_media,
            plain_text,
            issues=["missing_weekly_hashtag"],
        )

    def _required_topic_missing_result(
        self,
        section: Section,
        hashtags: Sequence[str],
        has_media: bool,
        plain_text: str,
    ) -> ClassificationResult:
        topics = self._required_topic_labels(section)
        issue = (
            "missing_weekly_hashtag"
            if section is Section.WEEKLY_QUESTION
            else f"missing_required_hashtag:{section.value}"
        )
        return self._result(
            Section.UNCLASSIFIED,
            0.95,
            [
                f"内容准备归入{section.display_name}，但缺少当前指定话题：{topics}"
            ],
            hashtags,
            has_media,
            plain_text,
            issues=[issue],
        )

    def _section_from_hashtags(self, hashtags: Set[str]) -> Optional[Section]:
        for section in _EXPLICIT_PRIORITY:
            if self._has_section_hashtag(section, hashtags):
                return section
        return None

    def _section_from_required_topic(self, hashtags: Set[str]) -> Optional[Section]:
        for section in _EXPLICIT_PRIORITY:
            policy = self.config.section_topic_policies.get(section)
            if policy is None or not policy.enabled:
                continue
            configured = {normalize_text(value).lstrip("#") for value in policy.required_hashtags}
            if configured.intersection(hashtags):
                return section
        return None

    def _required_topic_missing(self, section: Section, hashtags: Set[str]) -> bool:
        policy = self.config.section_topic_policies.get(section)
        if policy is None or not policy.enabled:
            return False
        configured = {normalize_text(value).lstrip("#") for value in policy.required_hashtags}
        return not bool(configured.intersection(hashtags))

    def _matching_required_topic(self, section: Section, hashtags: Set[str]) -> str:
        policy = self.config.section_topic_policies.get(section)
        if policy is None:
            return ""
        display_by_normalized = {
            normalize_text(value).lstrip("#"): value for value in policy.required_hashtags
        }
        match = sorted(set(display_by_normalized).intersection(hashtags))[0]
        return display_by_normalized[match]

    def _required_topic_labels(self, section: Section) -> str:
        policy = self.config.section_topic_policies.get(section)
        if policy is None:
            return ""
        return " 或 ".join(f"#{value}" for value in policy.required_hashtags)

    def _has_section_hashtag(self, section: Section, hashtags: Set[str]) -> bool:
        configured = self.config.section_hashtags.get(section, set())
        return bool(configured.intersection(hashtags))

    def _matching_hashtag(self, section: Section, hashtags: Set[str]) -> str:
        matches = self.config.section_hashtags.get(section, set()).intersection(hashtags)
        return sorted(matches)[0] if matches else ""

    def _is_question_or_discussion(self, text: str) -> bool:
        return (
            "?" in text
            or "？" in text
            or self._contains_any(text, self.config.rules.question_keywords)
        )

    @staticmethod
    def _contains_any(text: str, keywords: Sequence[str]) -> bool:
        normalized_keywords = (normalize_text(keyword) for keyword in keywords)
        return any(keyword and keyword in text for keyword in normalized_keywords)

    def _result(
        self,
        section: Section,
        confidence: float,
        reasons: Sequence[str],
        hashtags: Sequence[str],
        has_media: bool,
        plain_text: str,
        issues: Sequence[str] = (),
    ) -> ClassificationResult:
        featured_candidate = (
            section is not Section.FEATURED
            and has_media
            and len(plain_text) >= self.config.rules.min_featured_text_length
        )
        return ClassificationResult(
            section=section,
            confidence=confidence,
            reasons=tuple(reasons),
            hashtags=tuple(hashtags),
            validation_issues=tuple(issues),
            featured_candidate=featured_candidate,
        )
