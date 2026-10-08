# verifies: FR-007-14
"""Точка входу доставки збирає джерело через пул за `config/rpc_pool.yaml` (FR-007-14, T-139).

Сторожі: сервер не стартує (`serve` замінено), сокети заборонені, значення змінних середовища — сентинелі, які
не мають з'явитися ні у виводі, ні в помилках.
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest

from unmask.delivery import main as main_mod
from unmask.delivery.main import main
from unmask.ingest.rpc.pool import PooledRpcSource

CONFIG = Path(__file__).resolve().parents[1] / "config"
# Файли, які `main` читає разом із rpc_pool.yaml (delivery, ingest, hubs + hub_addresses, clusters, protection — 008 T-167).
COPIED = ("delivery.yaml", "ingest.yaml", "hubs.yaml", "hub_addresses.yaml", "clusters.yaml",
          "protection.yaml")

VAR_A = "UNMASK_RPC_URL_A"
VAR_B = "UNMASK_RPC_URL_B"
SENTINEL_A = "https://sentinel-a.invalid/rpc?api-key=SENTINEL_KEY_A_7f3e"
SENTINEL_B = "https://sentinel-b.invalid/rpc?api-key=SENTINEL_KEY_B_9c1d"
SENTINEL_BAD = "SENTINEL_NOT_A_URL_5a2b"

POOL_YAML = f"""\
version: 1
rate_limit_cooldown_seconds: 5
age_cache_entries: 1000
providers:
  - name: first
    url_env: {VAR_A}
    profile: helius_free
    archival: true
    retention_seconds: null
    skip_methods: []
  - name: second
    url_env: {VAR_B}
    profile: helius_free
    archival: true
    retention_seconds: null
    skip_methods: []
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    for name in COPIED:
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, no_network) -> dict:
    """Замінює сервер і збирач сервісів: фіксує джерело, яке `main` передав у `IngestService`."""
    record: dict = {}

    class RecIngest:
        def __init__(self, cfg, source):
            record["source"] = source

    class RecDelivery:
        def __init__(self, *args, **kwargs):
            record["delivery"] = True

    monkeypatch.setattr(main_mod, "IngestService", RecIngest)
    monkeypatch.setattr(main_mod, "DeliveryService", RecDelivery)
    monkeypatch.setattr(main_mod, "serve", lambda service, port, **kwargs: record.setdefault("served", True))
    return record


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("network access in a delivery test")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_main_builds_pool_from_config_dir(config_dir: Path, seen: dict) -> None:
    (config_dir / "rpc_pool.yaml").write_text(POOL_YAML, encoding="utf-8")
    environ = {VAR_A: SENTINEL_A, VAR_B: SENTINEL_B}

    assert main(["--no-bot", "--config-dir", str(config_dir)], environ=environ) == 0

    source = seen["source"]
    assert isinstance(source, PooledRpcSource)
    assert repr(source) == "PooledRpcSource(providers=('first', 'second'))"
    assert seen.get("served") is True


def test_main_without_rpc_pool_yaml_uses_unmask_rpc_url(config_dir: Path, seen: dict) -> None:
    # Без файла пул з одним провайдером `default` за `UNMASK_RPC_URL` — поведінка, як до фічі.
    assert not (config_dir / "rpc_pool.yaml").exists()
    environ = {"UNMASK_RPC_URL": SENTINEL_A}

    assert main(["--no-bot", "--config-dir", str(config_dir)], environ=environ) == 0

    source = seen["source"]
    assert isinstance(source, PooledRpcSource)
    assert repr(source) == "PooledRpcSource(providers=('default',))"
    assert seen.get("served") is True


def test_missing_pool_variables_fail_fast_without_values(config_dir: Path, seen: dict, capsys) -> None:
    (config_dir / "rpc_pool.yaml").write_text(POOL_YAML, encoding="utf-8")

    assert main(["--no-bot", "--config-dir", str(config_dir)], environ={}) == 2

    err = capsys.readouterr().err
    assert err.startswith("serve: ")
    assert VAR_A in err and VAR_B in err
    assert "served" not in seen


def test_invalid_pool_url_fails_fast_without_leaking_value(config_dir: Path, seen: dict, capsys) -> None:
    (config_dir / "rpc_pool.yaml").write_text(POOL_YAML, encoding="utf-8")
    environ = {VAR_A: SENTINEL_BAD, VAR_B: SENTINEL_B}

    assert main(["--no-bot", "--config-dir", str(config_dir)], environ=environ) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("serve: ")
    assert VAR_A in captured.err
    for text in (captured.out, captured.err):
        assert SENTINEL_BAD not in text
        assert SENTINEL_B not in text
    assert "served" not in seen
