# impl: FR-004-10
"""Точка входу доставки: env → YAML → сервіси → HTTP-потік + polling-бот.

Секрети — лише з оточення (`UNMASK_RPC_URL`, `UNMASK_BOT_TOKEN`); відсутня змінна —
fail-fast із назвою змінної ДО відкриття портів. У репозиторії секретів немає.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Mapping

from unmask.clusters.config import load_cluster_config
from unmask.clusters.service import ClusterService
from unmask.delivery.bot import HttpxBotTransport, run_polling
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.config import load_delivery_config
from unmask.delivery.http import serve
from unmask.delivery.render import render_png
from unmask.delivery.service import DeliveryService
from unmask.graph.service import GraphService
from unmask.hubs.config import ConfigError as HubConfigError
from unmask.hubs.config import load_hub_config
from unmask.ingest.config import ConfigError as IngestConfigError
from unmask.ingest.config import load_config
from unmask.ingest.rpc.http import HttpRpcSource
from unmask.ingest.service import IngestService

__all__ = ["main"]

BOT_TOKEN_VAR = "UNMASK_BOT_TOKEN"


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Запустити доставку. Повертає код виходу; блокється в `serve_forever` до Ctrl-C."""
    parser = argparse.ArgumentParser(description="Unmask: API і Telegram-бот кластерів токена")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-bot", action="store_true")
    parser.add_argument("--config-dir", default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    root = Path(__file__).resolve().parents[3]
    config_dir = Path(args.config_dir) if args.config_dir else root / "config"

    try:
        delivery_cfg = load_delivery_config(config_dir / "delivery.yaml")
        ingest_cfg = load_config(config_dir / "ingest.yaml")
        hub_cfg = load_hub_config(config_dir / "hubs.yaml", config_dir / "hub_addresses.yaml")
        cluster_cfg = load_cluster_config(config_dir / "clusters.yaml")
    except (HubConfigError, IngestConfigError) as exc:
        print(f"serve: bad config: {exc}", file=sys.stderr)
        return 2
    try:
        source = HttpRpcSource.from_env(ingest_cfg.rpc, commitment=ingest_cfg.commitment,
                                        environ=dict(env))
    except (HubConfigError, IngestConfigError) as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    bot_token = env.get(BOT_TOKEN_VAR)
    if not args.no_bot and not bot_token:
        print(f"serve: {BOT_TOKEN_VAR}: not set", file=sys.stderr)
        return 2

    service = DeliveryService(
        IngestService(ingest_cfg, source),
        GraphService(hub_cfg),
        ClusterService(cluster_cfg),
        DeliveryCache(),
    )
    render = lambda doc: render_png(doc, delivery_cfg)  # noqa: E731 — wiring, не логіка

    if not args.no_bot:
        transport = HttpxBotTransport(bot_token or "")
        thread = threading.Thread(
            target=run_polling, args=(transport, service),
            kwargs={"render": render, "evidence_limit": delivery_cfg.evidence_preview_limit},
            daemon=True)
        thread.start()
    serve(service, args.port or delivery_cfg.http_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
