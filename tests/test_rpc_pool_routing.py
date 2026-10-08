# verifies: FR-007-06, FR-007-07
"""Вік і підтвердження «не знайдено» для транзакцій у пулі (T-135, critical; contracts/pooled-rpc-source.md).

Помилка тут тиха: пул, що віддав неархівному вузлу транзакцію за межею його історії й прийняв «None» як
«нічого немає», не ламає збірку — він змінює висновок (принцип V: хибне «чисто» гірше за хибне «підозріло»).
Тому тести перевіряють не лише щасливий шлях, а й межі:

- вік береться з `blockTime` відповідей `getSignaturesForAddress`, що пройшли через пул (кеш FIFO
  ≤ `age_cache_entries`, лише справжній `int`); межа придатності — `вік <= retention_seconds` (включно);
- транзакція відомого віку за межею історії неархівного провайдера йому НЕ призначається (журнал дубля);
- невідомий вік: усі придатні, але `None` від неархівного не остаточне — позиція йде в раунд «лише архівні»;
  `None` остаточне лише від архівного; без придатного архівного — `RpcUnavailable("history beyond provider
  retention")` (до звернень, якщо це видно наперед; після раунду, якщо `None` прийшов від неархівного);
- плани часток НЕ суцільні (групи позицій із різними множинами придатних провайдерів), тож відповідь
  збирається строго за ПОЗИЦІЄЮ підпису: тест із чергуванням старих і свіжих позицій і підтверджувальний раунд
  із позиціями вперемішку (T-134-мутація «збирання в порядку провайдерів» тут розрізнювана);
- на рівні сервісу: пул [неархівний + архівний] дає побайтово той самий результат, що й одне джерело; пул
  лише з неархівного дає `incomplete` (причина `unavailable`), а не «чисто».

Без мережі: `ScriptedProvider` поверх власних `FixtureRpcSource`, `FakeClock`, `SequentialRunner`.
Правила для `getSignaturesForAddress` (архівним — підписи; коротка сторінка неархівного) — T-136; перебирання
при відмові — T-140: тут відмова частки піднімається як є.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
from pathlib import Path

import pytest
import yaml

from rpc_pool_doubles import ScriptedFailure, ScriptedProvider, SequentialRunner
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, ThreadRunner
from unmask.ingest.rpc.protocol import RpcBudgetTimeout, RpcTimeout, RpcUnavailable
from unmask.ingest.serialize import to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SHIPPED = ROOT / "config" / "ingest.yaml"
SHIPPED_POOL = ROOT / "config" / "rpc_pool.yaml"

RETENTION_TEXT = "history beyond provider retention"
ADDRESS = "ADDR"
NOW = 1_700_000_000  # «зараз» пулу (FakeClock.wall)
RETENTION = 1000  # глибина історії неархівного провайдера в тестах, секунди
FRESH = NOW - 10  # blockTime свіжої транзакції (вік 10 с)
OLD = NOW - RETENTION - 5000  # blockTime давньої транзакції (вік 6000 с > RETENTION)


# --------------------------------------------------------------------------- допоміжне


def _deadline(seconds: float = 600.0) -> Deadline:
    return Deadline(FakeClock(), seconds)


def _sigs(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(n)]


def _write_scenario(tmp_path: Path, ages: dict[str, int | None], *, name: str = "routing") -> Path:
    """Сценарій: історія `ADDRESS` (порядок словника) і транзакції з тим самим blockTime; відповідь несе підпис."""
    history = [
        {"signature": sig, "slot": 100 + i, "err": None, "memo": None, "blockTime": bt, "confirmationStatus": "finalized"}
        for i, (sig, bt) in enumerate(ages.items())
    ]
    transactions = {
        sig: {"slot": 100 + i, "blockTime": bt, "signature": sig} for i, (sig, bt) in enumerate(ages.items())
    }
    directory = tmp_path / name
    directory.mkdir()
    rpc = {"getSignaturesForAddress": {ADDRESS: history}, "getTransaction": transactions}
    (directory / "rpc.json").write_text(json.dumps(rpc), encoding="utf-8")
    return directory


def _non(name: str, weight: float, scenario: Path, **kw) -> tuple[Provider, ScriptedProvider]:
    """Неархівний вузол: глибина RETENTION, горизонт = NOW - RETENTION (транзакції за ним — None)."""
    double = ScriptedProvider(FixtureRpcSource(scenario), horizon_block_time=NOW - RETENTION, **kw.pop("scripted", {}))
    skip = kw.pop("skip", frozenset({"getSignaturesForAddress"}))  # підписи в тестах відають архівні (T-136)
    provider = Provider(name, double, weight, False, float(kw.pop("retention", RETENTION)), frozenset(skip))
    return provider, double


def _arch(name: str, weight: float, scenario: Path, **kw) -> tuple[Provider, ScriptedProvider]:
    double = ScriptedProvider(FixtureRpcSource(scenario), **kw.pop("scripted", {}))
    provider = Provider(name, double, weight, True, None, frozenset(kw.pop("skip", ())))
    return provider, double


def _pool(pairs, *, runner=None, clock=None, **kw) -> tuple[PooledRpcSource, list[ScriptedProvider]]:
    values = dict(clock=clock or FakeClock(wall_start=NOW), rate_limit_cooldown_seconds=30.0, age_cache_entries=1000)
    values.update(kw)
    pool = PooledRpcSource([p for p, _ in pairs], runner=runner or SequentialRunner(), **values)
    return pool, [d for _, d in pairs]


def _seed(pool: PooledRpcSource) -> None:
    """Пропустити історію через пул: вік підписів стає відомим."""
    pool.get_signatures_for_address(ADDRESS, before=None, until=None, limit=1000, deadline=_deadline())


def _tx_calls(double: ScriptedProvider) -> list[tuple[str, ...]]:
    return [keys for method, keys in double.log if method == "getTransaction"]


def _signatures_of(raws) -> list[str | None]:
    return [None if raw is None else raw["signature"] for raw in raws]


# --------------------------------------------------------------------------- US2-1: давня — не неархівному


def test_known_old_transaction_never_sent_to_nonarchival(tmp_path):
    sigs = _sigs("old", 12)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    (non, non_d), (arch, arch_d) = _non("rpcfast", 12, scenario), _arch("helius", 4.5, scenario)
    pool, _ = _pool([(non, non_d), (arch, arch_d)])
    _seed(pool)

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _signatures_of(got) == sigs  # усі дані на місці: нічого не втрачено на «неархівному»
    assert _tx_calls(non_d) == []  # журнал дубля: неархівний НЕ отримав жодного getTransaction
    assert _tx_calls(arch_d) == [tuple(sigs)]  # весь пакет — архівному, одним зверненням
    assert pool._not_found_escalations == [0, 0]  # підтверджувати нічого: питати неархівного взагалі не було чого


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_known_old_never_reaches_nonarchival_for_any_completion_order(tmp_path, order):
    sigs = _sigs("old", 7)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, arch_d) = _pool(
        [_non("rpcfast", 12, scenario), _arch("helius", 4.5, scenario)], runner=SequentialRunner(order)
    )
    _seed(pool)
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert _tx_calls(non_d) == [] and _tx_calls(arch_d) == [tuple(sigs)]


def test_known_old_without_any_archival_getter_raises_before_any_call(tmp_path):
    # Архівний провайдер є, але getTransaction не обслуговує (skip_methods): придатного для давньої немає.
    sigs = _sigs("old", 6)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, pages_d) = _pool(
        [_non("rpcfast", 12, scenario), _arch("pages", 1, scenario, skip={"getTransaction"})]
    )
    _seed(pool)
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert str(info.value) == RETENTION_TEXT
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert _tx_calls(non_d) == [] and _tx_calls(pages_d) == []  # до жодного звернення


def test_one_unservable_position_blocks_the_whole_round_before_any_call(tmp_path):
    # Одна давня позиція без придатного провайдера: виклик «все або нічого» падає ДО звернень — навіть
    # свіжі позиції, які неархівний міг би обслужити, не запитуються (раунд не стартує частково).
    ages = {"fresh00": FRESH, "fresh01": FRESH, "old00": OLD}
    scenario = _write_scenario(tmp_path, ages)
    pool, (non_d, pages_d) = _pool(
        [_non("rpcfast", 12, scenario), _arch("pages", 1, scenario, skip={"getTransaction"})]
    )
    _seed(pool)
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(list(ages), deadline=_deadline())
    assert str(info.value) == RETENTION_TEXT
    assert _tx_calls(non_d) == [] and _tx_calls(pages_d) == []


# --------------------------------------------------------------------------- US2-2: невідомий вік


def test_unknown_age_none_from_nonarchival_confirmed_by_archival(tmp_path):
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, arch_d) = _pool([_non("rpcfast", 12, scenario), _arch("helius", 4.5, scenario)])
    # історію через пул НЕ пропускали: вік невідомий, придатні всі

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _signatures_of(got) == sigs  # «None» неархівного не прийнято: дані дав архівний
    assert _tx_calls(non_d) == [tuple(sigs[:7])]  # перший раунд: 7/3 за вагами 12 : 4,5
    assert _tx_calls(arch_d) == [tuple(sigs[7:]), tuple(sigs[:7])]  # власна частка, потім підтвердження
    assert pool._not_found_escalations == [7, 0]  # лічильник: 7 позицій передано архівному


def test_none_is_final_only_from_archival(tmp_path):
    sigs = ["missing00", "old00", "missing01"]
    scenario = _write_scenario(tmp_path, {"old00": OLD})
    pool, (non_d, arch_d) = _pool([_non("rpcfast", 12, scenario), _arch("helius", 1, scenario)])
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _signatures_of(got) == [None, "old00", None]  # справді відсутні — остаточно None від архівного
    # неархівний отримав усі три (вага 12 : 1, три позиції → всі три йому), None → підтвердження у архівного
    assert _tx_calls(non_d) == [tuple(sigs)]
    assert _tx_calls(arch_d) == [tuple(sigs)]
    assert pool._not_found_escalations == [3, 0]


def test_none_from_archival_in_first_round_is_final_without_another_round(tmp_path):
    scenario = _write_scenario(tmp_path, {"old00": OLD})
    pool, (arch_d,) = _pool([_arch("helius", 1, scenario)])
    assert pool.get_transactions(["gone00", "gone01", "old00"], deadline=_deadline()) == [
        None, None, {"slot": 100, "blockTime": OLD, "signature": "old00"}
    ]
    assert len(_tx_calls(arch_d)) == 1  # жодного другого раунду


def test_confirmation_goes_only_to_archival_providers_never_back_to_nonarchival(tmp_path):
    sigs = _sigs("old", 9)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non1_d, non2_d, arch_d) = _pool(
        [_non("n1", 12, scenario), _non("n2", 12, scenario), _arch("a", 1, scenario)]
    )
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert len(_tx_calls(non1_d)) == 1 and len(_tx_calls(non2_d)) == 1  # кожен неархівний питаний рівно раз
    confirm = _tx_calls(arch_d)[-1]
    asked_non = set(_tx_calls(non1_d)[0]) | set(_tx_calls(non2_d)[0])
    assert set(confirm) == asked_non  # підтверджується рівно те, на що неархівні відповіли None


def test_confirmation_is_split_between_archival_providers_by_weight_in_position_order(tmp_path):
    sigs = _sigs("old", 12)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, a1_d, a2_d) = _pool(
        [_non("n", 12, scenario), _arch("a1", 12, scenario), _arch("a2", 4.5, scenario)]
    )
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    # раунд 1: apportion(12, [12, 12, 4.5]) = [5, 5, 2] → n: 0..4, a1: 5..9, a2: 10..11
    assert _tx_calls(non_d) == [tuple(sigs[0:5])]
    # раунд 2 (підтвердження 0..4): apportion(5, [12, 4.5]) = [4, 1] → a1: 0..3, a2: 4
    assert _tx_calls(a1_d) == [tuple(sigs[5:10]), tuple(sigs[0:4])]
    assert _tx_calls(a2_d) == [tuple(sigs[10:12]), tuple(sigs[4:5])]


# --------------------------------------------------------------------------- US2-3: без архівного


def test_no_archival_unknown_age_none_raises_unavailable(tmp_path):
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (n1_d, n2_d) = _pool([_non("n1", 12, scenario, skip=()), _non("n2", 4.5, scenario)])
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert type(info.value) is RpcUnavailable and str(info.value) == RETENTION_TEXT
    assert info.value.detail == RETENTION_TEXT
    assert info.value.__cause__ is None and info.value.__context__ is None  # raise — поза except
    assert len(_tx_calls(n1_d)) == 1 and len(_tx_calls(n2_d)) == 1  # один раунд; підтвердження немає кому віддати


def test_no_archival_but_data_everywhere_needs_no_confirmation(tmp_path):
    # Неархівний, що ВІДДАВ дані, достатній: «лише None потребує підтвердження» — виклик успішний без архівного.
    sigs = _sigs("fresh", 8)
    scenario = _write_scenario(tmp_path, {s: FRESH for s in sigs})
    pool, (non_d,) = _pool([_non("n", 12, scenario, skip=())])
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert _tx_calls(non_d) == [tuple(sigs)]


def test_archival_that_skips_get_transaction_cannot_confirm(tmp_path):
    sigs = _sigs("old", 6)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, pages_d) = _pool(
        [_non("n", 12, scenario), _arch("pages", 4.5, scenario, skip={"getTransaction"})]
    )
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert str(info.value) == RETENTION_TEXT
    assert _tx_calls(pages_d) == []  # skip_methods уважається і для підтвердження
    assert len(_tx_calls(non_d)) == 1


# --------------------------------------------------------------------------- US2-4: свіжа — і неархівному


def test_fresh_transaction_served_by_nonarchival(tmp_path):
    sigs = _sigs("fresh", 10)
    scenario = _write_scenario(tmp_path, {s: FRESH for s in sigs})
    pool, (non_d, arch_d) = _pool([_non("rpcfast", 12, scenario), _arch("helius", 4.5, scenario)])
    _seed(pool)

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _signatures_of(got) == sigs
    assert _tx_calls(non_d) == [tuple(sigs[:7])]  # неархівний отримав НЕПОРОЖНЮ частку за вагою — він у роботі
    assert _tx_calls(arch_d) == [tuple(sigs[7:])]
    assert pool._not_found_escalations == [0, 0]  # дані — остаточні: підтверджувального раунду немає


def test_fresh_with_unknown_age_is_also_served_by_nonarchival_without_escalation(tmp_path):
    sigs = _sigs("fresh", 10)
    scenario = _write_scenario(tmp_path, {s: FRESH for s in sigs})
    pool, (non_d, arch_d) = _pool([_non("rpcfast", 12, scenario), _arch("helius", 4.5, scenario)])
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs  # вік невідомий
    assert _tx_calls(non_d) == [tuple(sigs[:7])] and _tx_calls(arch_d) == [tuple(sigs[7:])]
    assert pool._not_found_escalations == [0, 0]


# --------------------------------------------------------------------------- розбіжність: дані над «нічого»


def test_data_answer_preferred_over_none(tmp_path):
    sigs = _sigs("tx", 10)
    full = _write_scenario(tmp_path, {s: FRESH for s in sigs}, name="full")
    empty = _write_scenario(tmp_path, {}, name="empty")  # цей вузол транзакцій не знає
    # (a) неархівний не знає (None), архівний знає → результат — дані архівного
    pool, (non_d, arch_d) = _pool([_non("n", 12, empty), _arch("a", 4.5, full)])
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert _tx_calls(arch_d)[-1] == tuple(sigs[:7])
    # (b) навпаки: неархівний знає, архівний — ні → дані неархівного не перекриваються «None» архівного
    pool, (non_d, arch_d) = _pool([_non("n", 12, full), _arch("a", 4.5, empty)])
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _signatures_of(got) == sigs[:7] + [None] * 3  # 7 позицій — дані неархівного; решта — власна частка архівного
    assert _tx_calls(arch_d) == [tuple(sigs[7:])]  # архівного про позиції, де вже є дані, не питали


# --------------------------------------------------------------------------- межі віку


def _single_sig_owner(tmp_path, block_time, *, clock=None, seed_clock=None):
    """Хто отримав єдину транзакцію віку `NOW - block_time`: ('non'|'arch'|None)."""
    scenario = _write_scenario(tmp_path, {"only": block_time})
    pool, (non_d, arch_d) = _pool([_non("n", 12, scenario), _arch("a", 1, scenario)], clock=clock)
    _seed(pool)
    pool.get_transactions(["only"], deadline=_deadline())
    return non_d, arch_d, pool


@pytest.mark.parametrize(
    "age, nonarchival_eligible",
    [(0, True), (RETENTION - 1, True), (RETENTION, True), (RETENTION + 1, False), (RETENTION * 50, False), (-50, True)],
    ids=["now", "just_inside", "exactly_at_retention", "one_second_over", "far_over", "future_block_time"],
)
def test_retention_boundary_is_inclusive(tmp_path, age, nonarchival_eligible):
    non_d, arch_d, _ = _single_sig_owner(tmp_path, NOW - age)
    if nonarchival_eligible:
        # найважчий з придатних бере єдину позицію; транзакція на межі (blockTime == горизонт) видима
        assert _tx_calls(non_d)[0] == ("only",)
    else:
        assert _tx_calls(non_d) == []
        assert _tx_calls(arch_d) == [("only",)]


def test_age_is_measured_by_the_pool_clock_and_grows_with_it(tmp_path):
    clock = FakeClock(wall_start=NOW)
    scenario = _write_scenario(tmp_path, {"only": NOW - (RETENTION - 1)})
    pool, (non_d, arch_d) = _pool([_non("n", 12, scenario), _arch("a", 1, scenario)], clock=clock)
    _seed(pool)
    pool.get_transactions(["only"], deadline=_deadline())  # вік RETENTION - 1 → неархівний
    assert len(_tx_calls(non_d)) == 1
    clock.advance(1)  # вік == RETENTION: ще придатний (горизонт дубля — фіксований, тож дані віддає)
    pool.get_transactions(["only"], deadline=_deadline())
    assert len(_tx_calls(non_d)) == 2 and _tx_calls(arch_d) == []
    clock.advance(1)  # вік == RETENTION + 1: непридатний
    pool.get_transactions(["only"], deadline=_deadline())
    assert len(_tx_calls(non_d)) == 2 and _tx_calls(arch_d) == [("only",)]


def test_boundary_uses_each_providers_own_retention(tmp_path):
    scenario = _write_scenario(tmp_path, {"mid": NOW - 2000})
    short, short_d = _non("short", 12, scenario, retention=1500, scripted={})
    long_, long_d = _non("long", 12, scenario, retention=2500, scripted={})
    arch, arch_d = _arch("arch", 1, scenario)
    short_d._horizon = NOW - 1500
    long_d._horizon = NOW - 2500
    pool, _ = _pool([(short, short_d), (long_, long_d), (arch, arch_d)])
    _seed(pool)
    pool.get_transactions(["mid"], deadline=_deadline())
    assert _tx_calls(short_d) == []  # 2000 > 1500
    assert _tx_calls(long_d) == [("mid",)]  # 2000 <= 2500
    assert _tx_calls(arch_d) == []


def test_shipped_rpc_pool_yaml_retention_golden_boundary(tmp_path):
    # Golden-файл: межа береться з зафіксованої v1 `config/rpc_pool.yaml` (константа впливає на маршрутизацію).
    raw = yaml.safe_load(SHIPPED_POOL.read_text(encoding="utf-8"))
    assert raw["version"] == 1  # зафіксована версія; зміна конфігу = новий запис журналу й свідомий перегляд цього тесту
    rpcfast = next(p for p in raw["providers"] if p["name"] == "rpcfast")
    assert rpcfast["archival"] is False and rpcfast["retention_seconds"] == 72000
    retention = float(rpcfast["retention_seconds"])
    for age, expected_non in [(72000, True), (72001, False), (71999, True)]:
        scenario = _write_scenario(tmp_path, {"only": NOW - age}, name=f"golden{age}")
        non_d = ScriptedProvider(FixtureRpcSource(scenario))  # справжня глибина тут не емулюється: лише маршрут
        arch_d = ScriptedProvider(FixtureRpcSource(scenario))
        providers = [
            Provider("rpcfast", non_d, 12.0, False, retention, frozenset({"getTokenAccountsByOwner", "getSignaturesForAddress"})),
            Provider("helius_1", arch_d, 4.5, True, None, frozenset()),
        ]
        pool = PooledRpcSource(
            providers, clock=FakeClock(wall_start=NOW), rate_limit_cooldown_seconds=float(raw["rate_limit_cooldown_seconds"]),
            age_cache_entries=raw["age_cache_entries"], runner=SequentialRunner(),
        )
        _seed(pool)
        pool.get_transactions(["only"], deadline=_deadline())
        assert bool(_tx_calls(non_d)) is expected_non, age
        assert bool(_tx_calls(arch_d)) is (not expected_non), age


# --------------------------------------------------------------------------- кеш віку


def _stub_pages(*pages):
    """Архівне «джерело», що віддає задані сторінки підписів (для перевірки кешу без фікстури)."""

    class Pages:
        name = "pages"

        def __init__(self):
            self.queue = list(pages)

        def get_signatures_for_address(self, address, *, before, until, limit, deadline):
            return self.queue.pop(0)

        def get_transactions(self, signatures, *, deadline):
            return [{"signature": s} for s in signatures]

    return Pages()


def _entry(sig, block_time):
    return {"signature": sig, "slot": 1, "err": None, "memo": None, "blockTime": block_time, "confirmationStatus": "finalized"}


def _page_pool(*pages, **kw):
    source = _stub_pages(*pages)
    pool = PooledRpcSource(
        [Provider("p", source, 1.0, True, None)], clock=FakeClock(wall_start=NOW),
        rate_limit_cooldown_seconds=30.0, age_cache_entries=kw.pop("age_cache_entries", 1000), runner=SequentialRunner(),
    )
    return pool


def _fetch_page(pool):
    return pool.get_signatures_for_address("a", before=None, until=None, limit=1000, deadline=_deadline())


def test_age_cache_bounded_fifo(tmp_path):
    sigs = _sigs("old", 5)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, arch_d) = _pool(
        [_non("n", 12, scenario), _arch("a", 1, scenario)], age_cache_entries=3
    )
    _seed(pool)
    assert list(pool._age) == sigs[2:]  # FIFO: лишились три ОСТАННІ вставлені, перші два витіснено
    assert len(pool._age) == 3

    # поведінково: відомо старі (old02) — лише архівному; витіснені (old00) — вік невідомий, придатні всі
    got = pool.get_transactions([sigs[0], sigs[4]], deadline=_deadline())
    assert _signatures_of(got) == [sigs[0], sigs[4]]
    assert _tx_calls(non_d) == [(sigs[0],)]  # old00 питали у неархівного (вік забуто) → None
    assert _tx_calls(arch_d) == [(sigs[4],), (sigs[0],)]  # old04 — одразу архівному; old00 — підтвердження
    assert len(pool._age) == 3  # запити транзакцій кеш не змінюють


@pytest.mark.parametrize("limit", [1, 2, 7])
def test_age_cache_never_exceeds_limit(limit):
    pool = _page_pool([_entry(f"s{i:03d}", OLD) for i in range(40)], age_cache_entries=limit)
    _fetch_page(pool)
    assert len(pool._age) == limit
    assert list(pool._age) == [f"s{i:03d}" for i in range(40 - limit, 40)]


def test_age_cache_existing_signature_keeps_its_fifo_position_and_size():
    first = [_entry("a", OLD), _entry("b", OLD), _entry("c", OLD)]
    second = [_entry("b", OLD + 1)]
    pool = _page_pool(first, second, age_cache_entries=3)
    _fetch_page(pool)
    _fetch_page(pool)
    assert list(pool._age) == ["a", "b", "c"] and pool._age["b"] == OLD + 1


def test_age_cache_ignores_everything_but_real_int_block_time():
    page = [
        _entry("bool", True), _entry("none", None), _entry("float", 1.7e9), _entry("str", "1700000000"),
        {"signature": 5, "blockTime": 10}, {"blockTime": 10}, "garbage", None, 42, {"signature": None, "blockTime": 3},
        _entry("good", OLD),
    ]
    pool = _page_pool(page)
    assert _fetch_page(pool) is page  # відповідь пул повертає ядру як є
    assert list(pool._age) == ["good"]


def test_unknown_age_for_null_block_time_means_every_provider_is_eligible(tmp_path):
    scenario = _write_scenario(tmp_path, {"only": None})  # blockTime: null (запис без часу)
    non = ScriptedProvider(FixtureRpcSource(scenario))
    arch = ScriptedProvider(FixtureRpcSource(scenario))
    pool = PooledRpcSource(
        [Provider("n", non, 12.0, False, float(RETENTION), frozenset({"getSignaturesForAddress"})), Provider("a", arch, 1.0, True, None)],
        clock=FakeClock(wall_start=NOW), rate_limit_cooldown_seconds=30.0, age_cache_entries=10, runner=SequentialRunner(),
    )
    _seed(pool)
    assert list(pool._age) == []
    pool.get_transactions(["only"], deadline=_deadline())
    assert _tx_calls(non) == [("only",)]


def test_failed_signatures_call_caches_nothing_and_raises_as_is(tmp_path):
    exc = RpcTimeout("request timed out")
    scenario = _write_scenario(tmp_path, {"only": OLD})
    pool, _ = _pool(
        [_arch("a", 1, scenario, scripted={"failures": [ScriptedFailure("getSignaturesForAddress", None, exc)]})]
    )
    with pytest.raises(RpcTimeout) as info:
        _seed(pool)
    assert info.value is exc and len(pool._age) == 0


def test_age_cache_is_filled_by_every_page_that_passes_through_the_pool():
    pool = _page_pool([_entry("a", OLD), _entry("b", FRESH)], [_entry("c", OLD)])
    _fetch_page(pool)
    _fetch_page(pool)
    assert dict(pool._age) == {"a": OLD, "b": FRESH, "c": OLD}


def test_state_lock_is_not_held_while_remembering_ages_or_calling_providers(tmp_path):
    scenario = _write_scenario(tmp_path, {"only": OLD})
    pool, _ = _pool([_arch("a", 1, scenario)])
    _seed(pool)
    assert not pool._state_lock.locked()
    pool.get_transactions(["only"], deadline=_deadline())
    assert not pool._state_lock.locked()


# --------------------------------------------------------------------------- плани НЕ суцільні: збирання за позицією


def test_mixed_old_and_fresh_positions_are_split_by_eligibility_and_gathered_by_position(tmp_path):
    # Позиції чергуються: парні — давні (лише архівні), непарні — свіжі (усі). Плани часток не суцільні.
    ages = {}
    for i in range(12):
        ages[f"s{i:02d}"] = OLD if i % 2 == 0 else FRESH
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)
    pool, (non_d, a1_d, a2_d) = _pool(
        [_non("n", 12, scenario), _arch("a1", 4.5, scenario), _arch("a2", 4.5, scenario)]
    )
    _seed(pool)

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _signatures_of(got) == sigs  # відповідь за ПОЗИЦІЄЮ, а не за порядком провайдерів
    # давні (6): apportion(6, [4.5, 4.5]) = [3, 3]; свіжі (6): apportion(6, [12, 4.5, 4.5]) = [4, 1, 1]
    assert _tx_calls(non_d) == [(sigs[1], sigs[3], sigs[5], sigs[7])]
    assert _tx_calls(a1_d) == [(sigs[0], sigs[2], sigs[4], sigs[9])]
    assert _tx_calls(a2_d) == [(sigs[6], sigs[8], sigs[10], sigs[11])]
    assert pool._not_found_escalations == [0, 0, 0]


@pytest.mark.parametrize("order", ["forward", "reverse", [2, 0, 1], [1, 2, 0], [0, 2, 1]])
def test_mixed_plan_result_and_shares_do_not_depend_on_completion_order(tmp_path, order):
    ages = {f"s{i:02d}": (OLD if i % 3 else FRESH) for i in range(15)}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)

    def run(runner):
        pool, doubles = _pool(
            [_non("n", 12, scenario), _arch("a1", 4.5, scenario), _arch("a2", 4.5, scenario)], runner=runner
        )
        _seed(pool)
        got = pool.get_transactions(sigs, deadline=_deadline())
        return got, [_tx_calls(d) for d in doubles]

    reference = run(SequentialRunner())
    got, shares = run(SequentialRunner(order))
    assert _signatures_of(got) == sigs
    assert (got, shares) == reference


def test_mixed_plan_with_real_threads_is_deterministic(tmp_path):
    ages = {f"s{i:02d}": (OLD if i % 2 else FRESH) for i in range(20)}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)
    expected = None
    for _ in range(5):
        runner = ThreadRunner(3)
        try:
            pool, doubles = _pool(
                [_non("n", 12, scenario), _arch("a1", 4.5, scenario), _arch("a2", 4.5, scenario)], runner=runner
            )
            _seed(pool)
            got = pool.get_transactions(sigs, deadline=_deadline())
            outcome = (_signatures_of(got), [_tx_calls(d) for d in doubles])
        finally:
            runner.close()
        assert outcome[0] == sigs
        expected = expected or outcome
        assert outcome == expected


def test_confirmation_round_with_interleaved_positions_lands_each_answer_at_its_own_position(tmp_path):
    # Невідомий вік; на неархівному «забуто» лише давні, і вони чергуються зі свіжими. У раунді підтвердження
    # позиції НЕ суцільні (0, 2, 4, …): архівний повинен віддати кожну на її місце.
    ages = {f"s{i:02d}": (OLD if i % 2 == 0 else FRESH) for i in range(14)}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)
    pool, (non_d, arch_d) = _pool([_non("n", 12, scenario), _arch("a", 4.5, scenario)])

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _signatures_of(got) == sigs
    # apportion(14, [12, 4.5]) = [10, 4]: неархівному 0..9 (давні 0,2,4,6,8 → None), архівному 10..13
    assert _tx_calls(non_d) == [tuple(sigs[:10])]
    assert _tx_calls(arch_d) == [tuple(sigs[10:]), (sigs[0], sigs[2], sigs[4], sigs[6], sigs[8])]
    assert pool._not_found_escalations == [5, 0]


def test_confirmation_round_split_between_two_archival_noncontiguous(tmp_path):
    ages = {f"s{i:02d}": (OLD if i % 2 == 0 else FRESH) for i in range(20)}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)
    pool, (non_d, a1_d, a2_d) = _pool(
        [_non("n", 12, scenario), _arch("a1", 4.5, scenario), _arch("a2", 4.5, scenario)]
    )
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _signatures_of(got) == sigs
    # apportion(20, [12, 4.5, 4.5]) = [12, 4, 4]; давні серед 0..11 → 0,2,4,6,8,10 (6 позицій)
    assert _tx_calls(non_d) == [tuple(sigs[:12])]
    olds = [sigs[i] for i in (0, 2, 4, 6, 8, 10)]
    # підтвердження: apportion(6, [4.5, 4.5]) = [3, 3] у порядку позицій
    assert _tx_calls(a1_d)[-1] == tuple(olds[:3]) and _tx_calls(a2_d)[-1] == tuple(olds[3:])


def test_share_keeps_input_order_when_groups_interleave(tmp_path):
    # Групи розбирають позиції по черзі: архівний бере давні 0, 3, 5 (група «лише архівні») і свіжу 4 (спільна група).
    # Частка йде провайдерові в порядку підписів входу, а не в порядку груп: 0, 3, 4, 5.
    ages = {"s0": OLD, "s1": FRESH, "s2": FRESH, "s3": OLD, "s4": FRESH, "s5": OLD}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)
    pool, (non_d, arch_d) = _pool([_non("n", 12, scenario), _arch("a", 12, scenario)])
    _seed(pool)
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _signatures_of(got) == sigs
    assert _tx_calls(non_d) == [("s1", "s2")]
    assert _tx_calls(arch_d) == [("s0", "s3", "s4", "s5")]


def test_duplicate_signatures_each_keep_their_own_position(tmp_path):
    scenario = _write_scenario(tmp_path, {"old00": OLD, "fresh00": FRESH})
    pool, _ = _pool([_non("n", 12, scenario), _arch("a", 4.5, scenario)])
    _seed(pool)
    sigs = ["old00", "fresh00", "old00", "fresh00", "old00"]
    assert _signatures_of(pool.get_transactions(sigs, deadline=_deadline())) == sigs


# --------------------------------------------------------------------------- раунди: дедлайн, відмови, дефекти


class _Advancing:
    """Неархівний вузол, що «довго думає»: просуває годинник і відповідає None на все."""

    name = "adv"

    def __init__(self, clock, seconds):
        self.clock, self.seconds, self.calls = clock, seconds, 0

    def get_transactions(self, signatures, *, deadline):
        self.calls += 1
        self.clock.advance(self.seconds)
        return [None] * len(signatures)


def test_deadline_is_checked_before_the_confirmation_round_with_zero_calls(tmp_path):
    clock = FakeClock(wall_start=NOW)
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    adv = _Advancing(clock, 11)
    arch_d = ScriptedProvider(FixtureRpcSource(scenario))
    pool = PooledRpcSource(
        [Provider("n", adv, 12.0, False, float(RETENTION)), Provider("a", arch_d, 4.5, True, None)],
        clock=clock, rate_limit_cooldown_seconds=30.0, age_cache_entries=100, runner=SequentialRunner(),
    )
    with pytest.raises(RpcBudgetTimeout) as info:
        pool.get_transactions(sigs, deadline=Deadline(clock, 10))
    assert str(info.value) == "budget"
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert adv.calls == 1 and len(_tx_calls(arch_d)) == 1  # раунд підтвердження не стартував


def test_failure_in_confirmation_round_is_raised_as_is_without_chain(tmp_path):
    exc = RpcTimeout("request timed out")
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, arch_d) = _pool([
        _non("n", 12, scenario),
        _arch("a", 4.5, scenario, scripted={"failures": [ScriptedFailure("getTransaction", frozenset({2}), exc)]}),
    ])
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert info.value is exc
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert len(_tx_calls(arch_d)) == 2  # перебирання немає (T-140): друга спроба — підтвердження, вона й упала


def test_failure_in_first_round_stops_before_any_confirmation(tmp_path):
    exc = RpcTimeout("request timed out")
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, (non_d, arch_d) = _pool([
        _non("n", 12, scenario),
        _arch("a", 4.5, scenario, scripted={"failures": [ScriptedFailure("getTransaction", None, exc)]}),
    ])
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert info.value is exc
    assert len(_tx_calls(non_d)) == 1 and len(_tx_calls(arch_d)) == 1  # підтверджувального раунду не було
    assert pool._not_found_escalations == [0, 0]  # відкинуті «None» не рахуються як передані архівному


def test_defect_in_confirmation_round_propagates(tmp_path):
    defect = ValueError("adapter bug")
    sigs = _sigs("old", 10)
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, _ = _pool([
        _non("n", 12, scenario),
        _arch("a", 4.5, scenario, scripted={"failures": [ScriptedFailure("getTransaction", frozenset({2}), defect)]}),
    ])
    with pytest.raises(ValueError) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert info.value is defect


def test_repeated_calls_are_deterministic(tmp_path):
    ages = {f"s{i:02d}": (OLD if i % 3 == 0 else FRESH) for i in range(18)}
    sigs = list(ages)
    scenario = _write_scenario(tmp_path, ages)

    def run():
        pool, doubles = _pool([_non("n", 12, scenario), _arch("a1", 4.5, scenario), _arch("a2", 4.5, scenario)])
        got1 = pool.get_transactions(sigs, deadline=_deadline())  # вік невідомий
        _seed(pool)
        got2 = pool.get_transactions(sigs, deadline=_deadline())  # вік відомий
        return got1, got2, [d.log for d in doubles], list(pool._not_found_escalations)

    assert run() == run()


def test_empty_input_does_not_touch_age_state_or_providers(tmp_path):
    scenario = _write_scenario(tmp_path, {"only": OLD})
    pool, (non_d, arch_d) = _pool([_non("n", 12, scenario), _arch("a", 4.5, scenario)])
    assert pool.get_transactions([], deadline=_deadline()) == []
    assert non_d.log == [] and arch_d.log == []


# --------------------------------------------------------------------------- сервіс: SC-004


class _TxHorizon:
    """Неархівний вузол, що забув ТРАНЗАКЦІЇ старші за горизонт, але підписи віддає повністю."""

    def __init__(self, inner, horizon):
        self._inner, self._horizon = inner, horizon

    @property
    def name(self):
        return self._inner.name

    def get_account_info(self, address, *, deadline):
        return self._inner.get_account_info(address, deadline=deadline)

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        return self._inner.get_signatures_for_address(address, before=before, until=until, limit=limit, deadline=deadline)

    def get_token_accounts_by_owner(self, owner, *, deadline):
        return self._inner.get_token_accounts_by_owner(owner, deadline=deadline)

    def get_transactions(self, signatures, *, deadline):
        raws = self._inner.get_transactions(signatures, deadline=deadline)
        return [None if raw is not None and raw.get("blockTime") is not None and raw["blockTime"] < self._horizon else raw
                for raw in raws]


SERVICE_CASES = ("basic", "hub", "swapsend")


def _service_setup(name):
    directory = SCENARIOS / name
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    if name == "hub":
        values = {**json.loads((SCENARIOS / "basic" / "expected.json").read_text())["config"], **expected["cases"][0]["config"]}
    else:
        values = expected["config"]
    mint = expected["mint"]
    times = [
        tx["blockTime"]
        for tx in json.loads((directory / "rpc.json").read_text(encoding="utf-8"))["getTransaction"].values()
        if isinstance(tx, dict) and isinstance(tx.get("blockTime"), int)
    ]
    now = max(times) + 10
    horizon = int(statistics.median(times))  # приблизно половина транзакцій «давніші за історію» неархівного
    assert min(times) < horizon <= max(times)
    cfg = dataclasses.replace(load_config(SHIPPED), **values)
    return directory, mint, cfg, now, horizon


def _service_pool(directory, now, horizon, *, with_archival=True, age_cache_entries=1000):
    non_d = ScriptedProvider(_TxHorizon(FixtureRpcSource(directory), horizon))
    providers = [Provider("rpcfast", non_d, 12.0, False, float(now - horizon))]
    doubles = [non_d]
    if with_archival:
        arch_d = ScriptedProvider(FixtureRpcSource(directory))
        providers.append(Provider("helius", arch_d, 4.5, True, None))
        doubles.append(arch_d)
    pool = PooledRpcSource(
        providers, clock=FakeClock(wall_start=now), rate_limit_cooldown_seconds=30.0,
        age_cache_entries=age_cache_entries, runner=SequentialRunner("reverse"),
    )
    return pool, doubles


@pytest.mark.parametrize("cache_entries", [1000, 1], ids=["age_known", "age_mostly_forgotten"])
@pytest.mark.parametrize("name", SERVICE_CASES)
def test_nonarchival_plus_archival_pool_equals_single_source(name, cache_entries):
    directory, mint, cfg, now, horizon = _service_setup(name)
    single = IngestService(cfg, FixtureRpcSource(directory), FakeClock(wall_start=now)).collect(mint)
    pool, (non_d, arch_d) = _service_pool(directory, now, horizon, age_cache_entries=cache_entries)

    pooled = IngestService(cfg, pool, FakeClock(wall_start=now)).collect(mint)

    assert isinstance(single, IngestResult) and isinstance(pooled, IngestResult)
    assert to_json(pooled) == to_json(single)  # забуті неархівним транзакції підтвердив/віддав архівний
    assert single.buyers
    assert _tx_calls(non_d) and _tx_calls(arch_d)  # обидва в роботі
    if cache_entries == 1:
        assert pool._not_found_escalations[0] > 0  # вік забуто: «None» неархівного довелося підтверджувати
    else:
        # ядро завжди спершу перегортає історію → вік відомий → неархівного про давнє не питали зовсім
        assert pool._not_found_escalations == [0, 0]


@pytest.mark.parametrize("name", SERVICE_CASES)
def test_nonarchival_only_pool_is_incomplete_not_clean(name):
    directory, mint, cfg, now, horizon = _service_setup(name)
    single = IngestService(cfg, FixtureRpcSource(directory), FakeClock(wall_start=now)).collect(mint)
    pool, _ = _service_pool(directory, now, horizon, with_archival=False)

    pooled = IngestService(cfg, pool, FakeClock(wall_start=now)).collect(mint)

    assert isinstance(pooled, IngestResult)
    assert pooled.completeness.status is CompletenessStatus.INCOMPLETE  # не «complete» і не «порожньо»
    assert single.completeness.status is CompletenessStatus.COMPLETE
    buyers = pooled.completeness.buyers
    assert buyers.complete is False and buyers.reason.value == "unavailable"  # причина — недоступність, не «порожньо»
    assert RETENTION_TEXT in buyers.detail  # чесна мітка пулу дійшла до результату
