# impl: FR-002-02, FR-002-03, FR-002-05, FR-002-06, FR-002-18, FR-002-22
"""Типи графа фінансування й результату (data-model.md фічі 002) з інваріантами.

Усі сутності незмінні (`frozen=True`) і перевіряють себе при побудові: некоректний
стан падає гучно (TypeError/ValueError), а не потрапляє в граф, звіт чи
кластеризацію. Суми — цілі в базових одиницях (float лише в частках `*_share`).

Ключові інваріанти:
- ребро — доказ (FR-002-02, принцип V): непорожні `refs`, `count == len(refs)`,
  `first_slot`/`last_slot` — рівно з `refs`; самопереказ неможливий;
- два види ребер не зливаються (FR-002-18): ключ `(kind, sender, receiver, asset)`,
  `delegated_buy` не несе ні активу, ні суми, ні шляху інструкції;
- статус повноти графа не зберігається, а обчислюється (FR-002-05): `complete`
  тоді й лише тоді, коли повний збір 001 **і** повний аналіз делегованих купівель.
  `GraphCompleteness` будується лише через `derive` — прийом із токеном, як у 001;
- порядок кожного кортежу визначає ключ лише з даних (`*_sort_key`);
- пилові виміри (FR-002-22, research R-22): `median_to_buyers is None` тоді й лише
  тоді, коли `buyer_fanout == 0`, інакше ціле `>= 1` у лампортах.

Модуль не імпортує `unmask.hubs` (принцип VI, plan «Правило залежностей»):
записи відсікання й звіт у `GraphResult` перевіряються лише за полями, які
потрібні інваріантам результату (`address`, `warnings`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Iterable

from unmask.ingest.model import (
    AddressType,
    Asset,
    CompletenessStatus,
    IngestResult,
    NOT_ANALYZED_REASON,
    MissingReason,
    UnexpandedReason,
)

GRAPH_SCHEMA_VERSION = "002.1"


# --- Перелічення -----------------------------------------------------------------


class NodeRole(StrEnum):
    BUYER = "buyer"
    FUNDER = "funder"
    DELEGATED_PAYER = "delegated_payer"
    DELEGATED_RECEIVER = "delegated_receiver"


class EdgeKind(StrEnum):
    TRANSFER = "transfer"
    DELEGATED_BUY = "delegated_buy"


class HubCriterion(StrEnum):
    KNOWN_LIST = "known_list"
    DEGREE = "degree"
    ONE_OFF_SENDERS = "one_off_senders"
    INGEST_HIGH_DEGREE = "ingest_high_degree"
    DUST_FANOUT = "dust_fanout"  # FR-002-22, research R-22


class GraphWarning(StrEnum):
    ADDRESS_LISTS_NOT_APPLIED = "address_lists_not_applied"
    GIANT_COMPONENT = "giant_component"
    EMPTY_GRAPH = "empty_graph"
    ALL_SOURCES_PRUNED = "all_sources_pruned"
    DELEGATED_INCOMPLETE = "delegated_incomplete"


class GraphCompletenessStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


NOT_ANALYZED = NOT_ANALYZED_REASON  # одне джерело з 001: "not_analyzed"
_MISSING_REASONS = frozenset(r.value for r in MissingReason)


class GraphInputError(Exception):
    """Порушення контракту 001 на вході побудови графа — дефект, не дані мережі."""


# --- Перевірки -------------------------------------------------------------------

_PATH_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _int(name: str, value: Any, lo: int | None = None, hi: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: expected int, got {value!r}")
    if lo is not None and value < lo:
        raise ValueError(f"{name}: {value} < {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{name}: {value} > {hi}")


def _opt_int(name: str, value: Any, lo: int | None = None, hi: int | None = None) -> None:
    if value is not None:
        _int(name, value, lo, hi)


def _share(name: str, value: Any, *, lo_open: bool) -> None:
    """Частка з конфігу: число в [0, 1] (або (0, 1], якщо `lo_open`)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name}: expected number, got {value!r}")
    if value > 1 or value < 0 or (lo_open and value == 0):
        raise ValueError(f"{name}: {value} out of range")


def _bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name}: expected bool, got {value!r}")


def _str(name: str, value: Any, *, nonempty: bool) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name}: expected str, got {value!r}")
    if nonempty and not value:
        raise ValueError(f"{name}: must not be empty")


def _enum(obj: object, name: str, enum: type[StrEnum]) -> None:
    value = getattr(obj, name)
    if not isinstance(value, enum):
        if not isinstance(value, str):
            raise TypeError(f"{name}: expected {enum.__name__}, got {value!r}")
        _set(obj, name, enum(value))  # невідоме значення -> ValueError


def _instance(name: str, value: Any, cls: type) -> None:
    if not isinstance(value, cls):
        raise TypeError(f"{name}: expected {cls.__name__}, got {value!r}")


def _items(name: str, value: Any) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{name}: expected a collection, got {value!r}")
    return tuple(value)


def _tuple_of(obj: object, name: str, cls: type) -> tuple:
    items = _items(name, getattr(obj, name))
    for item in items:
        _instance(name, item, cls)
    _set(obj, name, items)
    return items


def _path_key(path: str | None) -> tuple[int, ...]:
    return () if path is None else tuple(int(part) for part in path.split("."))


# --- Вершини ---------------------------------------------------------------------


@dataclass(frozen=True)
class NodeMeasures:
    """Виміри для критеріїв хаба (research R-7, R-8, R-22); частка — відношення двох цілих поруч.

    `buyer_fanout`/`median_to_buyers` (FR-002-22) — кількість різних покупців, яким
    вершина надіслала SOL, і верхня медіана сум цих ребер у лампортах. Медіану без
    списку сум не відтворити, тож перевіряється лише `None`-інваріант і `>= 1`.
    """

    degree: int
    unique_senders: int
    one_off_senders: int
    one_off_share: float | None
    buyer_fanout: int
    median_to_buyers: int | None

    def __post_init__(self) -> None:
        _int("measures.buyer_fanout", self.buyer_fanout, lo=0)
        if self.median_to_buyers is not None:
            _int("measures.median_to_buyers", self.median_to_buyers)
        if (self.median_to_buyers is None) != (self.buyer_fanout == 0):
            raise ValueError(
                f"measures: median_to_buyers={self.median_to_buyers!r} must be None iff "
                f"buyer_fanout == 0 (buyer_fanout={self.buyer_fanout})"
            )
        _opt_int("measures.median_to_buyers", self.median_to_buyers, lo=1)

        _int("measures.degree", self.degree, lo=0)
        _int("measures.unique_senders", self.unique_senders, lo=0)
        _int("measures.one_off_senders", self.one_off_senders, lo=0)
        if self.one_off_senders > self.unique_senders:
            raise ValueError(
                f"measures: one_off_senders={self.one_off_senders} > unique_senders={self.unique_senders}"
            )
        if self.unique_senders == 0:
            if self.one_off_share is not None:
                raise ValueError("measures.one_off_share: must be None when unique_senders == 0")
            return
        if self.one_off_share is None:
            raise ValueError("measures.one_off_share: required when unique_senders > 0")
        if not isinstance(self.one_off_share, float):
            raise TypeError(f"measures.one_off_share: expected float, got {self.one_off_share!r}")
        if self.one_off_share != self.one_off_senders / self.unique_senders:
            raise ValueError(
                f"measures.one_off_share: {self.one_off_share} != "
                f"{self.one_off_senders}/{self.unique_senders}"
            )


@dataclass(frozen=True)
class UnexpandedMark:
    """Атрибут нерозгорнутості вершини (FR-002-06): копія `UnexpandedNode` без `wallet`/`depth`."""

    reason: UnexpandedReason
    counterparties_seen: int
    signatures_seen: int
    signatures_truncated: bool

    def __post_init__(self) -> None:
        _enum(self, "reason", UnexpandedReason)
        _int("unexpanded.counterparties_seen", self.counterparties_seen, lo=0)
        _int("unexpanded.signatures_seen", self.signatures_seen, lo=0)
        _bool("unexpanded.signatures_truncated", self.signatures_truncated)


@dataclass(frozen=True)
class Node:
    """Вершина (FR-002-03). Ключ унікальності — `address`."""

    address: str
    roles: frozenset[NodeRole]
    depth: int
    buyer_rank: int | None
    address_type: AddressType
    unexpanded: UnexpandedMark | None
    measures: NodeMeasures

    def __post_init__(self) -> None:
        _str("node.address", self.address, nonempty=True)
        roles = _items("node.roles", self.roles)
        for role in roles:
            if not isinstance(role, str):
                raise TypeError(f"node.roles: expected NodeRole, got {role!r}")
        roles = frozenset(NodeRole(role) for role in roles)  # невідома роль -> ValueError
        if not roles:
            raise ValueError("node.roles: must not be empty")
        _set(self, "roles", roles)
        _int("node.depth", self.depth, lo=0)
        _opt_int("node.buyer_rank", self.buyer_rank, lo=1)
        is_buyer = NodeRole.BUYER in roles
        if is_buyer != (self.buyer_rank is not None):
            raise ValueError("node: role 'buyer' iff buyer_rank is not None")
        if is_buyer and self.depth != 0:
            raise ValueError(f"node: buyer must have depth 0, got {self.depth}")
        _enum(self, "address_type", AddressType)
        if self.unexpanded is not None:
            _instance("node.unexpanded", self.unexpanded, UnexpandedMark)
        _instance("node.measures", self.measures, NodeMeasures)

    def __repr__(self) -> str:
        # FR-002-04: `frozenset` показує елементи в порядку хешів, а хеш рядка залежить від
        # `PYTHONHASHSEED`; ролі виводяться впорядковано, решта полів — як у dataclass-repr.
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "roles":
                value = f"frozenset({{{', '.join(repr(r) for r in sorted(value))}}})"
            else:
                value = repr(value)
            parts.append(f"{f.name}={value}")
        return f"{type(self).__name__}({', '.join(parts)})"


# --- Ребра -----------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeRef:
    """Первинне посилання ребра (FR-002-02). `instruction_path` — для `transfer`; `None` — для `delegated_buy`."""

    signature: str
    slot: int
    instruction_path: str | None

    def __post_init__(self) -> None:
        _str("ref.signature", self.signature, nonempty=True)
        _int("ref.slot", self.slot, lo=0)
        if self.instruction_path is not None:
            _str("ref.instruction_path", self.instruction_path, nonempty=True)
            if not _PATH_RE.match(self.instruction_path):
                raise ValueError(f"ref.instruction_path: {self.instruction_path!r} is not 'i' or 'i.j'")


@dataclass(frozen=True)
class Edge:
    """Агреговане ребро (FR-002-01, FR-002-02, FR-002-18). Ключ — `(kind, sender, receiver, asset)`."""

    kind: EdgeKind
    sender: str
    receiver: str
    asset: Asset | None
    amount: int | None
    decimals: int | None
    count: int
    first_slot: int
    last_slot: int
    first_time: int | None
    last_time: int | None
    refs: tuple[EdgeRef, ...]

    def __post_init__(self) -> None:
        _enum(self, "kind", EdgeKind)
        _str("edge.sender", self.sender, nonempty=True)
        _str("edge.receiver", self.receiver, nonempty=True)
        if self.sender == self.receiver:
            raise ValueError(f"edge: self-transfer {self.sender} -> {self.receiver}")

        refs = _tuple_of(self, "refs", EdgeRef)
        if not refs:
            raise ValueError("edge.refs: must not be empty")
        if self.kind is EdgeKind.TRANSFER:
            if self.asset is None:
                raise ValueError("edge: transfer requires asset")
            _set(self, "asset", Asset(self.asset))
            if self.amount is None:
                raise ValueError("edge: transfer requires amount")
            _int("edge.amount", self.amount, lo=1)
            _opt_int("edge.decimals", self.decimals, lo=0, hi=255)  # SPL decimals — u8
            if self.asset == Asset.SOL and self.decimals is not None:
                raise ValueError("edge.decimals: must be None for sol")
            if any(r.instruction_path is None for r in refs):
                raise ValueError("edge: every transfer ref requires instruction_path")
        else:
            for name in ("asset", "amount", "decimals"):
                if getattr(self, name) is not None:
                    raise ValueError(f"edge: delegated_buy carries no {name}")
            if any(r.instruction_path is not None for r in refs):
                raise ValueError("edge: delegated_buy refs carry no instruction_path")

        # Для delegated_buy усі шляхи None, тож це водночас «без дублів signature».
        if len({(r.signature, r.instruction_path) for r in refs}) != len(refs):
            raise ValueError("edge.refs: duplicate (signature, instruction_path)")
        refs = tuple(sorted(refs, key=ref_sort_key))
        _set(self, "refs", refs)

        _int("edge.count", self.count, lo=1)
        if self.count != len(refs):
            raise ValueError(f"edge.count={self.count} != len(refs)={len(refs)}")
        _int("edge.first_slot", self.first_slot, lo=0)
        _int("edge.last_slot", self.last_slot, lo=0)
        if self.first_slot > self.last_slot:
            raise ValueError(f"edge: first_slot={self.first_slot} > last_slot={self.last_slot}")
        if self.first_slot != refs[0].slot or self.last_slot != max(r.slot for r in refs):
            raise ValueError("edge: first_slot/last_slot must come from refs")
        _opt_int("edge.first_time", self.first_time, lo=0)
        _opt_int("edge.last_time", self.last_time, lo=0)

    @property
    def key(self) -> tuple[EdgeKind, str, str, Asset | None]:
        return (self.kind, self.sender, self.receiver, self.asset)


# --- Граф ------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingGraph:
    """Граф фінансування. Конструктор сортує вершини й ребра канонічно сам."""

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def __post_init__(self) -> None:
        nodes = _tuple_of(self, "nodes", Node)
        edges = _tuple_of(self, "edges", Edge)
        addresses = {n.address for n in nodes}
        if len(addresses) != len(nodes):
            raise ValueError("graph.nodes: duplicate address")
        for e in edges:
            if e.sender not in addresses or e.receiver not in addresses:
                raise ValueError(f"graph.edges: endpoint is not a node: {e.sender} -> {e.receiver}")
        if len({e.key for e in edges}) != len(edges):
            raise ValueError("graph.edges: duplicate key (kind, sender, receiver, asset)")
        _set(self, "nodes", tuple(sorted(nodes, key=node_sort_key)))
        _set(self, "edges", tuple(sorted(edges, key=edge_sort_key)))

    def _index(self) -> dict[str, Node]:
        # Кеш прив'язаний до самого кортежу: примусова заміна `nodes` його інвалідує.
        cached = self.__dict__.get("_node_index")
        if cached is None or cached[0] is not self.nodes:
            cached = (self.nodes, {n.address: n for n in self.nodes})
            _set(self, "_node_index", cached)
        return cached[1]

    def node(self, address: str) -> Node:
        return self._index()[address]  # невідома адреса -> KeyError

    def incident(self, address: str) -> tuple[Edge, ...]:
        self.node(address)
        return tuple(e for e in self.edges if e.sender == address or e.receiver == address)

    def buyers(self) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if NodeRole.BUYER in n.roles)

    def without(self, addresses: Iterable[str]) -> FundingGraph:
        """Новий граф без вершин `addresses` та всіх інцидентних ребер; `measures` не перераховуються."""
        drop = frozenset(_items("without.addresses", addresses))
        index = self._index()
        unknown = sorted(a for a in drop if a not in index)
        if unknown:
            raise KeyError(f"without: not a node: {unknown}")
        return FundingGraph(
            nodes=tuple(n for n in self.nodes if n.address not in drop),
            edges=tuple(e for e in self.edges if e.sender not in drop and e.receiver not in drop),
        )


# --- Повнота ---------------------------------------------------------------------


@dataclass(frozen=True)
class MissingRef:
    """Копія `MissingHistory` 001: `(wallet, depth, reason, detail)`."""

    wallet: str
    depth: int
    reason: MissingReason
    detail: str

    def __post_init__(self) -> None:
        _str("missing.wallet", self.wallet, nonempty=True)
        _int("missing.depth", self.depth, lo=0)
        _enum(self, "reason", MissingReason)
        _str("missing.detail", self.detail, nonempty=False)


def _missing_order(m: MissingRef) -> tuple[int, str, str]:
    # Той самий порядок, що й `Completeness.missing` у 001.
    return (m.depth, m.wallet, m.reason.value)


def _reason(name: str, complete: Any, reason: Any, allowed: frozenset[str]) -> None:
    _bool(f"{name}_complete", complete)
    if complete and reason is not None:
        raise ValueError(f"{name}: complete=True must not carry a reason")
    if not complete:
        if reason is None:
            raise ValueError(f"{name}: complete=False requires a reason")
        _str(f"{name}_reason", reason, nonempty=True)
        if reason not in allowed:
            raise ValueError(f"{name}_reason: unknown {reason!r}")


_DERIVE_TOKEN = object()


@dataclass(frozen=True)
class GraphCompleteness:
    """Повнота графа (FR-002-05, FR-002-19). Лише через `derive`; `status` і `ingest_status` — властивості."""

    missing: tuple[MissingRef, ...]
    buyers_complete: bool
    buyers_reason: str | None
    delegated_complete: bool
    delegated_reason: str | None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _DERIVE_TOKEN:
            raise TypeError("GraphCompleteness is built only via GraphCompleteness.derive(ingest)")
        # Токен одноразовий: `dataclasses.replace` не може обійти `derive`.
        _set(self, "_token", None)
        items = _tuple_of(self, "missing", MissingRef)
        _set(self, "missing", tuple(sorted(items, key=_missing_order)))
        _reason("buyers", self.buyers_complete, self.buyers_reason, _MISSING_REASONS)
        _reason("delegated", self.delegated_complete, self.delegated_reason,
                _MISSING_REASONS | {NOT_ANALYZED})

    @classmethod
    def derive(cls, ingest: IngestResult) -> GraphCompleteness:
        if not isinstance(ingest, IngestResult):
            raise TypeError(f"derive: expected IngestResult, got {type(ingest).__name__}")
        completeness = ingest.completeness
        buyers = completeness.buyers
        # З T-044 `IngestResult.delegated` обов'язковий; чесне умовчання «не аналізували» —
        # `DelegatedAnalysis.NOT_ANALYZED` у самому 001 (єдине джерело). Граф його не вгадує:
        # зламане поле — порушення контракту 001, гучно. Значення `complete`/`reason`
        # перевіряє `_reason` у конструкторі (суперечлива пара — ValueError).
        delegated = ingest.delegated
        if delegated is None:
            raise TypeError("derive: IngestResult.delegated is None; expected DelegatedAnalysis (contract 001 1.1)")
        delegated_complete = delegated.complete
        reason = delegated.reason
        delegated_reason = None if reason is None else str(reason)
        result = cls(
            tuple(MissingRef(m.wallet, m.depth, m.reason, m.detail) for m in completeness.missing),
            buyers.complete,
            None if buyers.reason is None else str(buyers.reason),
            delegated_complete,
            delegated_reason,
            _DERIVE_TOKEN,
        )
        if result.ingest_status != completeness.status:  # правило 001 змінилось — дефект
            raise AssertionError("GraphCompleteness.ingest_status diverged from IngestResult.completeness")
        return result

    @property
    def ingest_status(self) -> CompletenessStatus:
        if not self.missing and self.buyers_complete:
            return CompletenessStatus.COMPLETE
        return CompletenessStatus.INCOMPLETE

    @property
    def status(self) -> GraphCompletenessStatus:
        if self.ingest_status is CompletenessStatus.COMPLETE and self.delegated_complete:
            return GraphCompletenessStatus.COMPLETE
        return GraphCompletenessStatus.INCOMPLETE


# --- Метадані й результат --------------------------------------------------------


@dataclass(frozen=True)
class ThresholdsSnapshot:
    """Знімок порогів `config/hubs.yaml` у метаданих результату (FR-002-13)."""

    degree_threshold: int
    one_off_senders_share: float
    one_off_min_senders: int
    giant_component_warn_share: float
    prune_off_curve: bool
    prune_ingest_high_degree: bool
    dust_amount_lamports: int
    dust_min_fanout: int

    def __post_init__(self) -> None:
        _int("thresholds.degree_threshold", self.degree_threshold, lo=1)
        _share("thresholds.one_off_senders_share", self.one_off_senders_share, lo_open=False)
        _int("thresholds.one_off_min_senders", self.one_off_min_senders, lo=2)
        _share("thresholds.giant_component_warn_share", self.giant_component_warn_share, lo_open=True)
        _bool("thresholds.prune_off_curve", self.prune_off_curve)
        _bool("thresholds.prune_ingest_high_degree", self.prune_ingest_high_degree)
        _int("thresholds.dust_amount_lamports", self.dust_amount_lamports, lo=1)
        _int("thresholds.dust_min_fanout", self.dust_min_fanout, lo=2)


@dataclass(frozen=True)
class GraphMetadata:
    mint: str
    schema_version: str
    ingest_analyzed_at: int
    ingest_config_version: int
    ingest_source: str
    wallets_analyzed: int
    hub_config_version: int
    address_lists_version: int | None
    lists_applied: bool
    thresholds: ThresholdsSnapshot
    nodes_total: int
    edges_total: int

    def __post_init__(self) -> None:
        _str("metadata.mint", self.mint, nonempty=True)
        _str("metadata.schema_version", self.schema_version, nonempty=True)
        if self.schema_version != GRAPH_SCHEMA_VERSION:
            raise ValueError(f"metadata.schema_version: {self.schema_version!r} != {GRAPH_SCHEMA_VERSION!r}")
        _int("metadata.ingest_analyzed_at", self.ingest_analyzed_at, lo=0)
        _int("metadata.ingest_config_version", self.ingest_config_version, lo=1)
        _str("metadata.ingest_source", self.ingest_source, nonempty=True)
        _int("metadata.wallets_analyzed", self.wallets_analyzed, lo=0)
        _int("metadata.hub_config_version", self.hub_config_version, lo=1)
        _opt_int("metadata.address_lists_version", self.address_lists_version, lo=1)
        _bool("metadata.lists_applied", self.lists_applied)
        if (self.address_lists_version is None) == self.lists_applied:
            raise ValueError("metadata: address_lists_version is None iff lists_applied is False")
        _instance("metadata.thresholds", self.thresholds, ThresholdsSnapshot)
        _int("metadata.nodes_total", self.nodes_total, lo=0)
        _int("metadata.edges_total", self.edges_total, lo=0)


@dataclass(frozen=True)
class GraphResult:
    """Результат фічі 002. `pruned`/`buyer_flags`/`report` — типи `unmask.hubs` (T-035, T-036)."""

    metadata: GraphMetadata
    completeness: GraphCompleteness
    graph: FundingGraph
    pruned: tuple[Any, ...]
    buyer_flags: tuple[Any, ...]
    report: Any

    def __post_init__(self) -> None:
        _instance("result.metadata", self.metadata, GraphMetadata)
        _instance("result.completeness", self.completeness, GraphCompleteness)
        _instance("result.graph", self.graph, FundingGraph)
        pruned = _items("result.pruned", self.pruned)
        for record in pruned:
            if not isinstance(getattr(record, "address", None), str):
                raise TypeError(f"result.pruned: expected a prune record with address, got {record!r}")
        _set(self, "pruned", pruned)
        _set(self, "buyer_flags", _items("result.buyer_flags", self.buyer_flags))
        warnings = _items("result.report.warnings", getattr(self.report, "warnings", None))

        buyers = len(self.graph.buyers())
        if buyers != self.metadata.wallets_analyzed:
            raise ValueError(
                f"result: graph has {buyers} buyers != metadata.wallets_analyzed={self.metadata.wallets_analyzed}"
            )
        overlap = sorted({r.address for r in pruned} & {n.address for n in self.graph.nodes})
        if overlap:
            raise ValueError(f"result: pruned addresses still in graph: {overlap}")
        if self.metadata.nodes_total != len(self.graph.nodes) + len(pruned):
            raise ValueError(
                f"result: metadata.nodes_total={self.metadata.nodes_total} != "
                f"len(graph.nodes)={len(self.graph.nodes)} + len(pruned)={len(pruned)}"
            )
        if not self.metadata.lists_applied and GraphWarning.ADDRESS_LISTS_NOT_APPLIED not in warnings:
            raise ValueError("result: lists not applied requires warning address_lists_not_applied")


# --- Ключі порядку: лише з даних -------------------------------------------------


def node_sort_key(node: Node) -> str:
    return node.address


def edge_sort_key(edge: Edge) -> tuple[str, str, str, str]:
    """`(kind, sender, receiver, asset or "")`; `kind` — рядкове значення виду."""
    return (edge.kind.value, edge.sender, edge.receiver, edge.asset or "")


def ref_sort_key(ref: EdgeRef) -> tuple[int, str, tuple[int, ...]]:
    """`(slot, signature, instruction_path)`; шлях — числово: "2" < "2.1" < "10"; `None` -> `()`."""
    return (ref.slot, ref.signature, _path_key(ref.instruction_path))
