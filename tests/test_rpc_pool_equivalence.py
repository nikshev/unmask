# verifies: FR-007-01, FR-007-04, FR-007-05
"""Еквівалентність пулу одиночному джерелу (T-134, critical; contracts/pooled-rpc-source.md).

Пул (`PooledRpcSource`) — це ще один `RpcSource`; `IngestService` працює з ним без змін. Помилка тут тиха:
пул, що перемішав відповіді, загубив елемент чи змінив клас помилки, не ламає збірку — він змінює висновок
(покупці, перекази, повнота), тож тест порівнює не «щось повернулось», а БАЙТИ канонічного JSON.

Що доводять тести (усе на записаних фікстурах і `FakeClock`, без мережі, потоків і реального часу, окрім
одного прогону зі справжнім `ThreadRunner`):

- `test_pool_equals_single[basic|hub|corrupt|swapsend]` — FR-007-01/04: `to_json` результату збору через пул
  із трьох архівних `ScriptedProvider` (власні `FixtureRpcSource`, ваги 12/4.5/4.5, `SequentialRunner("reverse")`)
  == `to_json` збору через один `FixtureRpcSource`, разом із volatile-полями (`rpc_calls`, `source`, …);
  до того ж пул справді ділить пакети (непорожній перелік провайдерів, що обслужили `getTransaction`), тож
  рівність не порожня. `basic` додатково звірено з незалежним еталоном `expected.json`;
- `test_pool_equals_single_for_edge_batch_sizes` / `..._for_pool_shapes` — межові випадки: пакет із 1, 2, 3
  підписів (одна/дві/три частки, нуль у частки), великий пакет, один провайдер, нерівні ваги, найважчий
  в кінці, провайдер у середині, що пропускає метод, провайдер лише для `getTransaction`;
- `test_source_level_*` — те саме без сервісу, на рівні протоколу: порожній пакет, дублікати, невідомі
  підписи (→ `None`), довільний порядок; одиночні методи (SWRR обходить усіх провайдерів — відповіді
  однакові, хто б не обслужив);
- `test_rejection_through_pool_equals_single` — відмови (`token_not_found`, `invalid_address`) ідентичні;
- `test_pool_result_independent_of_runner_order` — SC-003: прямий, зворотний, кілька випадкових (із
  зафіксованим зерном) порядків виконання часток і справжній `ThreadRunner` дають той самий JSON;
- `test_resume_through_the_same_pool_equals_fresh` — те саме, але `resume` іде через ТОЙ САМИЙ екземпляр пулу
  (перемикач збою `_Outage`): пул не має «памʼятати» впалі підписи;
- `test_resume_through_pool_equals_fresh` — FR-007-05: збій `FailFor` на ВСІХ провайдерах першого прогону
  (також у пакеті `getTransaction`) → неповний результат з тією самою причиною, що й в одиночного джерела
  (клас помилки не змінився) → `resume` через новий чистий пул == `resume` через одиночне джерело
  (побайтово) == свіжий збір (без volatile-полів; `basic` — ще й `expected.json`);
- `test_core_rpc_calls_unchanged_by_pool` — кількість і склад звернень ядра не залежать від пулу
  (пояснення, що саме порівнюється, — у докстрінгу тесту).

`_golden_view` — копія з `tests/test_resume.py` (порівняння з формою `expected.json`).
"""

import dataclasses
import json
import random
from collections import Counter
from pathlib import Path

import pytest

from rpc_pool_doubles import ScriptedProvider, SequentialRunner
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult, Rejection
from unmask.ingest.rpc.fixture import FailFor, FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, ThreadRunner
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable
from unmask.ingest.serialize import to_dict, to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SHIPPED = ROOT / "config" / "ingest.yaml"
BASIC, HUB, CORRUPT, SWAPSEND, NOTFOUND = (
    SCENARIOS / name for name in ("basic", "hub", "corrupt", "swapsend", "notfound")
)

BASIC_EXPECTED = json.loads((BASIC / "expected.json").read_text(encoding="utf-8"))
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text(encoding="utf-8"))
SWAPSEND_EXPECTED = json.loads((SWAPSEND / "expected.json").read_text(encoding="utf-8"))
CORRUPT_MINT = json.loads((CORRUPT / "rpc.json").read_text(encoding="utf-8"))["_meta"]["cast"]["M"]
NOTFOUND_CAST = json.loads((NOTFOUND / "rpc.json").read_text(encoding="utf-8"))["_meta"]["cast"]

# Ваги з T-134: RPC Fast (12 запитів/с) + два Helius (4.5). Усі провайдери архівні.
WEIGHTS = (12.0, 4.5, 4.5)

# (каталог сценарію, mint, значення конфігу) — ті самі конфіги, що в test_delegated_wiring / test_collector_failures.
CASES = {
    "basic": (BASIC, BASIC_EXPECTED["mint"], BASIC_EXPECTED["config"]),
    "hub": (HUB, HUB_EXPECTED["mint"], {**BASIC_EXPECTED["config"], **HUB_EXPECTED["cases"][0]["config"]}),
    "corrupt": (CORRUPT, CORRUPT_MINT, {**BASIC_EXPECTED["config"], "first_buyers_n": 3, "funding_depth": 2}),
    "swapsend": (SWAPSEND, SWAPSEND_EXPECTED["mint"], SWAPSEND_EXPECTED["config"]),
}

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
CONFIG_KEYS = (
    "first_buyers_n", "funding_depth", "counterparty_threshold", "max_signatures_per_wallet", "collect_spl_inbound",
)
GOLDEN = {k: BASIC_EXPECTED[k] for k in ("mint", "config", "completeness", "buyers", "transfers", "unexpanded")}


# --------------------------------------------------------------------------- допоміжне


def _cfg(values: dict, *, tx_batch_size: int | None = None):
    cfg = dataclasses.replace(load_config(SHIPPED), **values)
    if tx_batch_size is not None:
        cfg = dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, tx_batch_size=tx_batch_size))
    return cfg


def _deadline() -> Deadline:
    return Deadline(FakeClock(), 600.0)


def _kit(directory, *, weights=WEIGHTS, skips=None, runner=None, failures=None, wrap=None):
    """Пул із `ScriptedProvider` на провайдера (власне `FixtureRpcSource` кожному) і самі дублі.

    `skips` — `{індекс: набір імен методів}`; `failures` — фабрика списку `FailFor` (на кожного провайдера
    свій екземпляр: політика має змінний лічильник); `wrap` — обгортка над `FixtureRpcSource` кожного
    провайдера (`_Outage`). За замовчуванням runner — `SequentialRunner("reverse")`.
    """
    skips = skips or {}
    doubles, providers = [], []
    for index, weight in enumerate(weights):
        inner = FixtureRpcSource(directory, failures=failures() if failures else None)
        double = ScriptedProvider(wrap(inner) if wrap else inner)
        doubles.append(double)
        providers.append(
            Provider(
                name=f"p{index}",
                source=double,
                weight=weight,
                archival=True,
                retention_seconds=None,
                skip_methods=frozenset(skips.get(index, ())),
            )
        )
    pool = PooledRpcSource(
        providers,
        clock=FakeClock(),
        rate_limit_cooldown_seconds=30.0,
        age_cache_entries=1000,
        runner=runner if runner is not None else SequentialRunner("reverse"),
    )
    return pool, doubles


def _collect(source, cfg, mint, cache=None):
    return IngestService(cfg, source, FakeClock(), cache).collect(mint)


def _plain(obj) -> object:
    return json.loads(json.dumps(dataclasses.asdict(obj)))


def _golden_view(result: IngestResult) -> dict:
    """Результат у формі `expected.json` (копія з tests/test_resume.py)."""
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


def _stable(result: IngestResult) -> dict:
    """Повний `to_dict` без volatile-полів metadata (зокрема `delegated`, якого немає в `_golden_view`)."""
    doc = to_dict(result)
    for key in VOLATILE:
        doc["metadata"].pop(key)
    return doc


def _largest_remainder(n: int, weights) -> list[int]:
    """Незалежний від `pool.apportion` метод найбільших залишків (точні дроби; нічия — менший індекс)."""
    from fractions import Fraction

    exact = [Fraction(w) for w in weights]
    quotas = [n * w / sum(exact) for w in exact]
    counts = [int(q) for q in quotas]
    order = sorted(range(len(weights)), key=lambda i: (-(quotas[i] - counts[i]), i))
    for i in order[: n - sum(counts)]:
        counts[i] += 1
    return counts


def _served_transactions(doubles) -> list[int]:
    """Індекси провайдерів, що обслужили хоч один `getTransaction`."""
    return [i for i, d in enumerate(doubles) if any(m == "getTransaction" for m, _ in d.log)]


# --------------------------------------------------------------------------- пул == одиночне джерело


@pytest.mark.parametrize("name", ["basic", "hub", "corrupt", "swapsend"])
def test_pool_equals_single(name):
    directory, mint, values = CASES[name]
    cfg = _cfg(values)

    single = _collect(FixtureRpcSource(directory), cfg, mint)
    pool, doubles = _kit(directory)
    pooled = _collect(pool, cfg, mint)

    assert isinstance(single, IngestResult) and isinstance(pooled, IngestResult)
    assert to_json(pooled) == to_json(single)  # побайтово, разом з rpc_calls і metadata.source
    # рівність не порожня: збір щось знайшов і пул справді ділив пакети між провайдерами
    assert single.buyers
    assert len(_served_transactions(doubles)) >= 2
    if name == "basic":  # незалежний еталон: обидва шляхи не можуть помилятись однаково
        assert _golden_view(pooled) == GOLDEN
        assert pooled.completeness.status is CompletenessStatus.COMPLETE
    if name == "corrupt":  # пошкоджені записи — дані: неповнота виводиться однаково
        assert pooled.completeness.status is CompletenessStatus.INCOMPLETE


@pytest.mark.parametrize("name", ["basic", "swapsend"])
@pytest.mark.parametrize("batch", [1, 2, 3, 4, 7, 25, 100])
def test_pool_equals_single_for_edge_batch_sizes(name, batch):
    # Пакет із 1 підпису йде одному (найважчому) провайдерові, з 2-3 — частині провайдерів, решта має
    # нульову частку; великий пакет ділиться за вагами. Результат не залежить від розміру пакета ядра.
    directory, mint, values = CASES[name]
    cfg = _cfg(values, tx_batch_size=batch)

    single = _collect(FixtureRpcSource(directory), cfg, mint)
    pool, doubles = _kit(directory)
    pooled = _collect(pool, cfg, mint)

    assert to_json(pooled) == to_json(single)
    shares = [len(keys) for d in doubles for m, keys in d.log if m == "getTransaction"]
    assert shares and max(shares) <= batch
    if batch == 1:
        assert _served_transactions(doubles) == [0]  # єдиний підпис завжди найважчому
    else:
        assert len(_served_transactions(doubles)) >= 2


POOL_SHAPES = {
    "solo": dict(weights=(1.0,)),
    "equal2": dict(weights=(1.0, 1.0)),
    "heaviest-last": dict(weights=(4.5, 4.5, 12.0)),
    "four": dict(weights=(12.0, 4.5, 4.5, 4.5)),
    "middle-skips-transactions": dict(skips={1: {"getTransaction"}}),
    "first-only-transactions": dict(
        skips={0: {"getAccountInfo", "getSignaturesForAddress", "getTokenAccountsByOwner"},
               1: {"getTransaction"}, 2: {"getTransaction"}}
    ),
}


@pytest.mark.parametrize("shape", list(POOL_SHAPES))
@pytest.mark.parametrize("name", ["basic", "swapsend"])
def test_pool_equals_single_for_pool_shapes(name, shape):
    directory, mint, values = CASES[name]
    cfg = _cfg(values)
    spec = POOL_SHAPES[shape]

    single = _collect(FixtureRpcSource(directory), cfg, mint)
    pool, doubles = _kit(directory, **spec)
    pooled = _collect(pool, cfg, mint)

    assert to_json(pooled) == to_json(single)
    if shape == "middle-skips-transactions":  # пропущений метод провайдер не обслуговує, а решта його не чекає
        assert 1 not in _served_transactions(doubles)
        assert _served_transactions(doubles) == [0, 2]
    if shape == "first-only-transactions":
        assert [m for m, _ in doubles[0].log] == ["getTransaction"] * len(doubles[0].log)
        assert _served_transactions(doubles) == [0]


@pytest.mark.parametrize("mint_key", ["MISSING_MINT", "SYSTEM_OWNED", "TOKEN_ACCOUNT"])
def test_rejection_through_pool_equals_single(mint_key):
    mint = NOTFOUND_CAST[mint_key]
    cfg = _cfg(BASIC_EXPECTED["config"])
    single = _collect(FixtureRpcSource(NOTFOUND), cfg, mint)
    pool, doubles = _kit(NOTFOUND)
    pooled = _collect(pool, cfg, mint)
    assert isinstance(single, Rejection) and isinstance(pooled, Rejection)
    assert to_json(pooled) == to_json(single)
    assert sum(len(d.log) for d in doubles) == 1  # лише getAccountInfo, жодного зайвого звернення


def test_invalid_address_is_rejected_before_any_provider_call():
    cfg = _cfg(BASIC_EXPECTED["config"])
    single = _collect(FixtureRpcSource(BASIC), cfg, "not-an-address")
    pool, doubles = _kit(BASIC)
    pooled = _collect(pool, cfg, "not-an-address")
    assert isinstance(pooled, Rejection) and to_json(pooled) == to_json(single)
    assert all(not d.log for d in doubles)


# --------------------------------------------------------------------------- рівень протоколу джерела


def _transaction_batches(directory) -> list[list[str]]:
    """Пакети різної довжини з усіх записаних підписів: порожній, одиночні, дублікати, невідомі, перемішані."""
    recorded = list(json.loads((directory / "rpc.json").read_text(encoding="utf-8"))["getTransaction"])
    rng = random.Random(134)
    batches: list[list[str]] = [
        [], [recorded[0]], ["unknown-signature"], [recorded[0], recorded[0]],
        recorded[:3], [recorded[2], "unknown-signature", recorded[1]],
    ]
    for size in range(2, min(len(recorded), 40) + 1):
        batch = rng.sample(recorded, size)
        batch.insert(rng.randrange(size + 1), "unknown-%d" % size)  # невідомий підпис посеред пакета -> None
        batch.append(batch[rng.randrange(len(batch))])  # дублікат: той самий підпис у двох позиціях
        batches.append(batch)
    return batches


@pytest.mark.parametrize("order", ["forward", "reverse"])
@pytest.mark.parametrize("name", ["basic", "hub"])
def test_source_level_get_transactions_equals_single_on_edge_batches(name, order):
    directory = CASES[name][0]
    single = FixtureRpcSource(directory)
    pool, doubles = _kit(directory, runner=SequentialRunner(order))
    sizes = set()
    for batch in _transaction_batches(directory):
        sizes.add(len(batch))
        assert pool.get_transactions(batch, deadline=_deadline()) == single.get_transactions(
            batch, deadline=_deadline()
        ), batch
    assert {0, 1, 2, 3} <= sizes and max(sizes) >= 20
    # порожній пакет — нуль звернень до провайдерів; невідомий підпис справді дає None (а не зникає)
    assert pool.get_transactions(["unknown-signature"], deadline=_deadline()) == [None]
    assert len(_served_transactions(doubles)) == 3


@pytest.mark.parametrize("name", ["basic", "corrupt"])
def test_source_level_single_methods_equal_single_whoever_serves(name):
    directory = CASES[name][0]
    recorded = json.loads((directory / "rpc.json").read_text(encoding="utf-8"))
    single = FixtureRpcSource(directory)
    pool, doubles = _kit(directory)
    unknown = "unknown-address"

    accounts = list(recorded["getAccountInfo"]) + [unknown]
    histories = list(recorded["getSignaturesForAddress"]) + [unknown]
    owners = list(recorded["getTokenAccountsByOwner"]) + [unknown]
    for _ in range(6):  # SWRR обходить усіх провайдерів: кожен має віддати те саме, що одиночне джерело
        for address in accounts:
            assert pool.get_account_info(address, deadline=_deadline()) == single.get_account_info(
                address, deadline=_deadline()
            )
        for owner in owners:
            assert pool.get_token_accounts_by_owner(owner, deadline=_deadline()) == (
                single.get_token_accounts_by_owner(owner, deadline=_deadline())
            )
        for address in histories:
            history = recorded["getSignaturesForAddress"].get(address, [])
            cursors = [(None, None, 1000), (None, None, 1), (None, None, 0)]
            if len(history) >= 3:
                cursors += [(history[0]["signature"], None, 1000), (None, history[-1]["signature"], 1000),
                            (history[0]["signature"], history[-1]["signature"], 2)]
            for before, until, limit in cursors:
                kwargs = dict(before=before, until=until, limit=limit)
                assert pool.get_signatures_for_address(address, deadline=_deadline(), **kwargs) == (
                    single.get_signatures_for_address(address, deadline=_deadline(), **kwargs)
                )
    for method in ("getAccountInfo", "getTokenAccountsByOwner", "getSignaturesForAddress"):
        served = [i for i, d in enumerate(doubles) if any(m == method for m, _ in d.log)]
        assert served == [0, 1, 2], method  # неспростовно: відповіли всі три провайдери


# --------------------------------------------------------------------------- SC-003: порядок виконання часток


class _OrderedRunner:
    """Runner, що виконує задачі раунду у вказаному порядку (через `SequentialRunner(перестановка)`) і
    запамʼятовує його. Довжина перестановки — кількість задач раунду (1…3), тож порядок обчислюється на місці."""

    def __init__(self, kind: str, seed: int = 0) -> None:
        self._kind = kind
        self._rng = random.Random(seed)
        self.orders: list[tuple[int, ...]] = []

    def __call__(self, tasks):
        order = list(range(len(tasks)))
        if self._kind == "reverse":
            order.reverse()
        elif self._kind == "shuffle":
            self._rng.shuffle(order)
        self.orders.append(tuple(order))
        return SequentialRunner(order)(tasks)


@pytest.mark.parametrize("name", ["basic", "hub", "corrupt", "swapsend"])
def test_pool_result_independent_of_runner_order(name):
    """SC-003: різний порядок завершення часток раунду -> побайтово той самий результат (і той самий, що в
    одиночного джерела). Порядки: прямий, зворотний, три випадкові (зафіксоване зерно), "reverse" самого
    `SequentialRunner`, та справжні потоки (`ThreadRunner`)."""
    directory, mint, values = CASES[name]
    cfg = _cfg(values)
    expected = to_json(_collect(FixtureRpcSource(directory), cfg, mint))

    runners = [_OrderedRunner("forward"), _OrderedRunner("reverse")]
    runners += [_OrderedRunner("shuffle", seed) for seed in (1, 2, 3)]
    for runner in runners:
        pool, doubles = _kit(directory, runner=runner)
        assert to_json(_collect(pool, cfg, mint)) == expected, runner.orders[:5]
        multi = [order for order in runner.orders if len(order) >= 2]
        assert multi, "жоден раунд не мав двох часток: порядок завершення нічого б не перевіряв"
        if runner is not runners[0]:  # порядок справді відрізнявся від прямого хоча б у одному раунді
            assert any(order != tuple(sorted(order)) for order in multi)

    for order in ("forward", "reverse"):  # власні рядкові режими дубля
        pool, _ = _kit(directory, runner=SequentialRunner(order))
        assert to_json(_collect(pool, cfg, mint)) == expected

    threads = ThreadRunner(3)
    try:
        pool, doubles = _kit(directory, runner=threads)
        assert to_json(_collect(pool, cfg, mint)) == expected
        assert len(_served_transactions(doubles)) >= 2
    finally:
        threads.close()


# --------------------------------------------------------------------------- resume через пул


def _failure_cases():
    basic_buyers = BASIC_EXPECTED["buyers"]
    swap_buyers = SWAPSEND_EXPECTED["buyers"]
    keys = [
        ("basic", "wallet-A", BASIC_EXPECTED["wallets"]["A"]),
        ("basic", "buyer-transaction", basic_buyers[0]["first_buy_signature"]),
        ("basic", "funding-transaction", BASIC_EXPECTED["transfers"][0]["signature"]),
        ("swapsend", "buyer-wallet", swap_buyers[0]["wallet"]),
        ("swapsend", "buyer-transaction", swap_buyers[0]["first_buy_signature"]),
        ("swapsend", "delegated-link-transaction", SWAPSEND_EXPECTED["delegated"]["links"][0]["signature"]),
    ]
    errors = [
        ("unavailable", lambda: RpcUnavailable("node down")),
        ("rate-limited", lambda: RpcRateLimited(retry_after=1.0)),
        ("timeout", lambda: RpcTimeout("slow")),
    ]
    return [
        pytest.param(scenario, key, make_exc, id=f"{scenario}-{label}-{exc_label}")
        for scenario, label, key in keys
        for exc_label, make_exc in errors
    ]


@pytest.mark.parametrize(("scenario", "key", "make_exc"), _failure_cases())
def test_resume_through_pool_equals_fresh(scenario, key, make_exc):
    """FR-007-05: збій на ВСІХ провайдерах -> неповний результат (як в одиночного джерела) -> `resume` через
    новий чистий пул == `resume` через одиночне джерело (побайтово) == свіжий збір (без volatile-полів)."""
    directory, mint, values = CASES[scenario]
    cfg = _cfg(values)
    outage = lambda: [FailFor(key, make_exc(), times=1000)]  # noqa: E731 — свій екземпляр на провайдера

    # одиночне джерело: збій -> resume
    single_cache = ResultCache()
    single_first = _collect(FixtureRpcSource(directory, failures=outage()), cfg, mint, single_cache)
    single_second = _collect(FixtureRpcSource(directory), cfg, mint, single_cache)
    # пул: той самий збій на всіх трьох провайдерах -> resume новим чистим пулом із тим самим кешем
    pool_cache = ResultCache()
    failing_pool, failing_doubles = _kit(directory, failures=outage)
    pool_first = _collect(failing_pool, cfg, mint, pool_cache)
    # перший прогін: збій справді зачепив збір і лишив лише partial; клас помилки -> причина не змінились
    assert pool_first.completeness.status is CompletenessStatus.INCOMPLETE
    assert pool_cache.get_complete(mint) is None and pool_cache.get_partial(mint) is not None
    # УВАГА (T-140): рівність first-прогону залежить від того, який клас помилки пул підніме після вичерпання
    # всіх провайдерів (контракт: `last_error`, тобто той самий об'єкт адаптера). Зміна цього правила = дефект.
    assert to_json(pool_first) == to_json(single_first)
    assert any(key in keys for d in failing_doubles for _m, keys in d.log)  # збій відбувся через пул
    # resume новим чистим пулом: той самий процес «збій -> продовження», що й в одиночного джерела
    clean_pool, _ = _kit(directory)
    pool_second = _collect(clean_pool, cfg, mint, pool_cache)
    fresh = _collect(FixtureRpcSource(directory), cfg, mint)
    assert pool_second.metadata.resumed is True
    assert to_json(pool_second) == to_json(single_second)
    # ... і результат дорівнює свіжому збору
    assert pool_second.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(pool_second) == _stable(fresh)
    assert pool_cache.get_complete(mint) is not None and pool_cache.get_partial(mint) is None
    if scenario == "basic":
        assert _golden_view(pool_second) == GOLDEN


class _Outage:
    """Перемикач збою над джерелом провайдера: поки `switch.on`, будь-яке звернення, у параметрах якого є
    `key` (адреса, курсор або підпис пакета), піднімає виняток; потім джерело «відпускає». Один перемикач
    на всі три провайдери — збій вмикається/вимикається для всього пулу одразу, а сам пул лишається тим
    самим екземпляром (зі своїм станом: SWRR, далі кеш віку, паузи, лічильники)."""

    class Switch:
        on = True

    def __init__(self, inner, key: str, make_exc, switch: "_Outage.Switch") -> None:
        self._inner, self._key, self._make_exc, self._switch = inner, key, make_exc, switch

    @property
    def name(self) -> str:
        return self._inner.name

    def _check(self, *keys) -> None:
        if self._switch.on and self._key in keys:
            raise self._make_exc()

    def get_account_info(self, address, *, deadline):
        self._check(address)
        return self._inner.get_account_info(address, deadline=deadline)

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        self._check(address, before, until)
        return self._inner.get_signatures_for_address(
            address, before=before, until=until, limit=limit, deadline=deadline
        )

    def get_transactions(self, signatures, *, deadline):
        self._check(*signatures)
        return self._inner.get_transactions(signatures, deadline=deadline)

    def get_token_accounts_by_owner(self, owner, *, deadline):
        self._check(owner)
        return self._inner.get_token_accounts_by_owner(owner, deadline=deadline)


@pytest.mark.parametrize(("scenario", "key", "make_exc"), _failure_cases())
def test_resume_through_the_same_pool_equals_fresh(scenario, key, make_exc):
    """FR-007-05: пул довгоживучий і має стан, тож у роботі `resume` іде через ТОЙ САМИЙ екземпляр, що бачив
    збій. Перший прогін — збій на всіх провайдерах (перемикач увімкнено) -> `partial`; потім перемикач
    вимкнено, і другий прогін через ТОЙ САМИЙ `pool` із ТИМ САМИМ `ResultCache` продовжує збір. Пул не
    повинен «памʼятати» впалі підписи/провайдерів: результат == свіжому збору (без volatile-полів)."""
    directory, mint, values = CASES[scenario]
    cfg = _cfg(values)
    switch = _Outage.Switch()
    pool, doubles = _kit(directory, wrap=lambda inner: _Outage(inner, key, make_exc, switch))
    cache = ResultCache()

    switch.on = True
    first = _collect(pool, cfg, mint, cache)
    assert first.completeness.status is CompletenessStatus.INCOMPLETE
    assert cache.get_complete(mint) is None and cache.get_partial(mint) is not None
    assert any(key in keys for d in doubles for _m, keys in d.log)  # збій відбувся через пул

    switch.on = False
    second = _collect(pool, cfg, mint, cache)  # той самий пул, той самий кеш
    fresh = _collect(FixtureRpcSource(directory), cfg, mint)
    assert second.metadata.resumed is True
    assert second.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(second) == _stable(fresh)
    assert cache.get_complete(mint) is not None and cache.get_partial(mint) is None
    if scenario == "basic":
        assert _golden_view(second) == GOLDEN


# --------------------------------------------------------------------------- звернення ядра


class _CoreSpy:
    """Журнал звернень, які ЯДРО робить до джерела (до того, як пул розкладе їх на провайдерів).

    Формат запису — як у `FixtureRpcSource.calls`: `(метод, параметри)`, `get_transactions` — один запис на
    пакет."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._inner.name

    def get_account_info(self, address, *, deadline):
        self.calls.append(("getAccountInfo", {"address": address}))
        return self._inner.get_account_info(address, deadline=deadline)

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        self.calls.append(
            ("getSignaturesForAddress", {"address": address, "before": before, "until": until, "limit": limit})
        )
        return self._inner.get_signatures_for_address(
            address, before=before, until=until, limit=limit, deadline=deadline
        )

    def get_transactions(self, signatures, *, deadline):
        self.calls.append(("getTransaction", {"signatures": list(signatures)}))
        return self._inner.get_transactions(signatures, deadline=deadline)

    def get_token_accounts_by_owner(self, owner, *, deadline):
        self.calls.append(("getTokenAccountsByOwner", {"owner": owner}))
        return self._inner.get_token_accounts_by_owner(owner, deadline=deadline)


@pytest.mark.parametrize("name", ["basic", "hub", "corrupt", "swapsend"])
def test_core_rpc_calls_unchanged_by_pool(name):
    """Пул не змінює, ЩО і СКІЛЬКИ ядро просить у джерела.

    Порівнюється ЖУРНАЛ ЗВЕРНЕНЬ ЯДРА (`_CoreSpy` стоїть між сервісом і пулом, формат як у
    `FixtureRpcSource.calls`) і `metadata.rpc_calls`, а НЕ сума звернень провайдерів: пул за задумом
    розкладає один пакет `getTransaction` ядра на кілька звернень до провайдерів (паралелізм, FR-007-01),
    тож сума звернень провайдерів законно більша за `rpc_calls`. Тому:
      1. журнал ядра через пул == журнал ядра через одиночне джерело (кількість, склад, порядок, параметри);
      2. `metadata.rpc_calls` == довжина цього журналу == `rpc_calls` одиночного прогону;
      3. на рівні провайдерів: одиночні методи обслуговуються РІВНО раз на звернення ядра (жодних зайвих
         чи дубльованих), а підписи `getTransaction` усіх часток разом == підписи пакетів ядра (кожен
         підпис запитано рівно стільки разів, скільки його просило ядро; частки лише розкладають пакет).
    """
    directory, mint, values = CASES[name]
    cfg = _cfg(values)

    single_source = FixtureRpcSource(directory)
    single = _collect(single_source, cfg, mint)

    pool, doubles = _kit(directory)
    spy = _CoreSpy(pool)
    pooled = _collect(spy, cfg, mint)

    assert spy.calls == single_source.calls  # (1)
    assert pooled.metadata.rpc_calls == single.metadata.rpc_calls == len(spy.calls)  # (2)
    assert len(spy.calls) > 10  # неспростовно: зібрано багато звернень

    provider_calls = [entry for double in doubles for entry in double.log]
    for method, field in (
        ("getAccountInfo", "address"),
        ("getSignaturesForAddress", "address"),
        ("getTokenAccountsByOwner", "owner"),
    ):
        core = Counter(params[field] for m, params in spy.calls if m == method)
        served = Counter(keys[0] for m, keys in provider_calls if m == method)
        assert served == core, method  # (3): рівно одне звернення провайдера на звернення ядра
        assert all(len(keys) == 1 for m, keys in provider_calls if m == method)

    core_signatures = Counter(sig for m, params in spy.calls if m == "getTransaction" for sig in params["signatures"])
    shares = [keys for m, keys in provider_calls if m == "getTransaction"]
    assert Counter(sig for keys in shares for sig in keys) == core_signatures  # (3)
    # навантаження йде за вагами: кожен провайдер у сумі отримав стільки підписів, скільки дає метод
    # найбільших залишків (незалежна реалізація нижче) для кожного пакета ядра окремо
    expected_load = [0] * len(WEIGHTS)
    for m, params in spy.calls:
        if m == "getTransaction":
            for index, count in enumerate(_largest_remainder(len(params["signatures"]), WEIGHTS)):
                expected_load[index] += count
    assert [sum(len(keys) for m, keys in d.log if m == "getTransaction") for d in doubles] == expected_load
    core_tx_calls = sum(1 for m, _ in spy.calls if m == "getTransaction")
    assert core_tx_calls < len(shares) <= core_tx_calls * len(WEIGHTS)  # справжнє розкладання, не більше ніж на всіх
