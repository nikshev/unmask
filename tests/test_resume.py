# verifies: FR-001-13
"""Партиційний стан і повторення лише недоотриманого (T-017, data-model «Стан збору і кеш»).

`cache.ResultCache` — два рівні в пам'яті за mint: `complete` (лише `status == complete`) і `partial`
(`CollectionState`, який ніколи не віддається як результат). `collector.resume(state, source, config,
clock)` продовжує збір із партиційного стану: перегортання з `signature_cursor`, гаманці з `missing`,
нерозгорнуті рівні; гаманці з `expanded` не запитуються; `metadata.resumed = true`. Стан іншої
`config_version` відкидається (скидається на місці) і збір іде заново — тоді `resumed = false`, бо
продовжувати не було з чого. Порівняння — з незалежним еталоном `expected.json` і зі свіжим прогоном
(для відкинутого стану — зі свіжим прогоном під НОВИМ конфігом, зокрема коли старий конфіг ширший і
коли в стан підкладено чужі записи). Копія стану в кеші структурна (лише контейнери), тож окремо
перевіряється інваріант: усі значення в стані незмінні. Мережі немає: лише `FixtureRpcSource` і `FakeClock`.
"""

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    CacheInvariantError,
    CompletenessStatus,
    IngestResult,
    MissingHistory,
    MissingReason,
    UnexpandedNode,
    UnexpandedReason,
)
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB = SCENARIOS / "basic", SCENARIOS / "hub"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
A = W["A"]
HUB_MINT = json.loads((HUB / "expected.json").read_text())["mint"]

CONFIG_KEYS = ("first_buyers_n", "funding_depth", "counterparty_threshold",
               "max_signatures_per_wallet", "collect_spl_inbound")
VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
RPC_ERRORS = [RpcUnavailable("node down"), RpcRateLimited(retry_after=1.0), RpcTimeout("slow")]
WALLET_METHODS = ("getSignaturesForAddress", "getTokenAccountsByOwner")


def _cfg(**overrides):
    cfg = load_config(SHIPPED)
    values = dict(EXPECTED["config"])
    rpc = overrides.pop("rpc", None)
    values.update(overrides)
    cfg = dataclasses.replace(cfg, **values)
    return dataclasses.replace(cfg, rpc=rpc) if rpc is not None else cfg


def _page_size(n: int):
    return dataclasses.replace(load_config(SHIPPED).rpc, page_size=n)


def _plain(obj) -> object:
    return json.loads(json.dumps(dataclasses.asdict(obj)))


def _golden_view(result: IngestResult) -> dict:
    """Результат у формі `expected.json` (до `serialize.to_dict`, T-020) — як у test_collector."""
    return {
        "mint": result.metadata.mint,
        "config": {k: getattr(result.metadata, k) for k in CONFIG_KEYS},
        "completeness": {
            "status": result.completeness.status.value,
            "missing": [_plain(m) for m in result.completeness.missing],
            "buyers_complete": result.completeness.buyers.complete,
        },
        "buyers": [_plain(b) for b in result.buyers],
        "transfers": [_plain(t) for t in result.transfers],
        "unexpanded": [_plain(u) for u in result.unexpanded],
    }


GOLDEN = {k: EXPECTED[k] for k in ("mint", "config", "completeness", "buyers", "transfers", "unexpanded")}


def _stable(result: IngestResult) -> tuple:
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, _golden_view(result))


def _fresh(cfg, directory: Path = BASIC, mint: str = M):
    source = FixtureRpcSource(directory)
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, source, collect(state, source, cfg, FakeClock())


def _interrupted(cfg, *, failures=None, budget: float | None = None, directory: Path = BASIC, mint: str = M):
    """Перший, обірваний прогін: збої джерела (`failures`) або бюджет `budget` с при кроці 1 с/виклик."""
    clock = FakeClock(advance_per_call=1.0 if budget is not None else 0.0)
    if budget is not None:
        cfg = dataclasses.replace(cfg, time_budget_seconds=budget)
    source = FixtureRpcSource(directory, failures=failures, clock=clock)
    state = CollectionState(mint=mint, config_version=cfg.version)
    result = collect(state, source, cfg, clock)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    return cfg, state, result


def _incomplete_result_and_state():
    _cfg_used, state, result = _interrupted(_cfg(), failures=[FailFor(A, RpcUnavailable("down"), times=1)])
    return state, result


# --- Кеш: неповний результат ніколи не остаточний -------------------------------------------


def test_partial_never_returned_as_final_result():
    state, incomplete = _incomplete_result_and_state()
    cache = ResultCache()
    cache.put_partial(state)
    # партиційне сховище не дає результату: лише стан для resume
    assert cache.get_complete(M) is None
    assert isinstance(cache.get_partial(M), CollectionState)
    # неповний результат у complete не потрапляє — гучна відмова, кеш не змінено
    with pytest.raises(CacheInvariantError):
        cache.put_complete(incomplete)
    assert cache.get_complete(M) is None
    assert cache.get_partial(M) == state


def test_put_complete_rejects_incomplete_even_without_partial():
    _state, incomplete = _incomplete_result_and_state()
    cache = ResultCache()
    with pytest.raises(CacheInvariantError, match="incomplete"):
        cache.put_complete(incomplete)
    assert cache.get_complete(M) is None


def test_get_on_empty_cache_returns_none():
    cache = ResultCache()
    assert cache.get_complete(M) is None
    assert cache.get_partial(M) is None
    cache.drop_partial(M)  # відсутній запис — не помилка


def test_partial_isolated_from_caller_mutations():
    state, _ = _incomplete_result_and_state()
    snapshot = copy.deepcopy(state)
    cache = ResultCache()
    cache.put_partial(state)
    # мутація збереженого викликачем об'єкта не зачіпає кеш
    state.expanded.add("intruder")
    state.transfers.clear()
    state.missing.clear()
    assert cache.get_partial(M) == snapshot
    # мутація отриманої копії (resume її мутує) теж не зачіпає кеш
    got = cache.get_partial(M)
    got.expanded.clear()
    got.tx_cache.clear()
    assert cache.get_partial(M) == snapshot
    assert cache.get_partial(M) is not cache.get_partial(M)


CONTAINER_FIELDS = ("mint_signatures", "purchases_by_wallet", "frontier_by_depth", "expanded", "transfers",
                    "unexpanded", "missing", "tx_cache")


@pytest.mark.parametrize("name", CONTAINER_FIELDS)
@pytest.mark.parametrize("side", ["stored", "returned"])
def test_every_partial_container_isolated(name, side):
    # кожен контейнер стану — свій у кешу: очищення його в збереженому чи отриманому об'єкті кеш не зачіпає
    _c, state, _r = _interrupted(_cfg(counterparty_threshold=1),  # threshold=1 — щоб був unexpanded
                                 failures=[FailAfter(12, RpcUnavailable("down"))])
    assert getattr(state, name), name  # не порожній — інакше перевірка нічого не доводить
    snapshot = copy.deepcopy(state)
    cache = ResultCache()
    cache.put_partial(state)
    target = state if side == "stored" else cache.get_partial(M)
    getattr(target, name).clear()
    assert cache.get_partial(M) == snapshot


def test_partial_nested_containers_isolated():
    # вкладені контейнери теж свої: рівні frontier і сирий JSON `err` в історії mint
    state, _ = _incomplete_result_and_state()
    err = {"InstructionError": [0, "Custom"]}
    state.mint_signatures.append(("ErrSig" + "1" * 80, 1, None, err))
    snapshot = copy.deepcopy(state)
    cache = ResultCache()
    cache.put_partial(state)
    state.frontier_by_depth[0].clear()
    err["InstructionError"].append("mutated")
    assert cache.get_partial(M) == snapshot
    got = cache.get_partial(M)
    got.frontier_by_depth[1]["intruder"] = "x"
    got.mint_signatures[-1][3]["InstructionError"].clear()
    got.mint_signatures.clear()
    assert cache.get_partial(M) == snapshot


def _assert_immutable(value, path: str) -> None:
    """Значення незмінне: скаляр, enum, кортеж незмінних або frozen-дата-клас із незмінними полями."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, tuple):
        for i, item in enumerate(value):
            _assert_immutable(item, f"{path}[{i}]")
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        assert type(value).__dataclass_params__.frozen, f"{path}: {type(value).__name__} is not frozen"
        for f in dataclasses.fields(value):
            _assert_immutable(getattr(value, f.name), f"{path}.{f.name}")
        hash(value)
        return
    raise AssertionError(f"{path}: mutable value of type {type(value).__name__}")


def _states_for_invariant(tmp_path: Path):
    cfg = _cfg()
    yield _fresh(cfg)[0]
    yield _interrupted(cfg, failures=[FailAfter(20, RpcUnavailable("down"))])[1]
    yield _interrupted(cfg, budget=39.0)[1]
    yield _fresh(_cfg(counterparty_threshold=1))[0]  # з unexpanded
    hub = load_config(SHIPPED)
    yield _fresh(hub, HUB, HUB_MINT)[0]
    # історія mint із невдалою транзакцією: `err` — сирий JSON від RPC
    data = json.loads((BASIC / "rpc.json").read_text())
    data["getSignaturesForAddress"][M][0]["err"] = {"InstructionError": [0, "Custom"]}
    (tmp_path / "basic").mkdir()
    (tmp_path / "basic" / "rpc.json").write_text(json.dumps(data))
    state = CollectionState(mint=M, config_version=cfg.version)
    collect(state, FixtureRpcSource(tmp_path / "basic"), cfg, FakeClock())
    yield state


def test_collection_state_values_are_immutable(tmp_path):
    # інваріант структурної копії в ResultCache: значення в контейнерах стану незмінні, тож копіювати
    # треба лише контейнери; єдиний явний виняток — сирий JSON `err` у mint_signatures (копіюється глибоко)
    seen = {name: 0 for name in ("tx_cache", "transfers", "purchases_by_wallet", "missing", "unexpanded",
                                 "buyers", "frontier_by_depth", "expanded", "mint_signatures")}
    errors = 0
    for state in _states_for_invariant(tmp_path):
        for f in ("mint", "config_version", "signature_cursor", "mint_history_exhausted", "buyers",
                  "rpc_calls", "transactions_scanned"):
            _assert_immutable(getattr(state, f), f)
        for name in ("tx_cache", "transfers", "purchases_by_wallet", "missing"):
            for key, value in getattr(state, name).items():
                _assert_immutable(key, f"{name} key")
                _assert_immutable(value, f"{name}[{key}]")
                seen[name] += 1
        for i, node in enumerate(state.unexpanded):
            _assert_immutable(node, f"unexpanded[{i}]")
            seen["unexpanded"] += 1
        for depth, level in state.frontier_by_depth.items():
            _assert_immutable(depth, "frontier_by_depth key")
            for wallet, cutoff in level.items():
                _assert_immutable((wallet, cutoff), f"frontier_by_depth[{depth}]")
                seen["frontier_by_depth"] += 1
        for wallet in state.expanded:
            _assert_immutable(wallet, "expanded")
            seen["expanded"] += 1
        for entry in state.mint_signatures:
            assert isinstance(entry, tuple) and len(entry) == 4
            _assert_immutable(entry[:3], "mint_signatures")  # entry[3] (err) — явний виняток
            seen["mint_signatures"] += 1
            errors += entry[3] is not None
        seen["buyers"] += len(state.buyers)
    assert all(seen.values()), seen  # перевірка не порожня для жодного поля
    assert errors  # виняток справді трапляється в даних — тому його й копіюють глибоко


def test_cache_copy_table_covers_every_state_field():
    from unmask.ingest.cache import _FIELD_COPY
    assert set(_FIELD_COPY) == {f.name for f in dataclasses.fields(CollectionState)}


def test_complete_result_is_immutable_snapshot():
    _state, _source, result = _fresh(_cfg())
    cache = ResultCache()
    cache.put_complete(result)
    got = cache.get_complete(M)
    assert got == result
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.metadata.served_from_cache = True  # type: ignore[misc]
    assert isinstance(got.transfers, tuple) and isinstance(got.buyers, tuple)


def test_drop_partial_removes_only_that_mint():
    basic_state, _ = _incomplete_result_and_state()
    hub_state = CollectionState(mint=HUB_MINT, config_version=1)
    cache = ResultCache()
    cache.put_partial(basic_state)
    cache.put_partial(hub_state)
    cache.drop_partial(M)
    assert cache.get_partial(M) is None
    assert cache.get_partial(HUB_MINT) == hub_state


def test_two_mints_are_not_mixed():
    basic_state, _ = _incomplete_result_and_state()
    hub_state = CollectionState(mint=HUB_MINT, config_version=1, signature_cursor="x")
    cache = ResultCache()
    cache.put_partial(basic_state)
    cache.put_partial(hub_state)
    assert cache.get_partial(M) == basic_state
    assert cache.get_partial(HUB_MINT) == hub_state
    # повний результат одного mint не чистить партиційний стан іншого й не видається за нього
    _s, _src, complete = _fresh(_cfg())
    cache.put_complete(complete)
    assert cache.get_complete(HUB_MINT) is None
    assert cache.get_partial(HUB_MINT) == hub_state
    assert cache.get_partial(M) is None


def test_put_partial_replaces_previous_state_of_same_mint():
    cache = ResultCache()
    cache.put_partial(CollectionState(mint=M, config_version=1, signature_cursor="old"))
    cache.put_partial(CollectionState(mint=M, config_version=1, signature_cursor="new"))
    assert cache.get_partial(M).signature_cursor == "new"


# --- resume: лише недоотримане ----------------------------------------------------------------


@pytest.mark.parametrize("cut", ["rpc_failure", "budget"])
def test_resume_fetches_only_missing_wallets(cut):
    # перший прогін: A (рівень 1) недоотримано — збій джерела на першому зверненні або бюджет 39 с
    if cut == "rpc_failure":
        cfg, state, first = _interrupted(_cfg(), failures=[FailFor(A, RpcUnavailable("down"), times=1)])
    else:
        cfg, state, first = _interrupted(_cfg(), budget=39.0)
    assert {m.wallet for m in first.completeness.missing} == {A}
    expanded_before = set(state.expanded)
    assert expanded_before and A not in expanded_before

    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, FakeClock())

    queried = {p.get("address", p.get("owner")) for m, p in source.calls if m in WALLET_METHODS}
    assert not queried & expanded_before  # жодного звернення до розгорнутих гаманців
    assert M not in queried  # історія mint уже вичерпана — не перегортається
    assert A in queried  # недоотриманий гаманець добирається
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.metadata.resumed is True
    assert result.metadata.served_from_cache is False
    assert result.metadata.rpc_calls == len(source.calls)
    assert _golden_view(result) == GOLDEN


def test_resume_continues_buyer_enumeration_from_cursor():
    # page_size=2: історія mint (7 підписів) — сторінки 2+2+2+1+порожня; третя сторінка падає
    cfg = _cfg(rpc=_page_size(2))
    cfg, state, first = _interrupted(cfg, failures=[FailAfter(3, RpcRateLimited(retry_after=1.0))])
    assert first.completeness.buyers.complete is False and first.buyers == ()
    assert state.mint_history_exhausted is False
    cursor = state.signature_cursor
    assert cursor is not None and len(state.mint_signatures) == 4

    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, FakeClock())

    paging = [p for m, p in source.calls if m == "getSignaturesForAddress" and p["address"] == M]
    assert paging[0]["before"] == cursor  # продовження з курсора, а не з початку історії
    assert all(p["before"] is not None for p in paging)
    assert len(paging) == 3  # сторінки 3, 4 і порожня
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.metadata.resumed is True
    _s, _src, fresh = _fresh(cfg)
    assert _stable(result) == _stable(fresh)
    assert _golden_view(result) == GOLDEN


def test_resume_after_failure_cleared_yields_complete_and_moves_to_complete_cache():
    cache = ResultCache()
    cfg, state, first = _interrupted(_cfg(), failures=[FailFor(A, RpcTimeout("slow"), times=1)])
    cache.put_partial(state)
    assert cache.get_complete(M) is None

    partial = cache.get_partial(M)
    result = resume(partial, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert result.completeness.status is CompletenessStatus.COMPLETE
    cache.put_complete(result)
    # після повного успіху partial чиститься, результат — у complete
    assert cache.get_partial(M) is None
    assert cache.get_complete(M) == result
    assert _golden_view(cache.get_complete(M)) == GOLDEN


def test_resume_that_is_still_incomplete_stays_partial():
    # другий прогін теж падає на A: результат неповний, у complete його не кладуть, стан — оновлений partial
    cache = ResultCache()
    failures = lambda: [FailFor(A, RpcUnavailable("down"), times=10)]  # noqa: E731
    cfg, state, _first = _interrupted(_cfg(), failures=failures())
    cache.put_partial(state)
    partial = cache.get_partial(M)
    again = resume(partial, FixtureRpcSource(BASIC, failures=failures()), cfg, FakeClock())
    assert again.completeness.status is CompletenessStatus.INCOMPLETE
    assert again.metadata.resumed is True
    with pytest.raises(CacheInvariantError):
        cache.put_complete(again)
    cache.put_partial(partial)
    assert cache.get_complete(M) is None
    assert {m.wallet for m in again.completeness.missing} == {A}


GHOST = "Ghost1111111111111111111111111111111111111"  # гаманця немає у фікстурі
GHOST_SIG = "GhostSig" + "1" * 80


def _plant_foreign_records(state: CollectionState) -> None:
    """Підкласти в застарілий стан записи для гаманця поза фікстурою й поза охопленням будь-якого конфігу:
    протікання будь-якого поля в новий збір стає видимим у результаті, журналі або стані."""
    transfer = next(iter(state.transfers.values()))
    parsed = next(iter(state.tx_cache.values()))
    buyer, purchase = state.buyers[0], next(iter(state.purchases_by_wallet.values()))
    state.signature_cursor = GHOST_SIG
    state.mint_signatures.append((GHOST_SIG, 1, None, None))
    state.purchases_by_wallet[GHOST] = dataclasses.replace(purchase, wallet=GHOST, slot=0, signature=GHOST_SIG)
    state.buyers = (dataclasses.replace(buyer, wallet=GHOST, rank=len(state.buyers) + 1),) + state.buyers
    state.frontier_by_depth.setdefault(1, {})[GHOST] = GHOST_SIG
    state.frontier_by_depth[9] = {GHOST: GHOST_SIG}
    state.expanded.add(GHOST)
    state.transfers[(GHOST_SIG, "0")] = dataclasses.replace(
        transfer, signature=GHOST_SIG, instruction_path="0", receiver=GHOST)
    state.unexpanded.append(UnexpandedNode(wallet=GHOST, depth=1, reason=UnexpandedReason.HIGH_DEGREE,
                                           counterparties_seen=999, signatures_seen=999,
                                           signatures_truncated=True))
    state.missing[(GHOST, MissingReason.UNAVAILABLE)] = MissingHistory(
        wallet=GHOST, depth=1, reason=MissingReason.UNAVAILABLE, detail="planted")
    state.tx_cache[GHOST_SIG] = dataclasses.replace(parsed, signature=GHOST_SIG)
    state.rpc_calls += 1000
    state.transactions_scanned += 1000


NARROW = {"first_buyers_n": 2, "funding_depth": 1}
WIDE = {}  # значення еталона: n=5, depth=3


@pytest.mark.parametrize(("old_values", "new_values", "fail_after", "plant"), [
    pytest.param(NARROW, WIDE, 12, False, id="old-narrower-than-new"),
    pytest.param(WIDE, NARROW, 20, False, id="old-wider-than-new"),
    pytest.param(WIDE, {"first_buyers_n": 1, "funding_depth": 1}, 30, False, id="old-wider-depth1-n1"),
    pytest.param(WIDE, NARROW, 20, True, id="old-wider-with-foreign-records"),
    pytest.param(NARROW, WIDE, 12, True, id="old-narrower-with-foreign-records"),
])
@pytest.mark.parametrize("stale_offset", [1, 5])
def test_partial_with_stale_config_version_is_discarded_and_recollected(
        old_values, new_values, fail_after, plant, stale_offset):
    cfg = _cfg(**new_values)
    # партиційний стан іншої версії конфігу (і інших значень) — частково зібраний
    old_cfg = dataclasses.replace(_cfg(**old_values), version=cfg.version + stale_offset)
    _old, state, _first = _interrupted(old_cfg, failures=[FailAfter(fail_after, RpcUnavailable("down"))])
    assert state.config_version != cfg.version and state.expanded and state.transfers
    if plant:
        _plant_foreign_records(state)

    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, FakeClock())

    # еталон — свіжий прогін під НОВИМ конфігом
    fresh_state, fresh_source, fresh = _fresh(cfg)
    assert source.calls == fresh_source.calls  # збір заново: той самий журнал, що й у свіжого прогону
    assert result.metadata.transactions_scanned == fresh.metadata.transactions_scanned
    assert result.metadata.rpc_calls == fresh.metadata.rpc_calls
    assert _stable(result) == _stable(fresh)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    # продовжувати не було з чого: стан відкинуто, тож resumed=false
    assert result.metadata.resumed is False
    assert result.metadata.config_version == cfg.version
    # стан скинуто на місці (викликач кладе в partial той самий об'єкт) і він == стану свіжого прогону
    assert state == fresh_state


class _StateSpyClock(FakeClock):
    """Знімок стану при ПЕРШОМУ зверненні до годинника — `collect` робить його першим, ще до будь-якого
    кроку збору, тож знімок показує рівно те, з чим новий збір стартує після відкидання."""

    def __init__(self, state: CollectionState) -> None:
        super().__init__()
        self._state, self.first_seen = state, None

    def monotonic(self) -> float:
        if self.first_seen is None:
            self.first_seen = copy.deepcopy(self._state)
        return super().monotonic()


@pytest.mark.parametrize("plant", [False, True], ids=["as-collected", "with-foreign-records"])
def test_stale_state_is_fully_reset_before_collection_starts(plant):
    # пряма перевірка відкидання: новий збір стартує з порожнього стану поточної версії — жодне поле
    # застарілого стану не просочується, навіть те, яке збір потім перезаписав би сам
    cfg = _cfg(**NARROW)
    old_cfg = dataclasses.replace(_cfg(), version=cfg.version + 1)
    _old, state, _first = _interrupted(old_cfg, failures=[FailAfter(20, RpcUnavailable("down"))])
    if plant:
        _plant_foreign_records(state)
    clock = _StateSpyClock(state)
    resume(state, FixtureRpcSource(BASIC), cfg, clock)
    assert clock.first_seen == CollectionState(mint=M, config_version=cfg.version)


def test_stale_state_discard_matches_golden_for_shipped_values():
    cfg = _cfg()
    old_cfg = dataclasses.replace(cfg, version=cfg.version + 1, **NARROW)
    _old, state, _first = _interrupted(old_cfg, failures=[FailAfter(12, RpcUnavailable("down"))])
    _plant_foreign_records(state)
    assert _golden_view(resume(state, FixtureRpcSource(BASIC), cfg, FakeClock())) == GOLDEN


def test_direct_collect_still_refuses_stale_state_loudly():
    # відкидання — лише в resume; прямий collect зі станом іншої версії — дефект викликача
    cfg = _cfg()
    state = CollectionState(mint=M, config_version=cfg.version + 1)
    source = FixtureRpcSource(BASIC)
    with pytest.raises(ValueError, match="config_version"):
        collect(state, source, cfg, FakeClock())
    assert source.calls == []


INTERRUPTIONS = [
    pytest.param({"failures": [FailAfter(1, exc)]}, id=f"paging-{type(exc).__name__}") for exc in RPC_ERRORS
] + [
    pytest.param({"failures": [FailAfter(k, RpcUnavailable("down"))]}, id=f"after-{k}-calls") for k in (5, 12, 20, 30)
] + [
    pytest.param({"failures": [FailFor(A, exc, times=1)]}, id=f"wallet-A-{type(exc).__name__}") for exc in RPC_ERRORS
] + [
    pytest.param({"budget": b}, id=f"budget-{int(b)}s") for b in (3.0, 10.0, 39.0, 41.0, 45.0)
]


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_resume_result_equals_uninterrupted_collection(interruption):
    cfg, state, first = _interrupted(_cfg(), **interruption)
    result = resume(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    _fs, _fsrc, fresh = _fresh(cfg)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.metadata.resumed is True
    assert result.metadata.transactions_scanned == fresh.metadata.transactions_scanned
    assert _stable(result) == _stable(fresh)
    assert _golden_view(result) == GOLDEN  # той самий expected.json


def test_resume_on_complete_state_makes_no_source_calls():
    cfg = _cfg()
    state, _src, fresh = _fresh(cfg)
    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, FakeClock())
    assert source.calls == []
    assert result.metadata.rpc_calls == 0 and result.metadata.resumed is True
    assert _stable(result) == _stable(fresh)


def test_resume_reports_missing_reason_when_source_still_failing_for_other_reason():
    cfg, state, first = _interrupted(_cfg(), budget=39.0)
    assert {m.reason for m in first.completeness.missing} == {MissingReason.BUDGET_EXHAUSTED}
    result = resume(state, FixtureRpcSource(BASIC, failures=[FailFor(A, RpcRateLimited(1.0), times=1)]),
                    cfg, FakeClock())
    # причини — лише актуальні: бюджетної вже немає, є ліміт
    assert {(m.wallet, m.reason) for m in result.completeness.missing} == {(A, MissingReason.RATE_LIMITED)}
