from typing import Optional, Protocol

from .classifier import ContentClassifier
from .config import GuardConfig
from .models import DeleteResult, GuardDecision, IncomingContent, ItemKind, ModerationAction
from .moderation import ModerationEngine, duplicate_policy_reason
from .storage import AuditStore


class DeleteAdapter(Protocol):
    async def delete(self, item: IncomingContent) -> DeleteResult:
        ...


class DryRunDeleteAdapter:
    async def delete(self, item: IncomingContent) -> DeleteResult:
        return DeleteResult(status="dry_run")


class BotpyDeleteAdapter:
    def __init__(self, api: object, hide_recall_tip: bool = False) -> None:
        self.api = api
        self.hide_recall_tip = hide_recall_tip

    async def delete(self, item: IncomingContent) -> DeleteResult:
        try:
            if item.kind is ItemKind.FORUM_THREAD:
                await self.api.delete_thread(item.channel_id, item.platform_item_id)
            else:
                await self.api.recall_message(
                    item.channel_id,
                    item.platform_item_id,
                    hidetip=self.hide_recall_tip,
                )
        except Exception as exc:
            return DeleteResult(status="failed", error=f"{type(exc).__name__}: {exc}")
        return DeleteResult(status="deleted")


class GuardService:
    def __init__(
        self,
        config: GuardConfig,
        classifier: ContentClassifier,
        store: AuditStore,
        delete_adapter: DeleteAdapter,
        moderation_engine: Optional[ModerationEngine] = None,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.store = store
        self.delete_adapter = delete_adapter
        self.moderation_engine = moderation_engine or ModerationEngine(config)

    async def handle(self, item: IncomingContent) -> GuardDecision:
        classification = self.classifier.classify(item)
        moderation = self.moderation_engine.evaluate(item, classification)
        check = self.store.record_and_check(item, classification, moderation)
        reasons = list(moderation.reasons)

        if check.is_redelivery:
            return GuardDecision(
                classification=classification,
                duplicate=False,
                previous_platform_item_id=None,
                delete_status="redelivery_ignored",
                redelivery=True,
                moderation=moderation,
                recommended_action="ignore_redelivery",
                decision_reasons=tuple(reasons),
            )

        if check.is_duplicate:
            duplicate_reason = duplicate_policy_reason(check.previous_platform_item_id or "")
            reasons.append(duplicate_reason)
            recommended_action = ModerationAction.DELETE_CANDIDATE.value
            if not self.config.auto_delete_duplicates:
                self.store.update_delete_result(check.event_row_id, "disabled")
                status = "disabled"
                review_status = "pending"
            else:
                outcome = await self.delete_adapter.delete(item)
                self.store.update_delete_result(check.event_row_id, outcome.status, outcome.error)
                status = outcome.status
                review_status = "deleted" if outcome.status == "deleted" else "pending"
        elif moderation.action is ModerationAction.DELETE_CANDIDATE:
            recommended_action = moderation.action.value
            is_auto_delete_eligible = any(
                reason.auto_delete_eligible for reason in moderation.reasons
            )
            if self.config.auto_delete_policy_violations and is_auto_delete_eligible:
                outcome = await self.delete_adapter.delete(item)
                self.store.update_delete_result(check.event_row_id, outcome.status, outcome.error)
                status = outcome.status
                review_status = "deleted" if outcome.status == "deleted" else "pending"
            else:
                status = "review_required"
                review_status = "pending"
                self.store.update_delete_result(check.event_row_id, status)
        elif moderation.action is ModerationAction.REVIEW:
            recommended_action = moderation.action.value
            status = "review_required"
            review_status = "pending"
            self.store.update_delete_result(check.event_row_id, status)
        else:
            recommended_action = moderation.action.value
            status = "not_needed"
            review_status = "not_required"

        self.store.update_decision(
            check.event_row_id,
            recommended_action,
            tuple(reasons),
            review_status,
        )

        return GuardDecision(
            classification=classification,
            duplicate=check.is_duplicate,
            previous_platform_item_id=check.previous_platform_item_id,
            delete_status=status,
            moderation=moderation,
            recommended_action=recommended_action,
            decision_reasons=tuple(reasons),
        )
