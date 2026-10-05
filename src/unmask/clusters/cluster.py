# impl: FR-003-01, FR-003-12
"""Union-find над покупцями: компоненти зв'язків → чернетки кластерів."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from unmask.clusters.links import Link, link_sort_key
from unmask.clusters.model import cluster_id

__all__ = ["ClusterDraft", "form_clusters", "cluster_id"]


@dataclass(frozen=True)
class ClusterDraft:
    wallets: frozenset[str]
    links: tuple[Link, ...]


def form_clusters(buyers: Sequence[str], links: Sequence[Link]) -> tuple[ClusterDraft, ...]:
    """Компоненти зв'язності покупців за зв'язками; компонента ≥ 2 → `ClusterDraft`.

    Union-find за рангом зі стисканням шляху, ітеративний. Порядок — за мінімальною
    адресою; результат не залежить від порядку `links`.
    """
    parent: dict[str, str] = {}
    rank: dict[str, int] = {}
    for b in buyers:
        if b not in parent:
            parent[b] = b
            rank[b] = 0

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # стискання шляху, ітеративно
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    for link in links:
        members = sorted(link.buyers)
        first = members[0]
        for other in members[1:]:
            union(first, other)

    components: dict[str, set[str]] = {}
    for b in parent:
        components.setdefault(find(b), set()).add(b)

    drafts: list[ClusterDraft] = []
    for members in components.values():
        if len(members) < 2:
            continue
        frozen = frozenset(members)
        own = tuple(sorted((link for link in links if link.buyers <= frozen), key=link_sort_key))
        drafts.append(ClusterDraft(wallets=frozen, links=own))
    drafts.sort(key=lambda d: min(d.wallets))
    return tuple(drafts)
