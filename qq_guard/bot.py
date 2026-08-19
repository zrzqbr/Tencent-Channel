import json
import os
from dataclasses import asdict
from typing import List, Tuple

from .classifier import ContentClassifier
from .config import GuardConfig
from .models import IncomingContent, ItemKind
from .service import BotpyDeleteAdapter, DryRunDeleteAdapter, GuardService
from .storage import AuditStore


def _forum_text_and_media(container: object) -> Tuple[str, Tuple[str, ...]]:
    text_parts: List[str] = []
    media_urls: List[str] = []
    for paragraph in getattr(container, "paragraphs", []) or []:
        for element in getattr(paragraph, "elems", []) or []:
            element_type = getattr(element, "type", None)
            if element_type == 1:
                value = getattr(getattr(element, "text", None), "text", None)
                if value:
                    text_parts.append(str(value))
            elif element_type == 2:
                value = getattr(getattr(getattr(element, "image", None), "plat_image", None), "url", None)
                if value:
                    media_urls.append(str(value))
            elif element_type == 3:
                value = getattr(getattr(getattr(element, "video", None), "plat_video", None), "url", None)
                if value:
                    media_urls.append(str(value))
            elif element_type == 4:
                link = getattr(element, "url", None)
                description = getattr(link, "desc", None)
                url = getattr(link, "url", None)
                if description:
                    text_parts.append(str(description))
                if url:
                    text_parts.append(str(url))
    return "\n".join(text_parts), tuple(media_urls)


def _build_client(config: GuardConfig):
    try:
        import botpy
        from botpy.forum import Thread
        from botpy.message import Message
    except ImportError as exc:
        raise RuntimeError("缺少 qq-botpy，请先执行 pip install -e '.[qq]'") from exc

    class GuardBot(botpy.Client):
        def __init__(self) -> None:
            intents = botpy.Intents(guild_messages=True, forums=True)
            super().__init__(intents=intents)
            store = AuditStore(config.database_path)
            adapter = (
                BotpyDeleteAdapter(self.api, hide_recall_tip=config.hide_recall_tip)
                if config.delete_mode == "live"
                else DryRunDeleteAdapter()
            )
            self.guard_service = GuardService(
                config=config,
                classifier=ContentClassifier(config),
                store=store,
                delete_adapter=adapter,
            )

        async def on_ready(self) -> None:
            _log({"event": "ready", "robot": getattr(self.robot, "name", "unknown"), "delete_mode": config.delete_mode})

        async def on_message_create(self, message: Message) -> None:
            if getattr(message.author, "bot", False):
                return
            media = tuple(
                str(attachment.url)
                for attachment in (message.attachments or [])
                if getattr(attachment, "url", None)
            )
            item = IncomingContent(
                platform_item_id=str(message.id),
                kind=ItemKind.CHANNEL_MESSAGE,
                guild_id=str(message.guild_id),
                channel_id=str(message.channel_id),
                author_id=str(message.author.id),
                body=message.content or "",
                media_urls=media,
                created_at=message.timestamp,
            )
            await self._guard(item)

        async def on_forum_thread_create(self, thread: Thread) -> None:
            title, title_media = _forum_text_and_media(thread.thread_info.title)
            body, body_media = _forum_text_and_media(thread.thread_info.content)
            item = IncomingContent(
                platform_item_id=str(thread.thread_info.thread_id),
                kind=ItemKind.FORUM_THREAD,
                guild_id=str(thread.guild_id),
                channel_id=str(thread.channel_id),
                author_id=str(thread.author_id),
                title=title,
                body=body,
                media_urls=title_media + body_media,
                created_at=thread.thread_info.date_time,
            )
            await self._guard(item)

        async def _guard(self, item: IncomingContent) -> None:
            decision = await self.guard_service.handle(item)
            _log(
                {
                    "event": "content_checked",
                    "platform_item_id": item.platform_item_id,
                    "kind": item.kind.value,
                    "section": decision.classification.section.value,
                    "section_name": decision.classification.section.display_name,
                    "confidence": decision.classification.confidence,
                    "hashtags": decision.classification.hashtags,
                    "validation_issues": decision.classification.validation_issues,
                    "featured_candidate": decision.classification.featured_candidate,
                    "risk_level": decision.moderation.risk_level.value if decision.moderation else "low",
                    "risk_score": decision.moderation.risk_score if decision.moderation else 0,
                    "policy_version": decision.moderation.policy_version if decision.moderation else "",
                    "recommended_action": decision.recommended_action,
                    "decision_reasons": [asdict(reason) for reason in decision.decision_reasons],
                    "duplicate": decision.duplicate,
                    "previous_platform_item_id": decision.previous_platform_item_id,
                    "delete_status": decision.delete_status,
                }
            )

    return GuardBot()


def _log(payload: object) -> None:
    if hasattr(payload, "__dataclass_fields__"):
        payload = asdict(payload)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def main() -> None:
    config_path = os.environ.get("QQ_GUARD_CONFIG", "config.json")
    app_id = os.environ.get("QQBOT_APP_ID")
    app_secret = os.environ.get("QQBOT_APP_SECRET")
    if not app_id or not app_secret:
        raise SystemExit("请设置 QQBOT_APP_ID 和 QQBOT_APP_SECRET 环境变量")
    config = GuardConfig.from_file(config_path)
    client = _build_client(config)
    client.run(appid=app_id, secret=app_secret)


if __name__ == "__main__":
    main()
