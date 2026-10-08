# verifies: FR-007-14
"""Воркер експерименту `scripts/screen_worker.py` збирає джерело через пул за `config/rpc_pool.yaml` (T-143).

Що перевіряється (мережі немає: провайдери лише конструюються, `DeliveryService` підмінено, `analyze` не ходить):
- джерело — `PooledRpcSource` з провайдерами з конфігу, за наявними змінними середовища;
- воркер передає `fallback_profile="helius_free"` у `build_rpc_source` (використано на шляху «без файла»);
- без жодної змінної — гучна `ConfigError` з іменами змінних, без значень;
- одна змінна — пул з одним провайдером, профіль із YAML;
- після кожного токена в stdout рядок `rpc_pool …` (`stats_line()`);
- URL і ключі не потрапляють у stdout/stderr;
- пул закривається і при нормальному завершенні, і коли збірка сервісу падає;
- `.env.example` містить ті самі імена змінних, що `config/rpc_pool.yaml`, з порожніми значеннями.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from unmask.ingest.config import ConfigError
from unmask.ingest.rpc import pool_config
from unmask.ingest.rpc.http import RPC_PROFILES
from unmask.ingest.rpc.pool import PooledRpcSource
from unmask.ingest.rpc.pool_config import load_rpc_pool_config

ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "scripts" / "screen_worker.py"
RPC_CONFIG = ROOT / "config" / "rpc_pool.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
RPC_ENV_NAMES = ("UNMASK_RPC_URL", "UNMASK_RPC_URL_ALT", "UNMASK_RPC_URL_ALT1", "UNMASK_RPC_URL_ALT2")
SECRET = "SECRETKEY-7f3a91"


def _load_worker():
    spec = importlib.util.spec_from_file_location("screen_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, env: dict[str, str], *, fail_delivery: bool = False):
    """Запускає `main()` воркера з підміненим сервісом; повертає (worker, seen)."""
    for name in RPC_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    seen: dict = {"sources": [], "closed": 0, "build_calls": []}

    real_build = pool_config.build_rpc_source

    def spy_build(*args, **kwargs):
        seen["build_calls"].append((args, kwargs))
        return real_build(*args, **kwargs)

    real_close = PooledRpcSource.close

    def spy_close(self):
        seen["closed"] += 1
        real_close(self)

    monkeypatch.setattr(pool_config, "build_rpc_source", spy_build)
    monkeypatch.setattr(PooledRpcSource, "close", spy_close)

    worker = _load_worker()

    class FakeIngest:
        def __init__(self, cfg, src):
            seen["sources"].append(src)

    class FakeDelivery:
        def __init__(self, *args):
            if fail_delivery:
                raise RuntimeError("delivery wiring failed")

        def analyze(self, mint):
            return {"risk_score": 5, "band": "low", "clusters": []}

    monkeypatch.setattr(worker, "IngestService", FakeIngest)
    monkeypatch.setattr(worker, "DeliveryService", FakeDelivery)

    mints = tmp_path / "mints.jsonl"
    mints.write_text('{"mint": "MintOne1111111111111111111111111111111111"}\n'
                     '{"mint": "MintTwo2222222222222222222222222222222222"}\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["screen_worker.py", str(mints), str(tmp_path / "out.csv"), "0"])
    return worker, seen


def _two_providers_env() -> dict[str, str]:
    return {
        "UNMASK_RPC_URL": f"https://rpc.example.invalid/{SECRET}",
        "UNMASK_RPC_URL_ALT1": f"https://helius.example.invalid/{SECRET}-2",
    }


def test_worker_header_declares_fr_007_14() -> None:
    """FR-007-14: шапка воркера несе `impl: … FR-007-14` (trace перевіряє лише покриття FR по всіх файлах)."""
    header = WORKER_PATH.read_text(encoding="utf-8").splitlines()[:3]
    assert any(line.startswith("# impl:") and "FR-007-14" in line for line in header)


def test_worker_builds_pool_from_config(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: джерело — `PooledRpcSource` з провайдерами з конфігу (дві задані змінні → два провайдери)."""
    worker, seen = _run(monkeypatch, tmp_path, _two_providers_env())

    assert worker.main() == 0
    assert len(seen["sources"]) == 1
    src = seen["sources"][0]
    assert isinstance(src, PooledRpcSource)
    assert set(src.stats()) == {"rpcfast", "helius_2"}


def test_worker_passes_config_and_fallback_profile(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: `build_rpc_source(ROOT/config/rpc_pool.yaml, cfg, environ, fallback_profile="helius_free")`."""
    worker, seen = _run(monkeypatch, tmp_path, _two_providers_env())

    worker.main()
    assert len(seen["build_calls"]) == 1
    args, kwargs = seen["build_calls"][0]
    assert Path(args[0]) == RPC_CONFIG
    assert kwargs.get("fallback_profile") == "helius_free"


def test_worker_prints_stats_line_after_each_token(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: після кожного токена — рядок `rpc_pool …` (`stats_line()`) у stdout."""
    worker, _ = _run(monkeypatch, tmp_path, _two_providers_env())

    worker.main()
    lines = capsys.readouterr().out.splitlines()
    stats = [line for line in lines if line.startswith("rpc_pool ")]
    assert len(stats) == 2
    assert all(line.startswith("rpc_pool rpcfast requests=0") for line in stats)
    # рядок статистики йде одразу за прогресом того самого токена
    assert lines[0].startswith("[0] 1/2 ")
    assert lines[1].startswith("rpc_pool ")
    assert lines[2].startswith("[0] 2/2 ")
    assert lines[3].startswith("rpc_pool ")


def test_no_env_fails_loudly_with_variable_names(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: без жодної змінної — чесна `ConfigError` з іменами змінних, без значень."""
    worker, seen = _run(monkeypatch, tmp_path, {})

    with pytest.raises(ConfigError) as info:
        worker.main()
    message = str(info.value)
    assert "rpc_pool.yaml: no RPC provider available" in message
    assert "UNMASK_RPC_URL" in message
    assert seen["sources"] == []


def test_one_variable_builds_pool_of_one_with_yaml_profile(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: одна задана змінна → пул з одним провайдером; профіль береться з YAML (helius_free)."""
    worker, seen = _run(monkeypatch, tmp_path, {"UNMASK_RPC_URL_ALT2": f"https://helius.example.invalid/{SECRET}"})

    assert worker.main() == 0
    src = seen["sources"][0]
    assert isinstance(src, PooledRpcSource)
    assert set(src.stats()) == {"helius_3"}
    (provider,) = src._providers  # внутрішнє: вага провайдера = rate_per_second його профілю
    assert provider.weight == RPC_PROFILES["helius_free"]["rate_per_second"]


def test_urls_and_keys_never_reach_output(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14 / FR-007-12: URL і ключі не виводяться ні в stdout, ні в stderr."""
    env = {name: f"https://{name.lower()}.example.invalid/{SECRET}" for name in RPC_ENV_NAMES}
    worker, _ = _run(monkeypatch, tmp_path, env)

    worker.main()
    captured = capsys.readouterr()
    assert any(line.startswith("rpc_pool ") for line in captured.out.splitlines())  # вивід справді був
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_worker_closes_pool_on_normal_exit(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: наприкінці джерело закривається рівно раз."""
    worker, seen = _run(monkeypatch, tmp_path, _two_providers_env())

    worker.main()
    assert seen["closed"] == 1


def test_worker_closes_pool_when_wiring_fails(monkeypatch, tmp_path, capsys) -> None:
    """FR-007-14: якщо збірка сервісу впала після створення пулу, пул усе одно закривається."""
    worker, seen = _run(monkeypatch, tmp_path, _two_providers_env(), fail_delivery=True)

    with pytest.raises(RuntimeError):
        worker.main()
    assert seen["closed"] == 1


def test_env_example_names_match_rpc_pool_yaml(monkeypatch) -> None:
    """FR-007-14: `.env.example` містить саме ті імена змінних, що `config/rpc_pool.yaml`, з порожніми значеннями."""
    yaml_names = {spec.url_env for spec in load_rpc_pool_config(RPC_CONFIG).providers}
    example: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        example[name.strip()] = value.strip()
    example_names = {name for name in example if name.startswith("UNMASK_RPC_URL")}

    assert example_names == yaml_names
    assert all(example[name] == "" for name in yaml_names)
