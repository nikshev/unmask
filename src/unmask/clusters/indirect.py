# impl: FR-003-04, FR-003-05, FR-003-06
"""Прохід 2: непрямі зв'язки через спільне джерело на глибині 2."""

from __future__ import annotations

from typing import Mapping

from unmask.clusters.config import ClusterConfig
from unmask.clusters.links import Link, basis_for, edge_time, group_by_window, link_sort_key, window_of, window_slots
from unmask.clusters.model import EvidenceType, TimeBasis
from unmask.graph.model import Edge, EdgeKind, NodeRole, ref_sort_key

__all__ = ["indirect_links"]


def indirect_links(graph_result, clusters_so_far: Mapping[str, str], config: ClusterConfig) -> tuple[Link, ...]:
    """Непрямі зв'язки `C → M → P` / `C → P` через спільне джерело `C` на глибині 2.

    Група ребер `C → *` дає зв'язок, лише якщо її покупці належать ≥ 2 різним
    компонентам проходу 1. `indirect_enabled == False` → `()`.
    """
    if not config.indirect_enabled:
        return ()
    graph = graph_result.graph
    buyers = {n.address for n in graph.nodes if NodeRole.BUYER in n.roles}
    flagged = {f.address for f in graph_result.buyer_flags}
    allow_flagged = config.link_through_flagged_buyers
    assets = set(config.link_assets)
    min_amount = config.link_min_amount_lamports

    def transfer_ok(e: Edge) -> bool:
        return (
            e.kind == EdgeKind.TRANSFER
            and e.asset in assets
            and e.amount is not None
            and e.amount >= min_amount
        )

    def buyer_ok(p: str) -> bool:
        return p in buyers and (allow_flagged or p not in flagged)

    from_mid: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if not transfer_ok(e):
            continue
        if e.sender in buyers or e.receiver not in buyers:
            continue
        if not buyer_ok(e.receiver):
            continue
        from_mid.setdefault(e.sender, []).append(e)
    mids_with_buyers = set(from_mid)

    by_source: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if not transfer_ok(e):
            continue
        if e.sender in buyers:
            continue
        if e.receiver in buyers:
            if buyer_ok(e.receiver):
                by_source.setdefault(e.sender, []).append(e)
        elif e.receiver in mids_with_buyers:
            # C → M: зараховується, лише якщо M веде хоч до одного покупця
            by_source.setdefault(e.sender, []).append(e)

    links: list[Link] = []
    for source in sorted(by_source):
        c_edges = by_source[source]
        basis = basis_for(c_edges)
        gap = config.funding_window_seconds if basis == TimeBasis.BLOCK_TIME else window_slots(config)
        for group in group_by_window([(edge_time(e, basis), e) for e in c_edges], gap):
            reached: dict[str, list[Edge]] = {}
            mids: set[str] = set()
            for ce in group:
                if ce.receiver in buyers:
                    reached.setdefault(ce.receiver, []).append(ce)
                else:
                    for ie in from_mid.get(ce.receiver, ()):
                        reached.setdefault(ie.receiver, []).append(ce)
                        reached[ie.receiver].append(ie)
                        mids.add(ce.receiver)
            if len({clusters_so_far.get(p, p) for p in reached}) < 2:
                continue
            members = frozenset(reached)
            if len(members) < 2:
                continue
            refs = tuple(sorted({r for edges in reached.values() for e in edges for r in e.refs},
                                key=ref_sort_key))
            via = tuple(sorted((source,) + tuple(mids)))
            links.append(Link(
                type=EvidenceType.INDIRECT_LINK,
                buyers=members,
                via=via,
                refs=refs,
                window=window_of(group, basis),
                basis=basis,
            ))

    # Відсічені вершини в `graph` відсутні — зв'язку через них немає без окремого коду.
    return tuple(sorted(links, key=link_sort_key))
