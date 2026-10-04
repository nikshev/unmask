# impl: FR-002-11
"""Компоненти зв'язності графа фінансування (research R-10; contracts/graph-service.md §3).

Слабка зв'язність: напрямок ребер ігнорується, з'єднують ребра **обох** видів
(`transfer` і `delegated_buy`) — R-10 «по всіх ребрах обох видів». Ізольована
вершина — компонента розміру 1; ізольований покупець входить у знаменник частки.

Union-find з об'єднанням за рангом і стисканням шляху, ітеративний (жодної
рекурсії: граф може мати сотні тисяч ребер). Складність O((V + E)·α(V)) (R-17).

Детермінізм (SC-004): вершини індексуються за адресою (канонічний порядок
`FundingGraph`), ідентифікатор компоненти — мінімальна адреса в ній, вершини
компоненти й самі компоненти — у порядку адрес. Від порядку вершин і ребер на
вході результат не залежить: розбиття на компоненти — властивість множини
ребер, а id і порядок обчислюються з адрес, а не з дерева union-find.

«Найбільша компонента серед покупців» (R-10) — з максимальною кількістю
покупців; частка = покупці в ній / усі покупці графа (`buyers_total`; у
`GraphResult` це `wallets_analyzed`). 0 покупців -> частка 0.0 (R-10, R-16,
data-model `EffectSnapshot`), без ділення на нуль.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from unmask.graph.model import FundingGraph, NodeRole


@dataclass(frozen=True)
class Components:
    """Розбиття вершин графа на компоненти слабкої зв'язності. Будується лише `components()`."""

    _component_of: Mapping[str, str]
    _members: Mapping[str, tuple[str, ...]]  # id -> вершини; ключі в порядку id
    _buyers: Mapping[str, int]  # id -> кількість вершин з роллю buyer; ключі в порядку id
    buyers_total: int

    @property
    def count(self) -> int:
        return len(self._members)

    def ids(self) -> tuple[str, ...]:
        """Ідентифікатори компонент у порядку зростання (кожен — мінімальна адреса компоненти)."""
        return tuple(self._members)

    def of(self, address: str) -> str:
        """Ідентифікатор компоненти вершини; невідома адреса -> KeyError."""
        return self._component_of[address]

    def members(self, component_id: str) -> tuple[str, ...]:
        """Адреси компоненти за зростанням; невідомий id -> KeyError."""
        return self._members[component_id]

    def buyers_by_component(self) -> dict[str, int]:
        """id -> кількість вершин з роллю `buyer` (усі компоненти, зокрема з 0); копія в порядку id."""
        return dict(self._buyers)

    def largest_buyer_component(self) -> str | None:
        """Компонента з найбільшою кількістю покупців; `None`, якщо покупців немає.

        Тай-брейк (R-10 його не фіксує; на частку не впливає — лише на те, яку
        компоненту названо): більше вершин, далі менший id.
        """
        best: tuple[int, int] | None = None
        best_id: str | None = None
        for cid, buyers in self._buyers.items():  # id зростають: перший за рівності — менший id
            if buyers == 0:
                continue
            rank = (buyers, len(self._members[cid]))
            if best is None or rank > best:
                best, best_id = rank, cid
        return best_id

    def buyers_in_largest_component(self) -> int:
        cid = self.largest_buyer_component()
        return 0 if cid is None else self._buyers[cid]

    def largest_component_buyer_share(self) -> float:
        """`buyers_in_largest_component / buyers_total` без округлення; 0.0 при 0 покупців."""
        if self.buyers_total == 0:
            return 0.0
        return self.buyers_in_largest_component() / self.buyers_total


def components(graph: FundingGraph) -> Components:
    if not isinstance(graph, FundingGraph):
        raise TypeError(f"components: expected FundingGraph, got {type(graph).__name__}")

    addresses = sorted(n.address for n in graph.nodes)
    index = {a: i for i, a in enumerate(addresses)}
    parent = list(range(len(addresses)))
    rank = [0] * len(addresses)

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:  # стискання шляху: усі на шляху — одразу до кореня
            parent[i], i = root, parent[i]
        return root

    for edge in graph.edges:  # обидва види; напрямок не має значення
        a, b = find(index[edge.sender]), find(index[edge.receiver])
        if a == b:
            continue
        if rank[a] < rank[b]:
            a, b = b, a
        parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1

    buyer_set = {n.address for n in graph.nodes if NodeRole.BUYER in n.roles}
    root_to_id: dict[int, str] = {}
    component_of: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    buyers: dict[str, int] = {}
    for i, address in enumerate(addresses):  # за зростанням адрес: перша зустрінута — мінімальна
        root = find(i)
        cid = root_to_id.get(root)
        if cid is None:
            cid = root_to_id[root] = address
            members[cid] = []
            buyers[cid] = 0
        component_of[address] = cid
        members[cid].append(address)
        if address in buyer_set:
            buyers[cid] += 1

    return Components(
        _component_of=MappingProxyType(component_of),
        _members=MappingProxyType({cid: tuple(m) for cid, m in members.items()}),
        _buyers=MappingProxyType(buyers),
        buyers_total=len(buyer_set),
    )
