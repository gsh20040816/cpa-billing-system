from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import uvicorn

from .bot import run_bot
from .config import Settings
from .database import Database
from .migrate import migrate_legacy_bot
from .services import BillingService


def build() -> tuple[Settings, BillingService]:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    service = BillingService(settings, database)
    service.bootstrap()
    return settings, service


def worker(service: BillingService, interval: float, once: bool) -> None:
    while True:
        try:
            imported = service.sync_cpamp()
            rated = service.rate_events(limit=500)
            logging.info("worker imported=%s rated=%s", imported, rated)
        except Exception:
            logging.exception("usage sync iteration failed")
        try:
            keys = service.sync_cpa_keys()
            logging.info(
                "worker keys_current=%s keys_created=%s keys_retired=%s",
                keys["current"],
                keys["created"],
                keys["retired"],
            )
        except Exception:
            logging.exception("CPA key sync iteration failed")
        if once:
            return
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sync = sub.add_parser("sync"); sync.add_argument("--once", action="store_true")
    migrate = sub.add_parser("migrate-legacy"); migrate.add_argument("path", type=Path); migrate.add_argument("--dry-run", action="store_true")
    preview = sub.add_parser("preview"); preview.add_argument("cycle")
    close = sub.add_parser("close-cycle"); close.add_argument("cycle"); close.add_argument("operator", type=int); close.add_argument("--confirm-waiver", action="store_true")
    sub.add_parser("reconcile")
    price_sync = sub.add_parser("sync-prices")
    price_sync.add_argument("--name")
    price_sync.add_argument("--reason", required=True)
    reprice = sub.add_parser("rerate-active")
    reprice.add_argument("--name")
    reprice.add_argument("--reason", required=True)
    sub.add_parser("sync-keys")
    sub.add_parser("bot")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="0.0.0.0"); serve.add_argument("--port", type=int, default=18417)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings, service = build()
    if args.command == "init":
        print("initialized", settings.database_path)
    elif args.command == "sync":
        worker(service, settings.worker_interval_seconds, args.once)
    elif args.command == "migrate-legacy":
        print(json.dumps(migrate_legacy_bot(service, args.path, args.dry_run), ensure_ascii=False))
    elif args.command == "preview":
        print(json.dumps(service.dashboard(args.cycle) if service.preview_cycle(args.cycle) else {}, ensure_ascii=False))
    elif args.command == "close-cycle":
        service.close_cycle(args.cycle, args.operator, args.confirm_waiver)
    elif args.command == "reconcile":
        print(json.dumps(service.reconciliation(record=True), ensure_ascii=False))
    elif args.command == "sync-prices":
        print(json.dumps(service.sync_upstream_prices(
            args.name,
            operator_type="cli-admin",
            operator_id="deployment",
            reason=args.reason,
        ), ensure_ascii=False))
    elif args.command == "rerate-active":
        print(json.dumps(service.republish_active_pricing(
            reason=args.reason,
            version_name=args.name,
        ), ensure_ascii=False))
    elif args.command == "sync-keys":
        print(json.dumps(service.sync_cpa_keys(), ensure_ascii=False))
    elif args.command == "bot":
        asyncio.run(run_bot())
    elif args.command == "serve":
        from .web import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()
