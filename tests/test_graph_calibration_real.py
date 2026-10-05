# verifies: FR-002-07, FR-002-08
"""Перерахунок калібрування R-23 на виміряних вершинах реальних токенів (T-058; FR-002-07, FR-002-08; research R-23;
specs/002-funding-graph-hub-pruning/calibration.md, розділ «Перерахунок R-23 (2026-10-05)»).

Знахідка R-23: на калібрувальних зборах (`max_signatures_per_wallet = 30`) видно не більше ~30 відправників вершини,
і критерій `one_off_senders` з передумовою v2 (`one_off_min_senders = 10`) відсікав вузли з 15–30 відправниками — на
9 токенах шість вершин у п'яти токенах, серед них головного інсайдерського фінансиста ins1 `DKrPigau…` (fan-out 15
покупців по 2,4 SOL). `config/hubs.yaml` v3 піднімає передумову до 50; решта значень v2 без змін.

Тут — синтетичні вершини з РІВНО ВИМІРЯНИМИ значеннями тих шести вершин (лише числа з `calibration.md`; сирих даних
реальних токенів у репозиторії немає, адреси — синтетичні ключі). Для кожної перевіряється, що:

- з комітованим конфігом (v3) жодна не відсікається за `one_off_senders`; `cln4 Gua5EchG1A` відсікається лише
  `dust_fanout` (медіана 2 лампорти при fan-out 6); ins3 `4UqZhyrQgB` (теж пил за медіаною, але fan-out 3 < 5) на v3
  лишається в графі — ціна рішення, див. calibration.md, висновок 3;
- з тим самим конфігом, але `one_off_min_senders = 10` (і `version = 2` — рівно історична v2), кожна відсікається за
  `one_off_senders` з виміряною часткою — тобто різницю дає саме передумова, а не інші значення.

Для ins1 те саме перевірено наскрізно: `GraphService.analyze` на невеликому синтетичному результаті збору, побудова
якого відтворює виміри ins1 (ступінь 45, 30 відправників, 29 одноразових, fan-out 15, верхня медіана 2,4 SOL, без
позначки `unexpanded`) — виміри рахує код графа, а тест звіряє їх із каліброваними. Мережі немає.
"""

import dataclasses
import hashlib
from fractions import Fraction
from pathlib import Path

import pytest
from solders.keypair import Keypair
from solders.signature import Signature

from unmask.graph.model import GraphWarning, HubCriterion, Node, NodeMeasures, NodeRole, UnexpandedMark
from unmask.graph.service import GraphService
from unmask.hubs.config import HubConfig, load_hub_config
from unmask.hubs.criteria import evaluate
from unmask.ingest.model import AddressType, UnexpandedReason
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

SOL = 10**9
CALIBRATION_CAP = 30  # max_signatures_per_wallet калібрувальних зборів (calibration.md)
INGEST_THRESHOLD = 200  # counterparty_threshold калібрувальних зборів
ONE_OFF = HubCriterion.ONE_OFF_SENDERS
DUST = HubCriterion.DUST_FANOUT


def _address(label: str) -> str:
    seed = hashlib.sha256(f"unmask-calibration-r23/wallet/{label}".encode()).digest()
    return str(Keypair.from_seed(seed).pubkey())  # синтетичний ключ на кривій, не адреса реального гаманця


def _signature(index: int) -> str:
    return str(Signature.from_bytes(hashlib.sha512(f"unmask-calibration-r23/tx/{index}".encode()).digest()))


# Виміри шести вершин із `one_off_senders`-хітом на v2 (calibration.md, «Перерахунок R-23»; r23_oneoff.py).
# (мітка, ступінь, унікальних відправників, одноразових, fan-out у покупців, верхня медіана SOL до покупців у
#  лампортах, позначка збору: None або counterparties_seen для `signature_cap` (signatures_seen = 30, обрізано)).
MEASURED = {
    "ins1_DKrPigau": (45, 30, 29, 15, 2_400_000_000, None),
    "ins3_4UqZhyrQgB": (34, 28, 28, 3, 2, 28),
    "cln1_HsLS4KNT2F": (28, 27, 24, 1, 43_467_845, 27),
    "cln2_9yMwSPk9mr": (27, 26, 26, 1, 10_398_816, 26),
    "cln4_BUsHXQcabN": (16, 15, 13, 1, 50_000_000_000, 15),
    "cln4_Gua5EchG1A": (40, 27, 26, 6, 2, 27),
}
# Очікувані критерії з комітованим конфігом (v3): лише пил у Gua5EchG1A.
EXPECTED_V3 = {label: [] for label in MEASURED} | {"cln4_Gua5EchG1A": [DUST]}


def _shipped() -> HubConfig:
    return load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)


def _with_v2_precondition(config: HubConfig) -> HubConfig:
    """Комітований конфіг із поверненою передумовою v2 (`one_off_min_senders = 10`, `version = 2`)."""
    thresholds = dataclasses.replace(config.thresholds, version=2, one_off_min_senders=10)
    return dataclasses.replace(config, thresholds=thresholds)


def _node(label: str) -> Node:
    degree, unique, one_off, fanout, median, capped = MEASURED[label]
    mark = None if capped is None else UnexpandedMark(
        reason=UnexpandedReason.SIGNATURE_CAP, counterparties_seen=capped,
        signatures_seen=CALIBRATION_CAP, signatures_truncated=True)
    return Node(
        address=_address(label), roles=frozenset({NodeRole.FUNDER}), depth=1, buyer_rank=None,
        address_type=AddressType.WALLET, unexpanded=mark,
        measures=NodeMeasures(degree=degree, unique_senders=unique, one_off_senders=one_off,
                              one_off_share=one_off / unique, buyer_fanout=fanout, median_to_buyers=median),
    )


def _hits(node: Node, config: HubConfig):
    return evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD)


# --- Синтетичний результат збору, що відтворює виміри ins1 ------------------------------------


INS1_BUYERS = 15  # покупців, яким ins1 надіслав SOL
OTHER_BUYERS = 5  # покупці з приватними фінансистами (не склеєні з ins1)
INS1_SENDERS = 30  # унікальних відправників ins1; один із них надіслав двічі → 29 одноразових
INS1_AMOUNT = 2_400_000_000  # 2,4 SOL кожному покупцеві: 15 × 2,4 = 36 SOL, верхня медіана 2,4 SOL


def _ins1_result():
    """`(IngestResult, адреса ins1, покупці)`: 20 покупців, 15 профінансовано ins1, 5 — приватними фінансистами."""
    ins1 = _address("INS1")
    buyers = [_address(f"B{i}") for i in range(INS1_BUYERS + OTHER_BUYERS)]
    transfers: list[dict] = []

    def transfer(sender: str, receiver: str, amount: int, depth: int) -> None:
        index = len(transfers) + 1
        transfers.append({
            "signature": _signature(index), "slot": index, "block_time": 1_759_400_000 + index,
            "instruction_path": "0", "sender": sender, "receiver": receiver, "asset": "sol",
            "amount": amount, "decimals": None, "depth": depth,
        })

    for buyer in buyers[:INS1_BUYERS]:
        transfer(ins1, buyer, INS1_AMOUNT, 1)
    for i, buyer in enumerate(buyers[INS1_BUYERS:]):
        transfer(_address(f"F{i}"), buyer, 2 * SOL, 1)
    for j in range(INS1_SENDERS):
        for _ in range(2 if j == 0 else 1):  # S0 — двічі: 29 одноразових із 30
            transfer(_address(f"S{j}"), ins1, 3 * SOL, 2)
    buyer_rows = [{
        "wallet": buyer, "rank": i + 1, "first_buy_signature": _signature(1_000_000 + i),
        "first_buy_slot": 100_000 + i, "first_buy_time": 1_759_500_000 + i, "received_amount": SOL + i,
        "spent": [{"asset": "sol", "amount": SOL}], "programs": [_address("DEX")], "address_type": "wallet",
    } for i, buyer in enumerate(buyers)]
    document = {
        "metadata": {
            "mint": _address("MINT"), "analyzed_at": 1_759_600_000, "wallets_analyzed": len(buyers),
            "config_version": 2, "first_buyers_n": len(buyers), "funding_depth": 2,
            "counterparty_threshold": INGEST_THRESHOLD, "max_signatures_per_wallet": CALIBRATION_CAP,
            "collect_spl_inbound": True, "time_budget_seconds": 40.0, "elapsed_seconds": 1.0,
            "rpc_calls": 1 + len(transfers), "transactions_scanned": len(transfers),
            "source": "synthetic:calibration-r23", "resumed": False, "served_from_cache": False,
        },
        "completeness": {"status": "complete", "missing": [], "buyers": {"complete": True, "reason": None, "detail": ""}},
        "buyers": buyer_rows,
        "transfers": transfers,
        "unexpanded": [],
        "delegated": {"links": [], "unpaired": [], "complete": True, "reason": None, "detail": ""},
    }
    return from_dict(document), ins1, buyers


# --- Тести ------------------------------------------------------------------------------------


def test_committed_config_is_v3_with_precondition_50_and_v2_values_otherwise():
    t = _shipped().thresholds
    assert (t.version, t.one_off_min_senders) == (3, 50)
    v2 = _with_v2_precondition(_shipped()).thresholds
    assert dataclasses.replace(v2, version=3, one_off_min_senders=50) == t  # інших відмінностей немає
    # Передумова недосяжна при капі збору 30: видимих відправників не більше ~30 < 50 (known-issues).
    assert max(unique for _, unique, *_ in MEASURED.values()) <= CALIBRATION_CAP < t.one_off_min_senders


def test_real_ins1_funder_is_not_pruned_by_one_off_senders():
    """ins1 `DKrPigau…`: 30 відправників, 29 одноразових (0.9667), fan-out 15, медіана 2,4 SOL, без позначки.

    З комітованим конфігом — не хаб (жодного хіта); з `one_off_min_senders = 10` — хаб за `one_off_senders` з
    виміряною часткою 29/30. Отже саме поріг-передумова вирішує долю головного інсайдерського фінансиста.
    """
    node = _node("ins1_DKrPigau")
    assert node.unexpanded is None
    m = node.measures
    assert (m.degree, m.unique_senders, m.one_off_senders, m.buyer_fanout, m.median_to_buyers) == (
        45, 30, 29, 15, 2_400_000_000)
    assert m.one_off_share == pytest.approx(0.9667, abs=5e-5)

    shipped = _shipped()
    assert _hits(node, shipped) == ()

    v2_hits = _hits(node, _with_v2_precondition(shipped))
    assert [(h.criterion, h.measured, h.threshold, h.detail) for h in v2_hits] == [
        (ONE_OFF, 29 / 30, 0.8, "measured")]

    # Межа передумови — сама кількість відправників ins1: 30 включно ще відсікає, 31 — уже ні.
    def with_min(n):
        return dataclasses.replace(shipped, thresholds=dataclasses.replace(shipped.thresholds, one_off_min_senders=n))

    assert [h.criterion for h in _hits(node, with_min(30))] == [ONE_OFF]
    assert _hits(node, with_min(31)) == ()


def test_real_ins1_end_to_end_analyze_keeps_funder_and_its_buyer_component():
    """Наскрізно через `GraphService.analyze`: виміри ins1 рахує код графа; v3 лишає ins1 і його 15 покупців разом
    (попередження `giant_component` — сигнал, а не шум), історична передумова 10 відсікла б ins1 і розсипала кластер."""
    result, ins1, buyers = _ins1_result()

    v3 = GraphService(_shipped()).analyze(result)
    node = next(n for n in v3.graph.nodes if n.address == ins1)
    m = node.measures
    # побудова відтворила калібровані виміри ins1
    assert (m.degree, m.unique_senders, m.one_off_senders, m.buyer_fanout, m.median_to_buyers) == (
        45, 30, 29, 15, 2_400_000_000)
    assert Fraction(m.one_off_share).limit_denominator(100) == Fraction(29, 30)
    assert node.unexpanded is None
    assert v3.pruned == () and v3.buyer_flags == ()
    assert v3.metadata.hub_config_version == 3 and v3.metadata.thresholds.one_off_min_senders == 50
    assert v3.report.after.buyers_in_largest_component == INS1_BUYERS
    assert v3.report.after.largest_component_buyer_share == pytest.approx(INS1_BUYERS / len(buyers))
    assert GraphWarning.GIANT_COMPONENT in v3.report.warnings

    v2 = GraphService(_with_v2_precondition(_shipped())).analyze(result)
    (record,) = v2.pruned
    assert record.address == ins1 and record.config_version == 2
    assert [(h.criterion, h.measured, h.threshold) for h in record.criteria] == [(ONE_OFF, 29 / 30, 0.8)]
    assert ins1 not in {n.address for n in v2.graph.nodes}
    assert v2.report.after.buyers_in_largest_component == 1  # кластер ins1 зник разом із фінансистом
    assert GraphWarning.GIANT_COMPONENT not in v2.report.warnings


@pytest.mark.parametrize("label", list(MEASURED))
def test_measured_calibration_nodes_are_not_pruned_by_one_off_senders_on_v3(label):
    node = _node(label)
    shipped = _shipped()
    v3 = _hits(node, shipped)
    assert ONE_OFF not in [h.criterion for h in v3], label
    assert [h.criterion for h in v3] == EXPECTED_V3[label], label
    # З передумовою v2 кожна з шести відсікалась за `one_off_senders` з виміряною часткою (R-23).
    v2 = _hits(node, _with_v2_precondition(shipped))
    one_off = [h for h in v2 if h.criterion is ONE_OFF]
    assert [(h.measured, h.threshold) for h in one_off] == [(node.measures.one_off_share, 0.8)], label
    # інші хіти від передумови не залежать
    assert [h for h in v2 if h.criterion is not ONE_OFF] == list(v3), label


def test_ins3_dust_like_median_with_fanout_3_is_below_dust_precondition():
    """ins3 `4UqZhyrQgB`: медіана 2 лампорти, але fan-out 3 < `dust_min_fanout` 5 — не `dust_fanout`; на v3 — не хаб."""
    node = _node("ins3_4UqZhyrQgB")
    t = _shipped().thresholds
    assert node.measures.median_to_buyers < t.dust_amount_lamports
    assert node.measures.buyer_fanout == 3 < t.dust_min_fanout
    assert node.measures.one_off_share == 1.0 and node.measures.unique_senders == 28
    assert _hits(node, _shipped()) == ()


def test_cln4_gua5_is_pruned_only_by_dust_fanout_on_v3():
    """cln4 `Gua5EchG1A`: ступінь 40, 27 відправників (26 одноразових), fan-out 6, медіана 2 лампорти — пил.

    На v3 відсікається рівно одним критерієм `dust_fanout` (measured 2 < 1 000 000); `one_off_senders` для цього не
    потрібен. Це НЕ означає, що підняття передумови пиловий захист не послаблює: ins3 `4UqZhyrQgB` (fan-out 3 < 5)
    на v3 лишається в графі. Позначка `signature_cap` критерієм не є.
    """
    node = _node("cln4_Gua5EchG1A")
    assert node.unexpanded is not None and node.unexpanded.reason is UnexpandedReason.SIGNATURE_CAP
    hits = _hits(node, _shipped())
    assert [(h.criterion, h.measured, h.threshold, h.detail) for h in hits] == [(DUST, 2, 1_000_000, "measured")]
    assert [h.criterion for h in _hits(node, _with_v2_precondition(_shipped()))] == [DUST, ONE_OFF]


def test_signature_cap_mark_alone_does_not_prune_capped_calibration_nodes():
    """П'ять із шести вершин збір позначив `signature_cap` (30 підписів, обрізано) — це атрибут, а не критерій."""
    capped = [label for label, row in MEASURED.items() if row[-1] is not None]
    assert len(capped) == 5
    for label in capped:
        node = _node(label)
        assert node.unexpanded.signatures_seen == CALIBRATION_CAP and node.unexpanded.signatures_truncated
        assert HubCriterion.INGEST_HIGH_DEGREE not in [h.criterion for h in _hits(node, _shipped())], label
