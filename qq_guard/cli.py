import argparse
import asyncio
import json
from dataclasses import asdict

from .classifier import ContentClassifier
from .config import GuardConfig
from .models import IncomingContent, ItemKind
from .moderation import ModerationEngine
from .scan_control import ScanLock
from .service import DryRunDeleteAdapter, GuardService
from .storage import AuditStore
from .tencent_cli import TencentCliClient
from .tencent_monitor import TencentChannelMonitor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ频道栏目检测与重复治理工具")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify", help="在本地测试一条内容的分类")
    classify.add_argument("--title", default="")
    classify.add_argument("--body", required=True)
    classify.add_argument("--media", action="append", default=[])
    classify.add_argument("--author", default="demo-user")
    classify.add_argument("--channel", default="demo-channel")

    moderate = subparsers.add_parser("moderate", help="在本地测试分类、敏感词和审核决策")
    moderate.add_argument("--title", default="")
    moderate.add_argument("--body", required=True)
    moderate.add_argument("--media", action="append", default=[])
    moderate.add_argument("--author", default="demo-user")
    moderate.add_argument("--channel", default="demo-channel")

    audit = subparsers.add_parser("audit", help="查看最近审计记录")
    audit.add_argument("--limit", type=int, default=30)
    audit.add_argument("--duplicates-only", action="store_true")
    audit.add_argument("--review-only", action="store_true")

    resolve = subparsers.add_parser("review-resolve", help="标记一条本地人工审核记录")
    resolve.add_argument("--id", type=int, required=True)
    resolve.add_argument("--resolution", choices=["approved", "rejected", "deleted", "ignored"], required=True)
    resolve.add_argument("--reviewer", required=True)
    resolve.add_argument("--notes", default="")

    subparsers.add_parser("dashboard", help="输出可视化后台可直接使用的汇总 JSON")

    subparsers.add_parser("tencent-scan", help="通过腾讯官方 CLI 对真实频道执行一次巡检")
    subparsers.add_parser("tencent-sync", help="只同步腾讯频道内容，不执行 AI 巡检")
    subparsers.add_parser("tencent-monitor", help="持续轮询腾讯频道并处理连续重复帖子")
    return parser


async def _classify(args: argparse.Namespace, config: GuardConfig) -> None:
    service = GuardService(
        config=config,
        classifier=ContentClassifier(config),
        store=AuditStore(config.database_path),
        delete_adapter=DryRunDeleteAdapter(),
    )
    item = IncomingContent(
        platform_item_id="local-demo",
        kind=ItemKind.FORUM_THREAD,
        guild_id="local-demo-guild",
        channel_id=args.channel,
        author_id=args.author,
        title=args.title,
        body=args.body,
        media_urls=tuple(args.media),
    )
    result = service.classifier.classify(item)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))


def _moderate(args: argparse.Namespace, config: GuardConfig) -> None:
    item = IncomingContent(
        platform_item_id="local-demo",
        kind=ItemKind.FORUM_THREAD,
        guild_id="local-demo-guild",
        channel_id=args.channel,
        author_id=args.author,
        title=args.title,
        body=args.body,
        media_urls=tuple(args.media),
    )
    classification = ContentClassifier(config).classify(item)
    assessment = ModerationEngine(config).evaluate(item, classification)
    print(
        json.dumps(
            {"classification": asdict(classification), "moderation": asdict(assessment)},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def main() -> None:
    args = _parser().parse_args()
    config = GuardConfig.from_file(args.config)
    if args.command == "classify":
        asyncio.run(_classify(args, config))
        return
    if args.command == "moderate":
        _moderate(args, config)
        return
    if args.command in {"tencent-scan", "tencent-sync", "tencent-monitor"}:
        monitor = TencentChannelMonitor(config, TencentCliClient())
        if args.command == "tencent-scan":
            with ScanLock(config.database_path) as acquired:
                if not acquired:
                    raise RuntimeError("已有一轮腾讯频道巡检正在运行")
                report = monitor.scan_once()
            print(json.dumps(report.public_summary(), ensure_ascii=False, indent=2))
        elif args.command == "tencent-sync":
            with ScanLock(config.database_path) as acquired:
                if not acquired:
                    print(
                        json.dumps(
                            {"status": "skipped", "reason": "sync_or_review_running"},
                            ensure_ascii=False,
                        )
                    )
                    return
                report = monitor.sync_once()
            print(json.dumps(report.public_summary(), ensure_ascii=False, indent=2))
        else:
            monitor.run_forever()
        return
    store = AuditStore(config.database_path)
    if args.command == "review-resolve":
        store.resolve_review(args.id, args.resolution, args.reviewer, args.notes)
        print(json.dumps({"success": True, "id": args.id, "resolution": args.resolution}, ensure_ascii=False))
        return
    if args.command == "dashboard":
        print(json.dumps(store.dashboard_summary(), ensure_ascii=False, indent=2))
        return
    if args.review_only:
        payload = store.review_queue(args.limit)
    else:
        payload = store.recent_events(args.limit, args.duplicates_only)
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
