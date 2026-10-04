# verifies: FR-002-11
"""Швидкодія графа й відсікання (T-042; FR-002-11, SC-007; research R-17, R-22).

SC-007: побудова графа й відсікання для 300 покупців із типовим результатом збору — менш ніж за 2 секунди без
звернень до RPC. Тест документує вимогу, а не «підганяє» її під машину: поріг у `test_300_buyers_...` рівний SC
(R-17, «без запасу»). Тести, залежні від годинника (`perf`: SC-007 і лінійність), виключені з типового прогону
(`addopts = -m 'not perf'` у pyproject): на завантаженій машині реальний запас ≈ 1.0× (ревʼю T-042: 3 хибні падіння з 3
під 8 CPU-процесами), тож їх запускають явно — `pytest -m perf` на вільній машині — як крок контрольної точки
фази 5 і гейту ревʼю перед мержем. Hub- і dust-тести від часу не залежать і лишаються в типовому прогоні.

Синтетичний `IngestResult` будує детермінований генератор (`random.Random(seed)`, адреси й підписи — з SHA-256/512
за номером; жодного `random` без насіння, годинника чи мережі). Форма — типовий результат збору 001:

- 300 покупців; кожен має приватних фінансистів глибини 1 (7), у кожного — приватні відправники глибини 2 (6);
  частина пар повторюється (count > 1), тож агрегація ребер працює не вхолосту. Приватні джерела не склеюють
  покупців між собою: після відсікання кожен покупець — окрема компонента;
- хаб H: 200 одноразових відправників (глибина 2), фінансує 250 покупців (по 0,5 SOL), позначка
  `unexpanded(high_degree)`;
- пилове джерело D: фінансує 250 покупців по 500 000 лампортів, 2 відправники (глибина 2).

Разом ≈ 20 000 переказів на глибинах 1–2. Для лінійності (`test_runtime_grows_roughly_linearly_with_edges`) той
самий генератор масштабується кількістю покупців із тими самими пропорціями (хаб — 2/3 N відправників, 5/6 N
покупців; пил — 5/6 N покупців), тож ребер рівно вдвічі більше, а форма та сама.

Результат осмислений, не лише швидкий: хаб і пилове джерело відсічені, частка покупців у найбільшій компоненті
падає з ≈ 1.0 майже до 0.
"""

import gc
import hashlib
import random
import statistics
import time
from functools import lru_cache
from pathlib import Path

import pytest
from solders.keypair import Keypair
from solders.signature import Signature

from unmask.graph.model import HubCriterion
from unmask.graph.service import GraphService
from unmask.hubs.config import HubConfig, HubThresholds, load_hub_config
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

SC_007_BUDGET_SECONDS = 2.0  # SC-007; поріг не послаблюється «під CI» (R-17)
RUNS = 3  # прогонів на вимір бюджету SC-007; береться медіана
RATIO_RUNS = 3  # прогонів на вимір лінійності; береться мінімум (див. `_min_seconds`)
LINEAR_RATIO_LIMIT = 3.0  # 2× вхід -> < 3× часу; лінійне дає ≈ 2.0-2.4 (кеші, купа), квадратичне ≈ 4.5

SOL = 10**9
BASE_TIME = 1_759_400_000
SEED = 20261004
INGEST_THRESHOLD = 200  # `metadata.counterparty_threshold`: хаб позначено `high_degree` з 201 контрагентом

PRIVATE_FUNDERS_PER_BUYER = 7
PRIVATE_SENDERS_PER_FUNDER = 6


# --- Детермінований генератор -----------------------------------------------------------


def _address(label: str) -> str:
    seed = hashlib.sha256(f"unmask-perf/wallet/{label}".encode()).digest()
    return str(Keypair.from_seed(seed).pubkey())  # on-curve: жодного `off_curve` серед джерел


def _signature(index: int) -> str:
    return str(Signature.from_bytes(hashlib.sha512(f"unmask-perf/tx/{index}".encode()).digest()))


@lru_cache(maxsize=None)
def synthetic(n_buyers: int) -> tuple:
    """`(IngestResult, hub, dust, buyers)` для `n_buyers` покупців. Результат незмінний, тож кешується між тестами."""
    rng = random.Random(SEED)
    hub_senders, hub_buyers, dust_buyers = n_buyers * 2 // 3, n_buyers * 5 // 6, n_buyers * 5 // 6
    buyers = [_address(f"B{i}") for i in range(n_buyers)]
    hub, dust = _address("HUB"), _address("DUST")
    transfers: list[dict] = []
    counter = [0]

    def transfer(sender: str, receiver: str, amount: int, depth: int) -> None:
        counter[0] += 1
        slot = counter[0]
        transfers.append({
            "signature": _signature(counter[0]), "slot": slot, "block_time": BASE_TIME + slot,
            "instruction_path": "0", "sender": sender, "receiver": receiver, "asset": "sol",
            "amount": amount, "decimals": None, "depth": depth,
        })

    for i, buyer in enumerate(buyers):
        for k in range(PRIVATE_FUNDERS_PER_BUYER):
            funder = _address(f"F{i}_{k}")
            for _ in range(1 + k % 2):  # 1 або 2 перекази: частина ребер має count > 1
                transfer(funder, buyer, rng.randrange(SOL // 10, 3 * SOL), 1)
            for j in range(PRIVATE_SENDERS_PER_FUNDER):
                for _ in range(1 + (j % 3 == 0)):
                    transfer(_address(f"S{i}_{k}_{j}"), funder, rng.randrange(SOL // 10, 3 * SOL), 2)
    for i in range(hub_buyers):
        transfer(hub, buyers[i], SOL // 2, 1)
    for j in range(hub_senders):
        transfer(_address(f"HS{j}"), hub, SOL, 2)  # рівно один переказ від кожного: одноразові відправники
    for i in range(n_buyers - dust_buyers, n_buyers):
        transfer(dust, buyers[i], 500_000, 1)
    for j in range(2):
        transfer(_address(f"DS{j}"), dust, SOL, 2)

    def path_key(row: dict) -> tuple:
        return row["slot"], row["signature"], tuple(int(p) for p in row["instruction_path"].split("."))

    transfers.sort(key=path_key)
    buyer_rows = []
    for i, buyer in enumerate(buyers):
        slot = 1_000_000 + 10 * i
        buyer_rows.append({
            "wallet": buyer, "rank": i + 1, "first_buy_signature": _signature(10_000_000 + i), "first_buy_slot": slot,
            "first_buy_time": BASE_TIME + slot, "received_amount": SOL + i, "spent": [{"asset": "sol", "amount": SOL // 2}],
            "programs": [_address("DEX")], "address_type": "wallet",
        })
    document = {
        "metadata": {
            "mint": _address("MINT"), "analyzed_at": BASE_TIME + 100_000, "wallets_analyzed": n_buyers,
            "config_version": 2, "first_buyers_n": n_buyers, "funding_depth": 3,
            "counterparty_threshold": INGEST_THRESHOLD, "max_signatures_per_wallet": 300,
            "collect_spl_inbound": True, "time_budget_seconds": 40.0, "elapsed_seconds": 1.5,
            "rpc_calls": 10 + len(transfers), "transactions_scanned": len(transfers),
            "source": "synthetic:performance", "resumed": False, "served_from_cache": False,
        },
        "completeness": {"status": "complete", "missing": [], "buyers": {"complete": True, "reason": None, "detail": ""}},
        "buyers": buyer_rows,
        "transfers": transfers,
        "unexpanded": [{"wallet": hub, "depth": 1, "reason": "high_degree", "counterparties_seen": INGEST_THRESHOLD + 1,
                        "signatures_seen": 300, "signatures_truncated": False}],
        "delegated": {"links": [], "unpaired": [], "complete": True, "reason": None, "detail": ""},
    }
    return from_dict(document), hub, dust, tuple(buyers)


# --- Вимірювання -----------------------------------------------------------------------


def _shipped() -> HubConfig:
    return load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)


def _dust_only_config() -> HubConfig:
    """Пороги `hubs.yaml` v2, але ступінь і збірна позначка не можуть відсікти: лишається лише `dust_fanout`."""
    base = _shipped().thresholds
    thresholds = HubThresholds(
        version=base.version, degree_threshold=10**6, one_off_senders_share=base.one_off_senders_share,
        one_off_min_senders=base.one_off_min_senders, giant_component_warn_share=base.giant_component_warn_share,
        prune_off_curve=base.prune_off_curve, prune_ingest_high_degree=False,
        dust_amount_lamports=base.dust_amount_lamports, dust_min_fanout=base.dust_min_fanout,
    )
    return HubConfig(thresholds=thresholds, lists=None, thresholds_digest="0" * 64, lists_digest=None)


def _median_seconds(action, runs: int = RUNS) -> float:
    """Медіана часу `action()` за `runs` прогонів; `gc.collect()` перед кожним — однаковий початковий стан купи."""
    samples = []
    for _ in range(runs):
        gc.collect()
        started = time.perf_counter()
        action()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def _min_seconds(action, runs: int = RATIO_RUNS) -> float:
    """Мінімум часу `action()`: шум планувальника лише додає час, тож мінімум найближчий до справжньої вартості.

    Для відношення двох вимірів (лінійність) мінімум стійкіший за медіану: на завантаженій машині медіана 300 -> 600
    ловила викиди до 3.1× при справжніх ≈ 2.3×, мінімум трималась у 2.2-2.35×.
    """
    samples = []
    for _ in range(runs):
        gc.collect()
        started = time.perf_counter()
        action()
        samples.append(time.perf_counter() - started)
    return min(samples)


# --- Тести -----------------------------------------------------------------------------


@pytest.mark.perf
def test_300_buyers_typical_result_analyzed_and_serialized_under_two_seconds():
    result, *_ = synthetic(300)
    assert 19_000 <= len(result.transfers) <= 22_000, "синтетичний вхід має бути «~20 000 переказів»"
    assert {t.depth for t in result.transfers} == {1, 2}
    service = GraphService(_shipped())
    service.analyze(result)  # прогрів: імпорти, кеші інтерпретатора; не входить у вимір

    analyze_seconds = _median_seconds(lambda: service.analyze(result))
    assert analyze_seconds < SC_007_BUDGET_SECONDS, f"analyze: {analyze_seconds:.3f} s >= {SC_007_BUDGET_SECONDS} s"

    # `to_json` — T-040 (graph/serialize.py). Поки модуля немає, повна перевірка пропускається (analyze вже
    # перевірено вище); коли він зʼявиться, SC-007 міряється разом із серіалізацією.
    serialize = pytest.importorskip("unmask.graph.serialize")
    graph_result = service.analyze(result)
    serialize.to_json(graph_result)  # прогрів
    total_seconds = _median_seconds(lambda: serialize.to_json(service.analyze(result)))
    assert total_seconds < SC_007_BUDGET_SECONDS, f"analyze + to_json: {total_seconds:.3f} s >= {SC_007_BUDGET_SECONDS} s"


@pytest.mark.perf
def test_runtime_grows_roughly_linearly_with_edges():
    small, large = synthetic(300)[0], synthetic(600)[0]
    service = GraphService(_shipped())
    edges_small, edges_large = service.analyze(small).metadata.edges_total, service.analyze(large).metadata.edges_total
    assert 1.9 <= edges_large / edges_small <= 2.1, "вхід має подвоюватись за ребрами"  # аналіз вище = прогрів

    time_small = _min_seconds(lambda: service.analyze(small))
    time_large = _min_seconds(lambda: service.analyze(large))
    ratio = time_large / time_small
    # Лінійна (чи O(E log E)) складність дає ≈ 2×; квадратична — ≈ 4×. Поріг 3× розділяє їх із запасом.
    assert ratio < LINEAR_RATIO_LIMIT, (
        f"2× ребер ({edges_small} -> {edges_large}) дали {ratio:.2f}× часу ({time_small:.3f} s -> {time_large:.3f} s): "
        "схоже на квадратичну регресію")


def test_synthetic_hub_is_pruned_and_share_drops():
    result, hub, _dust, buyers = synthetic(300)
    out = GraphService(_shipped()).analyze(result)

    record = next(r for r in out.pruned if r.address == hub)
    hits = {h.criterion for h in record.criteria}
    assert {HubCriterion.ONE_OFF_SENDERS, HubCriterion.INGEST_HIGH_DEGREE} <= hits
    # степінь — за порогом із shipped-конфігу, а не за літералом (безпечна зміна hubs.yaml не ламає тест)
    assert (HubCriterion.DEGREE in hits) == (record.measures.degree > _shipped().thresholds.degree_threshold)
    assert record.measures.unique_senders == 200 and record.measures.one_off_senders == 200
    assert record.measures.degree == 200 + 250
    assert hub not in {n.address for n in out.graph.nodes}
    assert {n.address for n in out.graph.nodes} >= set(buyers), "покупці лишаються завжди (FR-002-10)"

    before, after = out.report.before, out.report.after
    assert before.buyers_total == after.buyers_total == 300
    assert before.largest_component_buyer_share == pytest.approx(1.0)  # хаб і пил разом склеюють усіх 300
    assert after.largest_component_buyer_share == pytest.approx(1 / 300)  # приватні джерела не склеюють нікого
    assert after.isolated_buyers == 0  # кожен покупець має приватних фінансистів
    assert out.report.pruned_nodes == len(out.pruned)


def test_synthetic_dust_source_is_pruned_by_dust_fanout():
    result, hub, dust, _buyers = synthetic(300)

    shipped = GraphService(_shipped()).analyze(result)
    dust_record = next(r for r in shipped.pruned if r.address == dust)
    hit = next(h for h in dust_record.criteria if h.criterion is HubCriterion.DUST_FANOUT)
    assert (hit.measured, hit.threshold) == (500_000, _shipped().thresholds.dust_amount_lamports)
    assert dust_record.measures.buyer_fanout == 250 and dust_record.measures.median_to_buyers == 500_000

    # Окремо від ступеня (250 + 2 > 100 відсікло б і так) і від збірної позначки: пилове джерело відсікає лише
    # `dust_fanout`. Хаб лишається відсіченим за одноразовими відправниками — це єдине інше відсікання.
    isolated = GraphService(_dust_only_config()).analyze(result)
    by_address = {r.address: [h.criterion for h in r.criteria] for r in isolated.pruned}
    assert by_address == {dust: [HubCriterion.DUST_FANOUT], hub: [HubCriterion.ONE_OFF_SENDERS]}
    assert isolated.report.after.largest_component_buyer_share < isolated.report.before.largest_component_buyer_share
