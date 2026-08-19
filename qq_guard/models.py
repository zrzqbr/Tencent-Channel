from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class Section(str, Enum):
    FEATURED = "featured"
    WEEKLY_QUESTION = "weekly_question"
    PRACTICAL_ARTICLE = "practical_article"
    QA_DISCUSSION = "qa_discussion"
    OFFICIAL_NEWS = "official_news"
    UNCLASSIFIED = "unclassified"

    @property
    def display_name(self) -> str:
        return {
            Section.FEATURED: "精华",
            Section.WEEKLY_QUESTION: "每周一问",
            Section.PRACTICAL_ARTICLE: "实用文章",
            Section.QA_DISCUSSION: "问答与交流",
            Section.OFFICIAL_NEWS: "官方资讯",
            Section.UNCLASSIFIED: "待管理员确认",
        }[self]


class ItemKind(str, Enum):
    FORUM_THREAD = "forum_thread"
    CHANNEL_MESSAGE = "channel_message"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ModerationAction(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    DELETE_CANDIDATE = "delete_candidate"


@dataclass(frozen=True)
class IncomingContent:
    platform_item_id: str
    kind: ItemKind
    guild_id: str
    channel_id: str
    author_id: str
    title: str = ""
    body: str = ""
    media_urls: Tuple[str, ...] = field(default_factory=tuple)
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ClassificationResult:
    section: Section
    confidence: float
    reasons: Tuple[str, ...]
    hashtags: Tuple[str, ...]
    validation_issues: Tuple[str, ...] = field(default_factory=tuple)
    featured_candidate: bool = False


@dataclass(frozen=True)
class PolicyReason:
    code: str
    category: str
    severity: str
    message: str
    evidence: str = ""
    score: int = 0
    auto_delete_eligible: bool = False


@dataclass(frozen=True)
class ModerationAssessment:
    action: ModerationAction
    risk_level: RiskLevel
    risk_score: int
    policy_version: str
    reasons: Tuple[PolicyReason, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AIReviewDecision:
    section: Section
    classification_confidence: float
    risk_level: RiskLevel
    risk_score: int
    recommended_action: ModerationAction
    summary: str
    reasons: Tuple[PolicyReason, ...] = field(default_factory=tuple)
    provider: str = "tencent_tokenhub"
    model: str = ""
    vision_model: str = ""
    vision_analysis: str = ""
    vision_status: str = "not_requested"
    prompt_version: str = ""
    status: str = "completed"
    error: str = ""


@dataclass(frozen=True)
class DuplicateCheck:
    event_row_id: int
    is_duplicate: bool
    previous_platform_item_id: Optional[str]
    is_redelivery: bool = False


@dataclass(frozen=True)
class DeleteResult:
    status: str
    error: Optional[str] = None


@dataclass(frozen=True)
class GuardDecision:
    classification: ClassificationResult
    duplicate: bool
    previous_platform_item_id: Optional[str]
    delete_status: str
    redelivery: bool = False
    moderation: Optional[ModerationAssessment] = None
    recommended_action: str = "allow"
    decision_reasons: Tuple[PolicyReason, ...] = field(default_factory=tuple)
