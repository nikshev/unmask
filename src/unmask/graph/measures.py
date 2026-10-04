# impl: FR-002-07, FR-002-22
"""Виміри вершин графа фінансування (research R-7, R-8, R-22; contracts/graph-service.md §2a).

`compute(nodes, edges) -> dict[address, NodeMeasures]` — чиста функція: лише
вимірює структуру, порогів і рішень «хаб / не хаб» не знає (це `unmask.hubs`).

Означення для вершини `v` (на повному графі, до відсікання):

- `degree` (R-7) — кількість унікальних контрагентів `u`, з якими є ребро `u -> v`
  або `v -> u` будь-якого виду й активу. Не кількість ребер і не переказів.
- `unique_senders` (R-8) — унікальні відправники вхідних ребер виду `transfer`
  (усі активи); `one_off_senders` — ті з них, у кого сумарний `count` по всіх
  ребрах `u -> v` виду `transfer` дорівнює 1; `one_off_share` — їх відношення
  або `None`, якщо відправників нема. `delegated_buy` відправника не робить.
- `buyer_fanout` (R-22) — кількість різних вершин із роллю `buyer`, до яких є
  ребро `(transfer, v, b, sol)`; SPL-ребра й `delegated_buy` не рахуються.
- `median_to_buyers` (R-22) — верхня медіана `Edge.amount` цих ребер, одне
  значення на покупця (агрегат усіх SOL-переказів `v -> b`):
  `sorted(amounts)[len(amounts) // 2]`, ціле в лампортах; `None` ⇔ fan-out 0.
  Верхня медіана нижче порогу ⇔ строга більшість покупців отримала менше.

Вершини на вході — будь-які об'єкти з `address` і `roles` (`Node` або проміжні
вершини `build_graph`, у яких вимірів ще нема); роль `buyer` береться звідти.
Ребра — `Edge`. Порушення контракту входу (дубль адреси, дубль ключа ребра,
кінець ребра поза вершинами) падає `ValueError`: тихий підрахунок по дублю чи
по невідомій вершині спотворив би виміри без жодного сліду.

Детермінізм (FR-002-04): результат — функція множин вершин і ребер, не їхнього
порядку; ключі словника — у порядку адрес. Складність O(V + E log E) (R-17).
Модуль імпортує лише `unmask.graph.model` (plan «Правило залежностей»).
"""

from __future__ import annotations

from typing import Iterable

from unmask.graph.model import Edge, EdgeKind, NodeMeasures, NodeRole

_SOL = "sol"


def compute(nodes: Iterable, edges: Iterable[Edge]) -> dict[str, NodeMeasures]:
    """Виміри кожної вершини `nodes` за ребрами `edges`; див. означення в шапці модуля."""
    roles_of: dict[str, frozenset] = {}
    for node in nodes:
        address = node.address
        if address in roles_of:
            raise ValueError(f"measures: duplicate node address {address!r}")
        roles_of[address] = frozenset(node.roles)

    counterparties: dict[str, set[str]] = {a: set() for a in roles_of}
    in_counts: dict[str, dict[str, int]] = {a: {} for a in roles_of}  # v -> {sender: Σ count}
    to_buyers: dict[str, list[int]] = {a: [] for a in roles_of}  # v -> суми SOL-ребер до покупців
    keys: set[tuple] = set()

    for edge in edges:
        if not isinstance(edge, Edge):
            raise TypeError(f"measures: expected Edge, got {edge!r}")
        if edge.key in keys:
            raise ValueError(f"measures: duplicate edge key {edge.key!r}")
        keys.add(edge.key)
        sender, receiver = edge.sender, edge.receiver
        for end in (sender, receiver):
            if end not in roles_of:
                raise ValueError(f"measures: edge endpoint is not a node: {sender} -> {receiver}")

        counterparties[sender].add(receiver)
        counterparties[receiver].add(sender)
        if edge.kind is not EdgeKind.TRANSFER:
            continue
        senders = in_counts[receiver]
        senders[sender] = senders.get(sender, 0) + edge.count
        # Ключ ребра унікальний, тож на пару (v, b) у SOL — рівно одне ребро = одна сума.
        if edge.asset == _SOL and NodeRole.BUYER in roles_of[receiver]:
            to_buyers[sender].append(edge.amount)

    result: dict[str, NodeMeasures] = {}
    for address in sorted(roles_of):
        senders = in_counts[address]
        unique = len(senders)
        one_off = sum(1 for total in senders.values() if total == 1)
        amounts = sorted(to_buyers[address])
        result[address] = NodeMeasures(
            degree=len(counterparties[address]),
            unique_senders=unique,
            one_off_senders=one_off,
            one_off_share=None if unique == 0 else one_off / unique,
            buyer_fanout=len(amounts),
            median_to_buyers=amounts[len(amounts) // 2] if amounts else None,
        )
    return result
