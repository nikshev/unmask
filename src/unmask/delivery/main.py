# impl: FR-004-10, FR-007-13, FR-007-14, FR-008-07, FR-008-08
"""Точка входу доставки: env → YAML → сервіси → HTTP-потік + polling-бот.

Секрети — лише з оточення (`UNMASK_BOT_TOKEN` і змінні RPC-провайдерів з `config/rpc_pool.yaml`,
а без нього `UNMASK_RPC_URL`); відсутня змінна —
fail-fast із назвою змінної ДО відкриття портів. У репозиторії секретів немає.

Межі навантаження (FR-008-08) — лише з `config/protection.yaml`; помилка конфігу — fail-fast (код 2, назва
поля) ДО порту й потоку бота. `Protection` будується РІВНО ОДИН раз і діляться ним сервіс (слоти) і HTTP (вікно
запитів, довірені проксі): бот береться за тим самим сервісом, тож глобальна межа важких аналізів спільна, а ліміти
користувача бота (`protection.users`) передаються в `run_polling(gate=...)` (FR-008-07).
Прапорець `--host` — адреса прив'язки (умовчання `0.0.0.0`; за nginx на тій самій машині — `127.0.0.1`).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Mapping

from unmask.clusters.config import load_cluster_config
from unmask.clusters.service import ClusterService
from unmask.delivery.bot import HttpxBotTransport, run_polling
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.config import load_delivery_config, load_protection_config
from unmask.delivery.http import serve
from unmask.delivery.limits import build_protection
from unmask.delivery.render import render_png
from unmask.delivery.service import DeliveryService
from unmask.graph.service import GraphService
from unmask.hubs.config import ConfigError as HubConfigError
from unmask.hubs.config import load_hub_config
from unmask.ingest.config import ConfigError as IngestConfigError
from unmask.ingest.config import load_config
from unmask.ingest.rpc.pool_config import build_rpc_source
from unmask.ingest.service import IngestService

__all__ = ["main"]

BOT_TOKEN_VAR = "UNMASK_BOT_TOKEN"
# Частота логу лічильників пулу (FR-007-13). Впливає лише на те, як часто пишеться рядок, не на результат (принцип III).
STATS_LOG_SECONDS = 60
_LOG = logging.getLogger("unmask.delivery")


def _log_stats_if_changed(source, last: str) -> str:
    """Один крок: логує `source.stats_line()`, якщо рядок змінився з `last`. Повертає останній залогований рядок."""
    line = source.stats_line()
    if line != last:
        _LOG.info("%s", line)
        return line
    return last


def _stats_loop(source, interval: float, stop: threading.Event) -> None:
    """Тіло daemon-потоку: раз на `interval` с викликає крок логування, доки `stop` не встановлено.

    Збій `stats_line` не вбиває потік і не зупиняє сервер: одне попередження на серію збоїв, без тексту винятку
    (він може містити URL з ключем) — лише назва типу.
    """
    last = ""
    warned = False
    while not stop.wait(interval):
        try:
            last = _log_stats_if_changed(source, last)
            warned = False
        except Exception as exc:  # noqa: BLE001 — лог-потік не має права вбивати сервіс
            if not warned:
                _LOG.warning("stats_line failed (%s); will retry in %s s", type(exc).__name__, interval)
                warned = True


def _ensure_logging() -> None:
    """INFO з `unmask.delivery` має дійти до stderr/файлу. Налаштовуємо root лише коли хендлерів ще немає."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Запустити доставку. Повертає код виходу; блокється в `serve_forever` до Ctrl-C."""
    parser = argparse.ArgumentParser(description="Unmask: API і Telegram-бот кластерів токена")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: all; behind nginx on the same host use 127.0.0.1)")
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
        protection_cfg = load_protection_config(config_dir / "protection.yaml")
    except (HubConfigError, IngestConfigError) as exc:
        print(f"serve: bad config: {exc}", file=sys.stderr)
        return 2
    bot_token = env.get(BOT_TOKEN_VAR)
    if not args.no_bot and not bot_token:
        print(f"serve: {BOT_TOKEN_VAR}: not set", file=sys.stderr)
        return 2
    try:
        source = build_rpc_source(config_dir / "rpc_pool.yaml", ingest_cfg, dict(env))
    except (HubConfigError, IngestConfigError) as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2

    _ensure_logging()
    protection = build_protection(protection_cfg)  # один на процес: сервіс і HTTP ділять його
    service = DeliveryService(
        IngestService(ingest_cfg, source),
        GraphService(hub_cfg),
        ClusterService(cluster_cfg),
        DeliveryCache(),
        slots=protection.slots,
    )
    render = lambda doc: render_png(doc, delivery_cfg)  # noqa: E731 — wiring, не логіка

    if not args.no_bot:
        transport = HttpxBotTransport(bot_token or "")
        thread = threading.Thread(
            target=run_polling, args=(transport, service),
            kwargs={"render": render, "evidence_limit": delivery_cfg.evidence_preview_limit,
                    "gate": protection.users},
            daemon=True)
        thread.start()
    if getattr(source, "stats_line", None):  # без stats_line (одиночний провайдер без пулу) потоку немає
        threading.Thread(target=_stats_loop, args=(source, STATS_LOG_SECONDS, threading.Event()),
                         daemon=True).start()
    serve(service, args.port or delivery_cfg.http_port, host=args.host, protection=protection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
