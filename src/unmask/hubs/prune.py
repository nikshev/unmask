# impl: FR-002-07, FR-002-09, FR-002-10, FR-002-12
"""Відсікання хабів із захистом покупців (принцип VI; contracts/graph-service.md §5; data-model «PruneRecord»,
«BuyerFlag», «PruneOutcome»; research R-16, R-22).

`prune_hubs(graph, config, *, ingest_counterparty_threshold) -> PruneOutcome` — чиста функція: для кожної вершини
`criteria.evaluate` (FR-002-07; усі критерії, незалежно від ролі); далі:

- не покупець із ≥ 1 хітом → `PruneRecord`: адреса, усі хіти (з виміряними значеннями й порогами), **усі**
  інцидентні ребра (на будь-якому кінці, обох видів — `transfer` і `delegated_buy`), виміри вершини на момент
  рішення, версія порогів і версія списків (FR-002-09). Ребро між двома хабами — в обох записах, тож повний граф
  відтворюється як `outcome.graph ∪ records`;
- покупець із ≥ 1 хітом → `BuyerFlag` (FR-002-10): вершина й усі її ребра до не-хабів лишаються; покупця не
  відсікає жоден критерій, хоч би скільки їх спрацювало;
- `outcome.graph = graph.without(адреси записів)` — без хабів і всіх інцидентних їм ребер; виміри решти вершин
  не перераховуються (data-model `FundingGraph.without`), тож повторне відсікання нічого не додає;
- `lists_applied = config.lists is not None`, `lists_version = config.lists.version` або `None` (FR-002-12):
  без списків критерій `known_list` за списком не спрацьовує, і це видно в результаті, а не виглядає як «хабів
  немає» (попередження — у звіті ефекту, T-036/T-037).

Порядок (детермінізм, FR-002-04): записи — за адресою, позначки — за `buyer_rank` (нічия рангів — за адресою);
інцидентні ребра — у канонічному порядку графа (`edge_sort_key`). Вхідний граф не змінюється (усі типи frozen).

Типи перевіряють себе при побудові (стиль `graph.model`): запис без хітів (SC-002), невпорядковані хіти, чуже
ребро, покупець у записах чи хаб у графі результату — гучно (`TypeError`/`ValueError`), а не тихо.

Залежності: `graph.model`, `hubs.criteria`, `hubs.config` (plan «Правило залежностей»); жодного `ingest`-коду,
мережі, файлів чи годинника.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from unmask.graph.model import Edge, FundingGraph, HubCriterion, NodeMeasures, NodeRole, edge_sort_key
from unmask.hubs.config import HubConfig
from unmask.hubs.criteria import CriterionHit, evaluate

__all__ = ["BuyerFlag", "PruneOutcome", "PruneRecord", "prune_hubs"]

_LIST_PREFIX = "list:"


# --- Перевірки -------------------------------------------------------------------


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _int(name: str, value: Any, *, lo: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: expected int, got {type(value).__name__}")
    if value < lo:
        raise ValueError(f"{name}: {value} < {lo}")


def _opt_int(name: str, value: Any, *, lo: int) -> None:
    if value is not None:
        _int(name, value, lo=lo)


def _address(name: str, value: Any) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name}: expected str, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{name}: must not be empty")


def _tuple_of(obj: object, name: str, cls: type) -> tuple:
    value = getattr(obj, name)
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{name}: expected a collection, got {type(value).__name__}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, cls):
            raise TypeError(f"{name}: expected {cls.__name__}, got {type(item).__name__}")
    _set(obj, name, items)
    return items


def _hit_key(hit: CriterionHit) -> tuple[str, str]:
    # Той самий порядок, що й у `criteria.evaluate` і в оракулі фікстур: (criterion, detail) як рядки.
    return hit.criterion.value, hit.detail


def _is_list_hit(hit: CriterionHit) -> bool:
    return hit.criterion is HubCriterion.KNOWN_LIST and hit.detail.startswith(_LIST_PREFIX)


def _criteria(obj: object, name: str) -> tuple[CriterionHit, ...]:
    """Непорожні (SC-002), строго впорядковані за `(criterion, detail)` хіти — отже, без дублів (≤ 6, R-22)."""
    hits = _tuple_of(obj, name, CriterionHit)
    if not hits:
        raise ValueError(f"{name}: must not be empty (no pruning or flag without an explanation, SC-002)")
    keys = [_hit_key(h) for h in hits]
    if any(a >= b for a, b in zip(keys, keys[1:])):
        raise ValueError(f"{name}: hits must be strictly ordered by (criterion, detail) without duplicates")
    if sum(_is_list_hit(h) for h in hits) > 1:
        raise ValueError(f"{name}: an address belongs to at most one address list category")
    return hits


# --- Типи ------------------------------------------------------------------------


@dataclass(frozen=True)
class PruneRecord:
    """Запис відсікання (FR-002-09): хаб і все, що з ним прибрано з графа кластеризації."""

    address: str
    criteria: tuple[CriterionHit, ...]
    incident_edges: tuple[Edge, ...]
    measures: NodeMeasures
    config_version: int
    lists_version: int | None

    def __post_init__(self) -> None:
        _address("prune_record.address", self.address)
        hits = _criteria(self, "criteria")
        edges = _tuple_of(self, "incident_edges", Edge)
        for e in edges:
            if self.address not in (e.sender, e.receiver):
                raise ValueError(f"prune_record.incident_edges: edge {e.sender} -> {e.receiver} "
                                 f"is not incident to {self.address}")
        keys = [edge_sort_key(e) for e in edges]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise ValueError("prune_record.incident_edges: must be in canonical edge order without duplicates")
        if not isinstance(self.measures, NodeMeasures):
            raise TypeError(f"prune_record.measures: expected NodeMeasures, got {type(self.measures).__name__}")
        _int("prune_record.config_version", self.config_version, lo=1)
        _opt_int("prune_record.lists_version", self.lists_version, lo=1)
        for hit in hits:
            if _is_list_hit(hit) and hit.lists_version != self.lists_version:
                raise ValueError(f"prune_record: list hit lists_version={hit.lists_version!r} != "
                                 f"record lists_version={self.lists_version!r}")


@dataclass(frozen=True)
class BuyerFlag:
    """Пояснювальна позначка покупця, що відповідає критерію хаба (FR-002-10); покупець лишається в графі."""

    address: str
    buyer_rank: int
    criteria: tuple[CriterionHit, ...]
    measures: NodeMeasures

    def __post_init__(self) -> None:
        _address("buyer_flag.address", self.address)
        _int("buyer_flag.buyer_rank", self.buyer_rank, lo=1)
        _criteria(self, "criteria")
        if not isinstance(self.measures, NodeMeasures):
            raise TypeError(f"buyer_flag.measures: expected NodeMeasures, got {type(self.measures).__name__}")


def _flag_order(flag: BuyerFlag) -> tuple[int, str]:
    return flag.buyer_rank, flag.address


@dataclass(frozen=True)
class PruneOutcome:
    """Результат `prune_hubs`: граф для кластеризації, записи відсікання, позначки покупців, версії."""

    graph: FundingGraph
    records: tuple[PruneRecord, ...]
    buyer_flags: tuple[BuyerFlag, ...]
    lists_applied: bool
    config_version: int
    lists_version: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.graph, FundingGraph):
            raise TypeError(f"prune_outcome.graph: expected FundingGraph, got {type(self.graph).__name__}")
        records = _tuple_of(self, "records", PruneRecord)
        flags = _tuple_of(self, "buyer_flags", BuyerFlag)
        if not isinstance(self.lists_applied, bool):
            raise TypeError(f"prune_outcome.lists_applied: expected bool, got {type(self.lists_applied).__name__}")
        _int("prune_outcome.config_version", self.config_version, lo=1)
        _opt_int("prune_outcome.lists_version", self.lists_version, lo=1)
        if self.lists_applied != (self.lists_version is not None):
            raise ValueError("prune_outcome: lists_applied iff lists_version is not None")

        addresses = [r.address for r in records]
        if any(a >= b for a, b in zip(addresses, addresses[1:])):
            raise ValueError("prune_outcome.records: must be strictly ordered by address")
        nodes = {n.address for n in self.graph.nodes}
        still = sorted(set(addresses) & nodes)
        if still:
            raise ValueError(f"prune_outcome: pruned addresses still in graph: {still}")
        for r in records:
            if (r.config_version, r.lists_version) != (self.config_version, self.lists_version):
                raise ValueError(f"prune_outcome: record {r.address} versions ({r.config_version}, "
                                 f"{r.lists_version}) != outcome ({self.config_version}, {self.lists_version})")

        order = [_flag_order(f) for f in flags]
        if any(a >= b for a, b in zip(order, order[1:])):
            raise ValueError("prune_outcome.buyer_flags: must be strictly ordered by buyer_rank")
        for f in flags:
            if f.address not in nodes:
                raise ValueError(f"prune_outcome: flagged buyer {f.address} is not in the graph (FR-002-10)")
            node = self.graph.node(f.address)
            if NodeRole.BUYER not in node.roles or node.buyer_rank != f.buyer_rank:
                raise ValueError(f"prune_outcome: flag {f.address} rank {f.buyer_rank} does not match a buyer node")
            for hit in f.criteria:
                if _is_list_hit(hit) and hit.lists_version != self.lists_version:
                    raise ValueError(f"prune_outcome: flag {f.address} list hit lists_version != outcome")

        if not self.lists_applied:
            hits = [h for item in (*records, *flags) for h in item.criteria]
            if any(_is_list_hit(h) for h in hits):
                raise ValueError("prune_outcome: lists not applied but a list:* hit is present (FR-002-12)")


# --- Відсікання ------------------------------------------------------------------


def prune_hubs(graph: FundingGraph, config: HubConfig, *, ingest_counterparty_threshold: int) -> PruneOutcome:
    """Відсікти хаби з `graph` за `config`; покупці лишаються з позначками. Чиста функція."""
    if not isinstance(graph, FundingGraph):
        raise TypeError(f"prune_hubs: expected FundingGraph, got {type(graph).__name__}")
    if not isinstance(config, HubConfig):
        raise TypeError(f"prune_hubs: expected HubConfig, got {type(config).__name__}")
    # Перевірка тут, а не лише в `evaluate`: на порожньому графі `evaluate` не викликається, а некоректний поріг
    # збору — дефект виклику незалежно від вмісту графа.
    if isinstance(ingest_counterparty_threshold, bool) or not isinstance(ingest_counterparty_threshold, int):
        raise TypeError("prune_hubs: ingest_counterparty_threshold: expected int, "
                        f"got {type(ingest_counterparty_threshold).__name__}")
    if ingest_counterparty_threshold < 1:
        raise ValueError("prune_hubs: ingest_counterparty_threshold must be >= 1")

    config_version = config.thresholds.version
    lists_version = None if config.lists is None else config.lists.version

    hub_hits: dict[str, tuple[CriterionHit, ...]] = {}
    flags: list[BuyerFlag] = []
    for node in graph.nodes:  # канонічний порядок — за адресою
        hits = evaluate(node, config, ingest_counterparty_threshold=ingest_counterparty_threshold)
        if not hits:
            continue
        if NodeRole.BUYER in node.roles:  # FR-002-10: покупця не відсікає жоден критерій
            flags.append(BuyerFlag(node.address, node.buyer_rank, hits, node.measures))
        else:
            hub_hits[node.address] = hits

    # Інцидентні ребра — одним проходом (O(E), а не O(E) на кожен хаб); ребра графа вже в канонічному порядку,
    # тож порядок у кожному записі — теж канонічний. Ребро між двома хабами потрапляє в обидва записи.
    incident: dict[str, list[Edge]] = {address: [] for address in hub_hits}
    for edge in graph.edges:
        if edge.sender in incident:
            incident[edge.sender].append(edge)
        if edge.receiver in incident:
            incident[edge.receiver].append(edge)

    records = tuple(
        PruneRecord(address, hits, tuple(incident[address]), graph.node(address).measures,
                    config_version, lists_version)
        for address, hits in hub_hits.items()  # вставлено в порядку адрес
    )
    return PruneOutcome(
        graph=graph.without(hub_hits),
        records=records,
        buyer_flags=tuple(sorted(flags, key=_flag_order)),
        lists_applied=config.lists is not None,
        config_version=config_version,
        lists_version=lists_version,
    )
