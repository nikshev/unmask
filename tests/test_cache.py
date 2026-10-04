# verifies: FR-001-12
"""Кеш повних результатів і публічний сервіс (T-018, contracts/ingest-service.md).

`IngestService(config, source, clock, cache).collect(mint)`:
- крок 2: `complete` у кеші → копія з `metadata.served_from_cache=true`, решта ідентична першому
  поверненню, нуль звернень до джерела (журнал `FixtureRpcSource.calls` не росте);
- крок 3: `partial` поточної версії конфігу → `resume` (лише недоотримане); стан іншої версії
  відкидається — сервіс ніколи не передає його в `collect` (там це `ValueError`);
- крок 6: `complete` → `put_complete` (partial чиститься), `incomplete` → лише `put_partial`;
  результат повертається в обох випадках із чесним статусом.
Кроки 1 і 4 (адреса, існування токена) перевіряє `tests/test_service_rejections.py` (T-019); тут
важливо лише, що кожен шлях, який не віддається з кешу, робить рівно одне звернення `getAccountInfo`
перед збором (журнал сервісу = `[getAccountInfo(mint)] + журнал колектора`). Мережі немає: лише
`FixtureRpcSource` і `FakeClock`.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect
from unmask.ingest.config import load_config
from unmask.ingest.model import CacheInvariantError, CompletenessStatus, IngestResult, MissingReason
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB = SCENARIOS / "basic", SCENARIOS / "hub"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
M = EXPECTED["mint"]
A = EXPECTED["wallets"]["A"]
HUB_MINT = json.loads((HUB / "expected.json").read_text())["mint"]

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
RPC_ERRORS = [RpcUnavailable("node down"), RpcRateLimited(retry_after=1.0), RpcTimeout("slow")]
WALLET_METHODS = ("getSignaturesForAddress", "getTokenAccountsByOwner")


def _cfg(**overrides):
    cfg = dataclasses.replace(load_config(SHIPPED), **EXPECTED["config"])
    return dataclasses.replace(cfg, **overrides)


def _stable(result: IngestResult):
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _fresh(cfg, directory: Path = BASIC, mint: str = M):
    """Еталон: неперервний збір колектором напряму; повертає (результат, журнал джерела)."""
    source = FixtureRpcSource(directory)
    result = collect(CollectionState(mint=mint, config_version=cfg.version), source, cfg, FakeClock())
    return result, source.calls


def _service(cfg=None, *, directory: Path = BASIC, failures=None, clock=None, cache=None):
    clock = clock if clock is not None else FakeClock()
    source = FixtureRpcSource(directory, failures=failures, clock=clock)
    return IngestService(cfg or _cfg(), source, clock=clock, cache=cache), source


def _flagged(result: IngestResult) -> IngestResult:
    return dataclasses.replace(result, metadata=dataclasses.replace(result.metadata, served_from_cache=True))


# --- Крок 2: повний результат віддається з кешу -------------------------------------------


def test_second_collect_makes_zero_source_calls():
    service, source = _service()
    first = service.collect(M)
    assert first.completeness.status is CompletenessStatus.COMPLETE
    calls_after_first = len(source.calls)
    assert calls_after_first > 0
    service.collect(M)
    assert source.calls[calls_after_first:] == []  # журнал порожній між викликами
    service.collect(M)
    assert len(source.calls) == calls_after_first


def test_second_result_identical_except_served_from_cache_flag():
    service, _source = _service()
    first = service.collect(M)
    second = service.collect(M)
    assert first.metadata.served_from_cache is False
    assert second.metadata.served_from_cache is True
    # рівно один прапорець відрізняється: rpc_calls, analyzed_at, elapsed, resumed — від першого повернення
    assert second == _flagged(first)
    assert dataclasses.replace(second.metadata, served_from_cache=False) == first.metadata
    # копія не глибока: незмінні кортежі діляться (вартість копії не залежить від розміру результату)
    assert second.buyers is first.buyers and second.transfers is first.transfers
    assert second.completeness is first.completeness
    # перше повернення й збережений у кеші знімок не зачеплено
    assert first.metadata.served_from_cache is False
    assert service._cache.get_complete(M) == first
    assert service.collect(M) == second


def test_served_result_equals_fresh_collection_and_golden_mint():
    service, _source = _service()
    service.collect(M)
    served = service.collect(M)
    fresh, _calls = _fresh(_cfg())
    assert _stable(served) == _stable(fresh)
    assert served.metadata.mint == M


# --- Крок 6: неповний результат — лише partial, ніколи не остаточний ----------------------


def test_incomplete_result_is_not_in_complete_cache():
    cache = ResultCache()
    service, source = _service(failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    first = service.collect(M)
    assert first.completeness.status is CompletenessStatus.INCOMPLETE  # чесний статус повернуто
    assert cache.get_complete(M) is None
    partial = cache.get_partial(M)
    assert isinstance(partial, CollectionState) and partial.missing

    # повторний виклик не віддає неповний як остаточний: продовжує лише недоотримане
    calls_before = len(source.calls)
    second = service.collect(M)
    retry = source.calls[calls_before:]
    assert retry, "incomplete result must not be served from cache"
    assert second.metadata.served_from_cache is False
    assert second.metadata.resumed is True
    assert second.completeness.status is CompletenessStatus.COMPLETE
    wallets_requested = {p.get("address", p.get("owner")) for m, p in retry if m in WALLET_METHODS}
    assert A in wallets_requested
    assert not wallets_requested & partial.expanded, "expanded wallets must not be requested again"
    assert len(retry) < len(_fresh(_cfg())[1])
    assert _stable(second) == _stable(_fresh(_cfg())[0])

    # після повного успіху: partial порожній, результат у complete, наступний виклик — з кешу
    assert cache.get_partial(M) is None
    assert cache.get_complete(M) == second
    calls_before = len(source.calls)
    third = service.collect(M)
    assert source.calls[calls_before:] == []
    assert third == _flagged(second)


def test_still_incomplete_retry_stays_partial_and_reports_honestly():
    cache = ResultCache()
    service, _source = _service(failures=[FailFor(A, RpcUnavailable("down"), times=6)], cache=cache)
    results = [service.collect(M)]
    while results[-1].completeness.status is CompletenessStatus.INCOMPLETE:
        assert any(m.wallet == A for m in results[-1].completeness.missing)
        assert cache.get_complete(M) is None and cache.get_partial(M) is not None
        assert len(results) < 10
        results.append(service.collect(M))
    assert len(results) >= 3  # щонайменше два неповні повтори поспіль
    final = results[-1]
    assert _stable(final) == _stable(_fresh(_cfg())[0])
    assert final.completeness.status is CompletenessStatus.COMPLETE
    assert cache.get_partial(M) is None and cache.get_complete(M) == final


def test_put_complete_rejects_incomplete_with_cache_invariant_error():
    cache = ResultCache()
    service, _source = _service(failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    incomplete = service.collect(M)
    assert incomplete.completeness.status is CompletenessStatus.INCOMPLETE
    partial_before = cache.get_partial(M)
    with pytest.raises(CacheInvariantError):
        cache.put_complete(incomplete)
    assert cache.get_complete(M) is None
    assert cache.get_partial(M) == partial_before


@pytest.mark.parametrize("budget", [10.0, 20.0, 39.0])
def test_repeat_after_budget_exhausted_completes_and_moves_to_complete(budget):
    cache = ResultCache()
    clock = FakeClock(advance_per_call=1.0)
    service, source = _service(_cfg(time_budget_seconds=budget), clock=clock, cache=cache)
    results = [service.collect(M)]
    first = results[0]
    assert first.completeness.status is CompletenessStatus.INCOMPLETE
    assert (not first.completeness.buyers.complete) or any(
        m.reason is MissingReason.BUDGET_EXHAUSTED for m in first.completeness.missing)
    while results[-1].completeness.status is CompletenessStatus.INCOMPLETE:
        assert cache.get_complete(M) is None and cache.get_partial(M) is not None
        assert len(results) < 60, "resume makes no progress"
        results.append(service.collect(M))
    final = results[-1]
    assert final.metadata.resumed is True
    assert _stable(final) == _stable(_fresh(_cfg(time_budget_seconds=budget))[0])
    assert cache.get_partial(M) is None and cache.get_complete(M) == final
    calls_before = len(source.calls)
    assert service.collect(M) == _flagged(final)
    assert len(source.calls) == calls_before


# --- Крок 3: стан іншої версії конфігу відкидається ---------------------------------------


def test_stale_partial_is_discarded_without_value_error_and_equals_fresh():
    old_cfg = _cfg()
    new_cfg = _cfg(version=old_cfg.version + 1)
    # стан старої версії, обірваний збоєм, — у спільному кеші
    stale_source = FixtureRpcSource(BASIC, failures=[FailFor(A, RpcUnavailable("down"), times=1)])
    stale = CollectionState(mint=M, config_version=old_cfg.version)
    assert collect(stale, stale_source, old_cfg, FakeClock()).completeness.status is CompletenessStatus.INCOMPLETE
    cache = ResultCache()
    cache.put_partial(stale)
    with pytest.raises(ValueError):  # саме це сервіс мусить ніколи не отримати
        collect(cache.get_partial(M), FixtureRpcSource(BASIC), new_cfg, FakeClock())

    service, source = _service(new_cfg, cache=cache)
    result = service.collect(M)  # без ValueError
    fresh, fresh_calls = _fresh(new_cfg)
    assert _stable(result) == _stable(fresh)
    assert result.metadata.config_version == new_cfg.version
    assert result.metadata.resumed is False
    # збір заново, нічого зі старого стану не використано; перед збором — рівно одне звернення
    # кроку 4 (існування токена, T-019), якого колектор сам не робить
    assert source.calls == [("getAccountInfo", {"address": M})] + fresh_calls
    assert cache.get_partial(M) is None and cache.get_complete(M) == result


def test_stale_partial_replaced_by_current_partial_when_recollection_incomplete():
    old_cfg = _cfg()
    new_cfg = _cfg(version=old_cfg.version + 1)
    stale = CollectionState(mint=M, config_version=old_cfg.version)
    collect(stale, FixtureRpcSource(BASIC, failures=[FailFor(A, RpcUnavailable("x"), times=1)]),
            old_cfg, FakeClock())
    cache = ResultCache()
    cache.put_partial(stale)
    service, _source = _service(new_cfg, failures=[FailFor(A, RpcTimeout("slow"), times=1)], cache=cache)
    result = service.collect(M)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert cache.get_partial(M).config_version == new_cfg.version
    assert cache.get_complete(M) is None


def test_complete_result_of_other_config_version_is_not_served():
    old_cfg = _cfg()
    new_cfg = _cfg(version=old_cfg.version + 1)
    cache = ResultCache()
    old_service, _ = _service(old_cfg, cache=cache)
    old_result = old_service.collect(M)
    service, source = _service(new_cfg, cache=cache)
    result = service.collect(M)
    assert source.calls, "result of another config version must not be served (principle III)"
    assert result.metadata.served_from_cache is False
    assert result.metadata.config_version == new_cfg.version != old_result.metadata.config_version
    assert cache.get_complete(M) == result
    calls_before = len(source.calls)
    assert service.collect(M) == _flagged(result)
    assert len(source.calls) == calls_before


# --- Межі кешу ---------------------------------------------------------------------------


def test_cache_is_per_mint():
    cache = ResultCache()
    basic_service, basic_source = _service(cache=cache)
    hub_service, hub_source = _service(directory=HUB, cache=cache)
    basic = basic_service.collect(M)
    hub = hub_service.collect(HUB_MINT)  # кеш має M, але не HUB_MINT — збір іде
    assert hub_source.calls
    assert hub.metadata.mint == HUB_MINT and hub.metadata.served_from_cache is False
    assert cache.get_complete(M) == basic and cache.get_complete(HUB_MINT) == hub
    assert _stable(hub) == _stable(_fresh(_cfg(), HUB, HUB_MINT)[0])
    n_basic, n_hub = len(basic_source.calls), len(hub_source.calls)
    assert basic_service.collect(M) == _flagged(basic)
    assert hub_service.collect(HUB_MINT) == _flagged(hub)
    assert (len(basic_source.calls), len(hub_source.calls)) == (n_basic, n_hub)


def test_partial_of_one_mint_does_not_affect_another():
    cache = ResultCache()
    failing, _ = _service(failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    assert failing.collect(M).completeness.status is CompletenessStatus.INCOMPLETE
    hub_service, _ = _service(directory=HUB, cache=cache)
    hub = hub_service.collect(HUB_MINT)
    assert hub.metadata.resumed is False
    assert _stable(hub) == _stable(_fresh(_cfg(), HUB, HUB_MINT)[0])
    assert cache.get_partial(M) is not None and cache.get_complete(M) is None


def test_two_services_have_separate_caches():
    first_service, _ = _service()
    first_service.collect(M)
    second_service, second_source = _service()
    result = second_service.collect(M)
    assert second_source.calls
    assert result.metadata.served_from_cache is False


def test_external_cache_is_used():
    cache = ResultCache()
    service, _ = _service(cache=cache)
    result = service.collect(M)
    assert cache.get_complete(M) == result
    # переданий заздалегідь заповнений кеш — нуль звернень у новому сервісі
    other, other_source = _service(cache=cache)
    assert other.collect(M) == _flagged(result)
    assert other_source.calls == []


# --- Гарантія: збій джерела не ламає сервіс ----------------------------------------------


@pytest.mark.parametrize("exc", RPC_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("after", [1, 2, 5])
def test_source_failure_does_not_break_service(exc, after):
    cache = ResultCache()
    service, _ = _service(failures=[FailAfter(after, exc)], cache=cache)
    for _ in range(3):  # джерело не відпускає: кожен виклик — чесно неповний, без винятку
        result = service.collect(M)
        assert isinstance(result, IngestResult)
        assert result.completeness.status is CompletenessStatus.INCOMPLETE
        assert result.metadata.served_from_cache is False
        assert cache.get_complete(M) is None and cache.get_partial(M) is not None
    # джерело відновилось (новий сервіс зі спільним кешем) — продовження доводить до complete
    healthy, _ = _service(cache=cache)
    final = healthy.collect(M)
    assert final.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(final) == _stable(_fresh(_cfg())[0])
    assert cache.get_partial(M) is None and cache.get_complete(M) == final
