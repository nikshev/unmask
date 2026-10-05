# impl: FR-003-01, FR-003-02, FR-003-03, FR-003-05, FR-003-06
"""Зв'язки проходу 1 з графа 002: вікно-ланцюг, спільне джерело, прямий переказ, делегована купівля, відновлені ребра."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence, TypeVar

from unmask.clusters.config import ClusterConfig
from unmask.clusters.model import EvidenceType, LINK_TYPES, TimeBasis, Window
from unmask.graph.model import Edge, EdgeKind, EdgeRef, GraphResult, HubCriterion, NodeRole, ref_sort_key

__all__ = [
    "Link",
    "link_sort_key",
    "group_by_window",
    "window_slots",
    "basis_for",
    "edge_time",
    "window_of",
    "extract_links",
]

T = TypeVar("T")


@dataclass(frozen=True)
class Link:
    """Проміжний зв'язок: вхід union-find і джерело `Evidence`."""

    type: EvidenceType
    buyers: frozenset[str]
    via: tuple[str, ...]
    refs: tuple[EdgeRef, ...]
    window: Window
    basis: TimeBasis

    def __post_init__(self) -> None:
        if not isinstance(self.type, EvidenceType) or self.type not in LINK_TYPES:
            raise TypeError(f"type: expected EvidenceType in LINK_TYPES, got {self.type!r}")
        if not isinstance(self.buyers, frozenset) or len(self.buyers) < 2:
            raise ValueError("buyers: must be a frozenset of at least 2 buyers")
        for b in self.buyers:
            if not isinstance(b, str) or not b:
                raise TypeError("buyers: expected non-empty strings")
        if not isinstance(self.via, tuple):
            raise TypeError(f"via: expected tuple, got {type(self.via).__name__}")
        if tuple(sorted(self.via)) != self.via or len(set(self.via)) != len(self.via):
            raise ValueError("via: must be sorted and unique")
        if set(self.buyers) & set(self.via):
            raise ValueError("buyers and via must be disjoint")
        if not isinstance(self.refs, tuple) or len(self.refs) == 0:
            raise ValueError("refs: must be a non-empty tuple")
        for r in self.refs:
            if not isinstance(r, EdgeRef):
                raise TypeError(f"refs: expected EdgeRef, got {r!r}")
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("refs: must be unique")
        if tuple(sorted(self.refs, key=ref_sort_key)) != self.refs:
            raise ValueError("refs: must be sorted by ref_sort_key")
        if not isinstance(self.window, Window):
            raise TypeError(f"window: expected Window, got {type(self.window).__name__}")
        if self.basis != self.window.basis:
            raise ValueError("basis must equal window.basis")


def link_sort_key(link: Link) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[int, str, tuple[int, ...]]]:
    """Ключ порядку зв'язків: `(type, via, sorted(buyers), refs[0])` — лише з даних."""
    return (link.type.value, link.via, tuple(sorted(link.buyers)), ref_sort_key(link.refs[0]))


def _payload_key(payload: object) -> object:
    if isinstance(payload, Edge):
        first = payload.refs[0] if payload.refs else None
        return (payload.sender, payload.receiver, str(payload.asset), payload.first_slot,
                first.signature if first is not None else "",
                first.instruction_path if first is not None and first.instruction_path is not None else "")
    return repr(payload)


def group_by_window(items: Sequence[tuple[int, T]], max_gap: int) -> tuple[tuple[T, ...], ...]:
    """Групи за вікном-ланцюгом: сортувати за `(час, ключ елемента)`, розривати де `next − prev > max_gap` строго.

    Функція множини — не залежить від порядку входу.
    """
    if isinstance(max_gap, bool) or not isinstance(max_gap, int) or max_gap < 0:
        raise ValueError("max_gap: must be an int >= 0")
    if len(items) == 0:
        return ()
    ordered = sorted(items, key=lambda it: (it[0], _payload_key(it[1])))
    groups: list[list[T]] = [[ordered[0][1]]]
    prev_time = ordered[0][0]
    for time, payload in ordered[1:]:
        if time - prev_time > max_gap:
            groups.append([payload])
        else:
            groups[-1].append(payload)
        prev_time = time
    return tuple(tuple(g) for g in groups)


def window_slots(config: ClusterConfig) -> int:
    """Ширина вікна в слотах: `floor(funding_window_seconds / seconds_per_slot)`."""
    return math.floor(Fraction(config.funding_window_seconds, 1) / Fraction(str(config.seconds_per_slot)))


def basis_for(edges: Sequence[Edge]) -> TimeBasis:
    """`slot`, якщо хоч одне ребро має `first_time is None`, інакше `block_time`."""
    for e in edges:
        if e.first_time is None:
            return TimeBasis.SLOT
    return TimeBasis.BLOCK_TIME


def edge_time(edge: Edge, basis: TimeBasis) -> int:
    """Час ребра в заданому базисі: `first_time` або `first_slot`."""
    if basis == TimeBasis.SLOT:
        return edge.first_slot
    assert edge.first_time is not None
    return edge.first_time


def window_of(edges: Sequence[Edge], basis: TimeBasis) -> Window:
    """`[min, max]` часів ребер у заданому базисі."""
    times = [edge_time(e, basis) for e in edges]
    return Window(basis=basis, start=min(times), end=max(times))


def _single_window(edge: Edge) -> tuple[Window, TimeBasis]:
    if edge.first_time is None:
        return Window(basis=TimeBasis.SLOT, start=edge.first_slot, end=edge.last_slot), TimeBasis.SLOT
    end = edge.last_time if edge.last_time is not None else edge.first_time
    return Window(basis=TimeBasis.BLOCK_TIME, start=edge.first_time, end=end), TimeBasis.BLOCK_TIME


def _has_dust_hit(record) -> bool:
    """Чи має запис відсікання критерій `dust_fanout` (дак-типізація `criteria[].criterion`)."""
    for hit in record.criteria:
        criterion = hit.criterion
        value = criterion.value if hasattr(criterion, "value") else criterion
        if value == HubCriterion.DUST_FANOUT.value:
            return True
    return False


def extract_links(graph_result: GraphResult, config: ClusterConfig) -> tuple[Link, ...]:
    """Усі зв'язки проходу 1: `shared_funder`, `direct_transfer`, `delegated_buy`, `recovered_edge`."""
    graph = graph_result.graph
    buyers = {n.address for n in graph.nodes if NodeRole.BUYER in n.roles}
    flagged = {f.address for f in graph_result.buyer_flags}
    allow_flagged = config.link_through_flagged_buyers
    assets = set(config.link_assets)
    min_amount = config.link_min_amount_lamports

    def ends_ok(a: str, b: str) -> bool:
        return allow_flagged or (a not in flagged and b not in flagged)

    def transfer_ok(e: Edge) -> bool:
        return (
            e.kind == EdgeKind.TRANSFER
            and e.asset in assets
            and e.amount is not None
            and e.amount >= min_amount
        )

    links: list[Link] = []

    for e in graph.edges:
        if e.kind == EdgeKind.TRANSFER and transfer_ok(e):
            if e.sender in buyers and e.receiver in buyers and ends_ok(e.sender, e.receiver):
                window, basis = _single_window(e)
                links.append(Link(
                    type=EvidenceType.DIRECT_TRANSFER,
                    buyers=frozenset((e.sender, e.receiver)),
                    via=(),
                    refs=tuple(sorted(e.refs, key=ref_sort_key)),
                    window=window,
                    basis=basis,
                ))

    by_sender: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if not transfer_ok(e):
            continue
        if e.sender in buyers:
            continue  # джерело-покупець обробляється як direct_transfer, не shared_funder
        if e.receiver not in buyers:
            continue
        if not ends_ok(e.sender, e.receiver):
            continue
        by_sender.setdefault(e.sender, []).append(e)

    for sender, edges in by_sender.items():
        basis = basis_for(edges)
        gap = config.funding_window_seconds if basis == TimeBasis.BLOCK_TIME else window_slots(config)
        for group in group_by_window([(edge_time(e, basis), e) for e in edges], gap):
            distinct = {e.receiver for e in group}
            if len(distinct) < 2:
                continue
            refs = tuple(sorted({r for e in group for r in e.refs}, key=ref_sort_key))
            links.append(Link(
                type=EvidenceType.SHARED_FUNDER,
                buyers=frozenset(distinct),
                via=(sender,),
                refs=refs,
                window=window_of(group, basis),
                basis=basis,
            ))

    for e in graph.edges:
        if e.kind != EdgeKind.DELEGATED_BUY:
            continue
        if e.sender in buyers and e.receiver in buyers and ends_ok(e.sender, e.receiver):
            window, basis = _single_window(e)
            links.append(Link(
                type=EvidenceType.DELEGATED_BUY,
                buyers=frozenset((e.sender, e.receiver)),
                via=(),
                refs=tuple(sorted(e.refs, key=ref_sort_key)),
                window=window,
                basis=basis,
            ))

    for record in graph_result.pruned:
        # US6: відновлюються лише ребра змішаного відправника (dust_fanout); записи за частотою
        # (напр. one_off_senders) не з'єднують нікого — інакше g_hub став би одним кластером.
        if not _has_dust_hit(record):
            continue
        address = record.address
        cands = [
            e for e in record.incident_edges
            if e.kind == EdgeKind.TRANSFER
            and e.sender == address
            and e.receiver in buyers
            and e.asset in assets
            and e.amount is not None
            and e.amount >= min_amount
            and ends_ok(e.sender, e.receiver)
        ]
        if not cands:
            continue
        basis = basis_for(cands)
        gap = config.funding_window_seconds if basis == TimeBasis.BLOCK_TIME else window_slots(config)
        for group in group_by_window([(edge_time(e, basis), e) for e in cands], gap):
            distinct = {e.receiver for e in group}
            if len(distinct) < 2:
                continue
            refs = tuple(sorted({r for e in group for r in e.refs}, key=ref_sort_key))
            links.append(Link(
                type=EvidenceType.RECOVERED_EDGE,
                buyers=frozenset(distinct),
                via=(address,),
                refs=refs,
                window=window_of(group, basis),
                basis=basis,
            ))

    return tuple(sorted(links, key=link_sort_key))
