# verifies: FR-007-13, FR-007-12
"""Лічильники пулу (T-141; contracts/pooled-rpc-source.md «Лічильники», data-model `ProviderStats`).

Оператор має бачити, що кожен провайдер справді обслуговує трафік (FR-007-13), і нічого секретного при цьому
не витікає (FR-007-12): ні URL, ні ключів у `stats()`, `stats_line()` чи логах. Перевіряємо:

- `requests` і `units` за викликом: частка `get_transactions` — кількість її підписів, інші звернення — 1;
  порожній виклик не рахується;
- `failures` за класами (`budget` раніше за `timeout`, усі п'ять ключів завжди присутні, з нулями);
- `not_found_escalations` (неархівний → архівний після `None` або короткої сторінки);
- `cooldowns` росте з кожною постановкою паузи;
- потоки: лічильники не губляться при одночасних викликах;
- `stats()` — знімок: мутація знімка не змінює пул;
- `stats_line()` дослівно за контрактом; `close()` ідемпотентний, закриває джерела по одному разу навіть при винятку
  в одному з них, пише рівно один рядок `stats_line()` у логер `unmask.ingest.rpc.pool` без URL.

Без мережі: `ScriptedProvider` поверх власних `FixtureRpcSource`, `FakeClock`, `SequentialRunner`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from pathlib import Path
from types import MappingProxyType

import pytest

from rpc_pool_doubles import ScriptedFailure, ScriptedProvider, SequentialRunner
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, ThreadRunner
from unmask.ingest.rpc.protocol import (
    RpcBudgetTimeout,
    RpcError,
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
)

NOW = 1_700_000_000
ADDRESS = "ADDR"
LOGGER = "unmask.ingest.rpc.pool"
CLASSES = ("budget", "timeout", "rate_limited", "unavailable", "other")
SECRET_URL = "https://rpc.secret.example/?api-key=SENTINEL-KEY-0123456789"


def _signatures(n: int) -> list[str]:
    return [f"sig{i:03d}" for i in range(n)]


@pytest.fixture
def scenario(tmp_path) -> Path:
    """60 транзакцій з власними підписами й blockTime; один адрес з accountInfo і короткою історією підписів."""
    rpc = {
        "getAccountInfo": {ADDRESS: {"lamports": 1}},
        "getSignaturesForAddress": {
            ADDRESS: [
                {"signature": "s-new", "slot": 2, "err": None, "memo": None, "blockTime": NOW - 5,
                 "confirmationStatus": "finalized"},
                {"signature": "s-old", "slot": 1, "err": None, "memo": None, "blockTime": NOW - 50,
                 "confirmationStatus": "finalized"},
            ]
        },
        "getTransaction": {
            sig: {"slot": 1000 + i, "blockTime": NOW + i, "signature": sig}
            for i, sig in enumerate(_signatures(60))
        },
    }
    (tmp_path / "rpc.json").write_text(json.dumps(rpc), encoding="utf-8")
    return tmp_path


class _ClosingProvider(ScriptedProvider):
    """ScriptedProvider із лічильником `close()` (власного для тесту закриття)."""

    def __init__(self, inner, *, close_error: BaseException | None = None, **kwargs) -> None:
        super().__init__(inner, **kwargs)
        self.closes = 0
        self._close_error = close_error

    def close(self) -> None:
        self.closes += 1
        if self._close_error is not None:
            raise self._close_error


def _provider(name: str, source, weight: float = 1.0, **overrides) -> Provider:
    values = dict(name=name, source=source, weight=weight, archival=True, retention_seconds=None)
    values.update(overrides)
    return Provider(**values)


def _pool(providers, *, runner=None, clock=None, cooldown: float = 30.0) -> PooledRpcSource:
    return PooledRpcSource(
        providers,
        clock=clock or FakeClock(wall_start=NOW),
        rate_limit_cooldown_seconds=cooldown,
        age_cache_entries=1000,
        runner=runner or SequentialRunner(),
    )


def _deadline(clock: FakeClock | None = None, seconds: float = 600.0) -> Deadline:
    return Deadline(clock or FakeClock(), seconds)


def _two_providers(scenario_dir, *, failures_0=(), weights=(1.0, 1.0), clock=None, cooldown: float = 30.0):
    """Два архівні провайдери `rpcfast` і `helius_1`; `failures_0` — збої першого (за ім'ям з конфігу)."""
    rpcfast = ScriptedProvider(FixtureRpcSource(scenario_dir), failures=failures_0)
    helius = ScriptedProvider(FixtureRpcSource(scenario_dir))
    pool = _pool(
        [_provider("rpcfast", rpcfast, weights[0]), _provider("helius_1", helius, weights[1])],
        clock=clock,
        cooldown=cooldown,
    )
    return pool


def _fail(exc, method: str = "getTransaction", calls=None) -> ScriptedFailure:
    return ScriptedFailure(method, None if calls is None else frozenset(calls), exc)


# --------------------------------------------------------------------------- порожній пул і знімок


def test_fresh_pool_counters_are_zero_in_config_order(scenario) -> None:
    pool = _two_providers(scenario)
    stats = pool.stats()
    assert list(stats) == ["rpcfast", "helius_1"]
    for name in ("rpcfast", "helius_1"):
        entry = stats[name]
        assert (entry.requests, entry.units, entry.not_found_escalations, entry.cooldowns) == (0, 0, 0, 0)
        assert dict(entry.failures) == {cls: 0 for cls in CLASSES}


def test_empty_get_transactions_is_not_counted(scenario) -> None:
    pool = _two_providers(scenario)
    assert pool.get_transactions([], deadline=_deadline()) == []
    entry = pool.stats()["rpcfast"]
    assert (entry.requests, entry.units) == (0, 0)


def test_stats_snapshot_does_not_change_with_pool(scenario) -> None:
    pool = _two_providers(scenario)
    before = pool.stats()
    pool.get_account_info(ADDRESS, deadline=_deadline())
    after = pool.stats()
    assert sum(s.requests for s in before.values()) == 0  # знімок «до» не змінився
    assert sum(s.requests for s in after.values()) == 1
    with pytest.raises(TypeError):
        before["rpcfast"].failures["budget"] = 5  # type: ignore[index]
    with pytest.raises(TypeError):
        before["rpcfast"] = None  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        before["rpcfast"].requests = 7  # type: ignore[misc]
    assert sum(s.requests for s in pool.stats().values()) == 1  # пул не постраждав від спроб мутації


def test_provider_stats_is_frozen_dataclass_with_contract_fields() -> None:
    import unmask.ingest.rpc.pool as pool_module

    stats_cls = pool_module.ProviderStats  # AttributeError до реалізації — очікувана червона причина
    assert dataclasses.is_dataclass(stats_cls)
    assert {f.name for f in dataclasses.fields(stats_cls)} == {
        "requests", "units", "failures", "not_found_escalations", "cooldowns",
    }
    assert stats_cls.__dataclass_params__.frozen is True


# --------------------------------------------------------------------------- requests і units


def test_stats_count_requests_and_units_per_provider(scenario) -> None:
    pool = _two_providers(scenario, weights=(1.0, 3.0))
    results = pool.get_transactions(_signatures(40), deadline=_deadline())
    assert len(results) == 40 and all(r is not None for r in results)
    stats = pool.stats()
    # apportion(40, [1, 3]) = [10, 30]: одна частка на провайдера, одиниць = підписів частки
    assert (stats["rpcfast"].requests, stats["rpcfast"].units) == (1, 10)
    assert (stats["helius_1"].requests, stats["helius_1"].units) == (1, 30)
    pool.get_account_info(ADDRESS, deadline=_deadline())  # одиничний виклик: 1 звернення, 1 одиниця
    stats = pool.stats()
    assert sum(s.requests for s in stats.values()) == 3
    assert sum(s.units for s in stats.values()) == 41


def test_single_call_units_are_one_per_call(scenario) -> None:
    pool = _two_providers(scenario)
    for _ in range(3):
        pool.get_account_info(ADDRESS, deadline=_deadline())
    stats = pool.stats()
    assert sum(s.requests for s in stats.values()) == 3
    assert sum(s.units for s in stats.values()) == 3


# --------------------------------------------------------------------------- failures за класами


@pytest.mark.parametrize(
    ("cls", "exc"),
    [
        ("budget", RpcBudgetTimeout()),
        ("timeout", RpcTimeout("request timed out")),
        ("rate_limited", RpcRateLimited(2.5)),
        ("unavailable", RpcUnavailable("http 5xx server error")),
        ("other", RpcError("other failure")),
    ],
)
def test_failures_counted_by_class(scenario, cls: str, exc: RpcError) -> None:
    pool = _two_providers(scenario, failures_0=[_fail(exc, calls={1})])
    results = pool.get_transactions(_signatures(40), deadline=_deadline())
    assert all(r is not None for r in results)  # перебирання: відмова не втрачає позицій
    rpcfast, helius = pool.stats()["rpcfast"], pool.stats()["helius_1"]
    assert dict(rpcfast.failures) == {c: (1 if c == cls else 0) for c in CLASSES}
    assert rpcfast.requests == 1 and rpcfast.units == 20  # невдала спроба теж звернення
    assert dict(helius.failures) == {c: 0 for c in CLASSES}
    assert helius.requests == 2 and helius.units == 40  # другий раунд: частка rpcfast перейшла сюди


def test_budget_is_counted_as_budget_not_timeout(scenario) -> None:
    # RpcBudgetTimeout — підклас RpcTimeout: лічильник класу має бути `budget`, а `timeout` — нуль.
    pool = _two_providers(scenario, failures_0=[_fail(RpcBudgetTimeout(), calls={1})])
    pool.get_transactions(_signatures(4), deadline=_deadline())
    failures = pool.stats()["rpcfast"].failures
    assert failures["budget"] == 1 and failures["timeout"] == 0


def test_single_call_failure_is_counted_and_failover_served(scenario) -> None:
    # Рівні ваги: перший одиничний виклик іде до rpcfast (нічия — порядок конфігу).
    pool = _two_providers(scenario, failures_0=[_fail(RpcUnavailable("down"), method="getAccountInfo", calls={1})])
    assert pool.get_account_info(ADDRESS, deadline=_deadline()) == {"lamports": 1}
    stats = pool.stats()
    assert stats["rpcfast"].requests == 1 and stats["rpcfast"].units == 1
    assert stats["rpcfast"].failures["unavailable"] == 1
    assert stats["helius_1"].requests == 1 and stats["helius_1"].failures["unavailable"] == 0


def test_cooldowns_grow_with_each_pause(scenario) -> None:
    clock = FakeClock(wall_start=NOW)
    pool = _two_providers(
        scenario,
        failures_0=[_fail(RpcRateLimited(2.5), calls={1, 2})],
        clock=clock,
        cooldown=30.0,
    )
    pool.get_transactions(_signatures(4), deadline=_deadline(clock))
    assert pool.stats()["rpcfast"].cooldowns == 1
    clock.advance(31.0)  # пауза скінчилась: rpcfast знову отримує частку і знову отримує 429
    pool.get_transactions(_signatures(4), deadline=_deadline(clock))
    stats = pool.stats()
    assert stats["rpcfast"].cooldowns == 2
    assert stats["rpcfast"].failures["rate_limited"] == 2
    assert stats["helius_1"].cooldowns == 0


def test_cooldown_not_counted_without_rate_limit(scenario) -> None:
    pool = _two_providers(scenario, failures_0=[_fail(RpcTimeout("slow"), calls={1})])
    pool.get_transactions(_signatures(4), deadline=_deadline())
    assert pool.stats()["rpcfast"].cooldowns == 0


# --------------------------------------------------------------------------- escalations


def test_escalations_counted(scenario) -> None:
    # Неархівний `rpcfast` віддає None для старих (blockTime < horizon) — 30 позицій передано архівному.
    nonarchival = _ClosingProvider(FixtureRpcSource(scenario), horizon_block_time=NOW + 30)
    archival = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool(
        [
            _provider("rpcfast", nonarchival, 1.0, archival=False, retention_seconds=10_000_000),
            _provider("helius_1", archival, 1.0),
        ]
    )
    results = pool.get_transactions(_signatures(60), deadline=_deadline())
    assert all(r is not None for r in results)
    stats = pool.stats()
    assert stats["rpcfast"].not_found_escalations == 30
    assert stats["helius_1"].not_found_escalations == 0


def test_escalation_counted_for_short_nonarchival_signature_page(scenario) -> None:
    # Єдиний провайдер — неархівний: коротка сторінка (2 < limit 10) -> escalation і RpcUnavailable.
    nonarchival = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", nonarchival, 1.0, archival=False, retention_seconds=10_000_000)])
    with pytest.raises(RpcUnavailable):
        pool.get_signatures_for_address(ADDRESS, before=None, until=None, limit=10, deadline=_deadline())
    stats = pool.stats()["rpcfast"]
    assert stats.not_found_escalations == 1
    assert (stats.requests, stats.units) == (1, 1)


# --------------------------------------------------------------------------- потоки


def test_counts_are_not_lost_under_threads(scenario) -> None:
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario))
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = PooledRpcSource(
        [_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)],
        clock=FakeClock(wall_start=NOW),
        rate_limit_cooldown_seconds=30.0,
        age_cache_entries=1000,
        runner=ThreadRunner(2),
    )
    threads, rounds = 4, 10
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(rounds):
                pool.get_transactions(_signatures(4), deadline=_deadline())
        except BaseException as exc:  # noqa: BLE001 — збираємо для звіту в головному потоці
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    try:
        assert errors == []
        stats = pool.stats()
        # кожен виклик: 4 підписи = 2 + 2 за рівних ваг, один звернення на провайдера
        assert stats["rpcfast"].requests == threads * rounds
        assert stats["helius_1"].requests == threads * rounds
        assert stats["rpcfast"].units == threads * rounds * 2
        assert stats["helius_1"].units == threads * rounds * 2
    finally:
        pool.close()


# --------------------------------------------------------------------------- stats_line


def test_stats_line_exact_format(scenario) -> None:
    pool = _two_providers(scenario)
    assert pool.stats_line() == (
        "rpc_pool rpcfast requests=0 units=0 failures=0 escalations=0 cooldowns=0; "
        "helius_1 requests=0 units=0 failures=0 escalations=0 cooldowns=0"
    )


def test_stats_line_exact_format_with_counts(scenario) -> None:
    # rpcfast отримує 2 підписи, відмовляє rate_limited -> пауза; helius_1 доробляє обидві позиції.
    pool = _two_providers(scenario, failures_0=[_fail(RpcRateLimited(2.5), calls={1})])
    pool.get_transactions(_signatures(4), deadline=_deadline())
    assert pool.stats_line() == (
        "rpc_pool rpcfast requests=1 units=2 failures=1 escalations=0 cooldowns=1; "
        "helius_1 requests=2 units=4 failures=0 escalations=0 cooldowns=0"
    )


# --------------------------------------------------------------------------- close і лог


def test_close_logs_stats_line_once_without_urls(scenario, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = RpcUnavailable(f"request to {SECRET_URL} failed")  # текст винятку з URL не має потрапити в лог
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario), failures=[_fail(leaky, calls={1})])
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)])
    pool.get_transactions(_signatures(4), deadline=_deadline())
    line = pool.stats_line()

    pool.close()
    pool.close()  # ідемпотентно: другий виклик нічого не пише

    records = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    assert [r.getMessage() for r in records] == [line]
    assert not [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]
    joined = caplog.text + line
    assert "SENTINEL-KEY" not in joined and "rpc.secret.example" not in joined and "://" not in joined


def test_close_closes_each_source_once_and_is_idempotent(scenario) -> None:
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario))
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)])
    pool.close()
    pool.close()
    assert (rpcfast.closes, helius.closes) == (1, 1)


def test_close_closes_all_sources_despite_one_failing(scenario, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    broken = _ClosingProvider(FixtureRpcSource(scenario), close_error=ValueError("close failed"))
    healthy = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", broken, 1.0), _provider("helius_1", healthy, 1.0)])
    with pytest.raises(ValueError, match="close failed"):
        pool.close()
    assert (broken.closes, healthy.closes) == (1, 1)  # друге джерело закрите попри виняток першого
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO] == [
        pool.stats_line()
    ]
    pool.close()  # повторний виклик: нічого не закриває, нічого не логує, не піднімає знову
    assert (broken.closes, healthy.closes) == (1, 1)
    assert len([r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]) == 1


def test_stats_snapshot_is_mapping_proxy_type(scenario) -> None:
    pool = _two_providers(scenario)
    assert isinstance(pool.stats()["rpcfast"].failures, MappingProxyType)


def test_snapshot_failures_do_not_change_after_later_failure(scenario) -> None:
    # Знімок `failures` — копія, а не вид на живий лічильник: відмова після знімка його не змінює.
    pool = _two_providers(scenario, failures_0=[_fail(RpcUnavailable("down"), method="getAccountInfo", calls={1})])
    before = pool.stats()
    pool.get_account_info(ADDRESS, deadline=_deadline())
    assert dict(before["rpcfast"].failures) == {cls: 0 for cls in CLASSES}
    assert pool.stats()["rpcfast"].failures["unavailable"] == 1


class _CountingRunner:
    """Runner-дубль: виконує задачі послідовно і рахує виклики close()."""

    def __init__(self) -> None:
        self.closes = 0
        self._inner = SequentialRunner()

    def __call__(self, tasks):
        return self._inner(tasks)

    def close(self) -> None:
        self.closes += 1


def test_close_closes_runner_once(scenario) -> None:
    runner = _CountingRunner()
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario))
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool(
        [_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)], runner=runner
    )
    pool.close()
    pool.close()
    assert runner.closes == 1


def test_stats_line_failures_is_sum_over_classes(scenario) -> None:
    # Два різні класи одного провайдера: failures= має бути сумою (2), а не максимумом (1).
    pool = _two_providers(
        scenario,
        failures_0=[_fail(RpcTimeout("slow"), calls={1}), _fail(RpcUnavailable("down"), calls={2})],
    )
    pool.get_transactions(_signatures(4), deadline=_deadline())
    pool.get_transactions(_signatures(4), deadline=_deadline())
    entry = pool.stats()["rpcfast"]
    assert entry.failures["timeout"] == 1 and entry.failures["unavailable"] == 1
    assert pool.stats_line() == (
        "rpc_pool rpcfast requests=2 units=4 failures=2 escalations=0 cooldowns=0; "
        "helius_1 requests=4 units=8 failures=0 escalations=0 cooldowns=0"
    )


def test_close_closes_shared_source_once(scenario) -> None:
    # Одне джерело під двома іменами провайдерів: close() закриває його один раз.
    shared = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", shared, 1.0), _provider("helius_1", shared, 1.0)])
    pool.close()
    assert shared.closes == 1


class _MalformedProvider(ScriptedProvider):
    """Джерело, що на getTransaction віддає частку неправильної довжини (хибна форма)."""

    def get_transactions(self, signatures, *, deadline):
        with self._call("getTransaction", tuple(signatures)):
            return []


def test_malformed_share_counted_as_unavailable(scenario) -> None:
    rpcfast = _MalformedProvider(FixtureRpcSource(scenario))
    helius = ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)])
    results = pool.get_transactions(_signatures(4), deadline=_deadline())
    assert all(r is not None for r in results)  # частка перебрана на helius_1
    entry = pool.stats()["rpcfast"]
    assert entry.failures["unavailable"] == 1
    assert sum(entry.failures.values()) == 1


def test_context_manager_returns_self_and_closes_once(scenario) -> None:
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario))
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)])
    with pool as entered:
        assert entered is pool
    assert (rpcfast.closes, helius.closes) == (1, 1)


def test_context_manager_does_not_swallow_exception(scenario, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rpcfast = _ClosingProvider(FixtureRpcSource(scenario))
    helius = _ClosingProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("rpcfast", rpcfast, 1.0), _provider("helius_1", helius, 1.0)])
    with pytest.raises(KeyError, match="boom"):
        with pool:
            raise KeyError("boom")
    assert (rpcfast.closes, helius.closes) == (1, 1)
    assert len([r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]) == 1
