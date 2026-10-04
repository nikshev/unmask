# impl: FR-002-01, FR-002-02, FR-002-03, FR-002-04, FR-002-05, FR-002-06, FR-002-18
"""Побудова графа фінансування з результату збору 001 (contracts/graph-service.md §2).

`build_graph(result: IngestResult) -> FundingGraph` — чиста функція: без мережі,
годинника й файлів; усі дані — лише з `result` (FR-002-20).

Ребра (T-028, research R-5; FR-002-01, FR-002-02, FR-002-18):

- переказ -> ребро виду `transfer` з ключем `(transfer, sender, receiver, asset)`;
  кілька переказів з одним ключем зливаються в одне ребро, різні активи чи
  напрямки — ні (два активи однієї пари — два ребра: суми в різних одиницях);
- `amount` — сума в базових одиницях активу; `count` — кількість переказів;
- `refs` — `(signature, slot, instruction_path)` кожного переказу, рівно 1:1 зі
  входом, у порядку `ref_sort_key` моделі `(slot, signature, шлях числово)`;
- `first_slot`/`last_slot`, `first_time`/`last_time` — слот і `block_time`
  першого й останнього переказу в цьому порядку («за слотом», а не найменший чи
  найбільший час і не порядок входу); відсутній `block_time` лишається `None` на
  своєму кінці — час сусіднього переказу не підставляється, нуль теж;
- `decimals` — спільні для всіх переказів ребра; розбіжність — `GraphInputError`.

Ребра `delegated_buy` (R-5, FR-002-18) будуються з `result.delegated.links`, яке
з'являється в контракті 001 з T-044 (T-047); поки цього поля в `IngestResult`
немає, делегованих ребер нема з чого будувати.

Перевірки входу (порушення контракту 001 — дефект, а не дані мережі; гучно,
до побудови будь-якої частини графа): самопереказ, дубль
`(signature, instruction_path)` — у тому числі між різними ребрами, розбіжні
`decimals` ребра, дубль запису нерозгорнутої вершини, запис нерозгорнутої вершини
для адреси поза графом, адреса не-покупця, що не є публічним ключем (T-029: покупець без
валідного `rank`, два покупці з однією адресою, вершина без ролі, не-покупець із глибиною 0 —
суперечність глибини; `IngestResult` частину цього вже не пропускає, перевірка подвійна, R-6).

Вершини: `Node` не існує без ролей, глибини, типу адреси, `unexpanded` і вимірів,
тож граф без них побудувати не можна. Тут — мінімальна чесна побудова за R-6
(обсяг T-029/T-030; їхні задачі додають власні тести й решту перевірок):
покупці ∪ відправники ∪ отримувачі переказів; ролі `buyer`/`funder`; глибина —
мінімум за правилами (покупець 0; отримувач переказу <= `depth - 1`; відправник
<= `depth`); `buyer_rank` з `Buyer.rank`; тип адреси покупця — з 001, решти —
`solders.Pubkey.is_on_curve` (без імпорту `unmask.ingest.addresses`, R-6);
`unexpanded` — копія запису `result.unexpanded`. Виміри — `graph.measures.compute`
над проміжними вершинами (з ролями, без вимірів), викликаний наприкінці.

Детермінізм (FR-002-04): групування — словником, порядок refs — ключем з даних,
вершини й ребра впорядковує конструктор `FundingGraph`.

Залежності: `unmask.graph.model`, `unmask.graph.measures`, `unmask.ingest.model`,
`solders`. Жодного `unmask.hubs` (принцип VI) чи модулів збору 001.
"""

from __future__ import annotations

from dataclasses import dataclass

from solders.pubkey import Pubkey

from unmask.graph.measures import compute
from unmask.graph.model import (
    Edge,
    EdgeKind,
    EdgeRef,
    FundingGraph,
    GraphInputError,
    Node,
    NodeRole,
    UnexpandedMark,
    ref_sort_key,
)
from unmask.ingest.model import AddressType, Asset, Buyer, IngestResult, Transfer


def build_graph(result: IngestResult) -> FundingGraph:
    """Граф фінансування з результату збору; див. правила в шапці модуля."""
    if not isinstance(result, IngestResult):
        raise TypeError(f"build_graph: expected IngestResult, got {type(result).__name__}")
    edges = _transfer_edges(result.transfers)
    protos = _proto_nodes(result)
    measures = compute(protos, edges)
    nodes = tuple(
        Node(
            address=p.address, roles=p.roles, depth=p.depth, buyer_rank=p.buyer_rank,
            address_type=p.address_type, unexpanded=p.unexpanded, measures=measures[p.address],
        )
        for p in protos
    )
    return FundingGraph(nodes, edges)


# --- Ребра -------------------------------------------------------------------------


def _transfer_edges(transfers: tuple[Transfer, ...]) -> tuple[Edge, ...]:
    seen: set[tuple[str, str]] = set()
    groups: dict[tuple[str, str, Asset], list[Transfer]] = {}
    for t in transfers:
        if t.sender == t.receiver:
            raise GraphInputError(
                f"self-transfer {t.sender} -> {t.receiver} in {t.signature}:{t.instruction_path}"
            )
        ident = (t.signature, t.instruction_path)
        if ident in seen:
            raise GraphInputError(f"duplicate transfer (signature, instruction_path) = {ident}")
        seen.add(ident)
        groups.setdefault((t.sender, t.receiver, t.asset), []).append(t)
    return tuple(_edge(key, items) for key, items in groups.items())


def _edge(key: tuple[str, str, Asset], items: list[Transfer]) -> Edge:
    sender, receiver, asset = key
    decimals = {t.decimals for t in items}
    if len(decimals) != 1:
        raise GraphInputError(
            f"mismatched decimals {sorted(decimals, key=repr)} on {sender} -> {receiver} {asset}"
        )
    ordered = sorted(
        ((EdgeRef(t.signature, t.slot, t.instruction_path), t.block_time) for t in items),
        key=lambda pair: ref_sort_key(pair[0]),
    )
    first, last = ordered[0], ordered[-1]
    return Edge(
        kind=EdgeKind.TRANSFER,
        sender=sender,
        receiver=receiver,
        asset=asset,
        amount=sum(t.amount for t in items),
        decimals=decimals.pop(),
        count=len(ordered),
        first_slot=first[0].slot,
        last_slot=last[0].slot,
        first_time=first[1],
        last_time=last[1],
        refs=tuple(ref for ref, _ in ordered),
    )


# --- Вершини (мінімум для існування `Node`; обсяг T-029/T-030) ----------------------


@dataclass(frozen=True)
class _ProtoNode:
    """Вершина без вимірів — вхід `measures.compute` (йому потрібні `address` і `roles`)."""

    address: str
    roles: frozenset[NodeRole]
    depth: int
    buyer_rank: int | None
    address_type: AddressType
    unexpanded: UnexpandedMark | None


def _proto_nodes(result: IngestResult) -> tuple[_ProtoNode, ...]:
    buyers: dict[str, Buyer] = {}
    for b in result.buyers:
        # `IngestResult`/`Buyer` це вже не пропускають; перевірка навмисно подвійна (R-6).
        if b.wallet in buyers:
            raise GraphInputError(f"duplicate buyer {b.wallet}")
        rank = b.rank
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise GraphInputError(f"buyer {b.wallet} has no valid rank: {rank!r}")
        buyers[b.wallet] = b
    depth: dict[str, int] = {wallet: 0 for wallet in buyers}
    funders: set[str] = set()
    for t in result.transfers:
        funders.add(t.sender)
        for address, bound in ((t.receiver, t.depth - 1), (t.sender, t.depth)):
            depth[address] = min(depth.get(address, bound), bound)

    marks: dict[str, UnexpandedMark] = {}
    for u in result.unexpanded:
        if u.wallet in marks:
            raise GraphInputError(f"duplicate unexpanded record for {u.wallet}")
        if u.wallet not in depth:
            raise GraphInputError(f"unexpanded record for {u.wallet}, which is not a graph node")
        marks[u.wallet] = UnexpandedMark(
            reason=u.reason, counterparties_seen=u.counterparties_seen,
            signatures_seen=u.signatures_seen, signatures_truncated=u.signatures_truncated,
        )

    protos = []
    for address in sorted(depth):
        buyer = buyers.get(address)
        roles = set()
        if buyer is not None:
            roles.add(NodeRole.BUYER)
        if address in funders:
            roles.add(NodeRole.FUNDER)
        if not roles:
            raise GraphInputError(
                f"{address} receives a transfer but is neither a buyer nor a sender of any transfer: "
                "node without a role"
            )
        if buyer is None and depth[address] < 1:
            raise GraphInputError(
                f"contradictory depth: {address} is not a buyer but its depth bound is "
                f"{depth[address]} (buyer level)"
            )
        protos.append(_ProtoNode(
            address=address,
            roles=frozenset(roles),
            depth=depth[address],
            buyer_rank=None if buyer is None else buyer.rank,
            address_type=buyer.address_type if buyer is not None else _address_type(address),
            unexpanded=marks.get(address),
        ))
    return tuple(protos)


def _address_type(address: str) -> AddressType:
    try:
        key = Pubkey.from_string(address)
    except ValueError as exc:
        raise GraphInputError(f"address {address!r} is not a public key: {exc}") from exc
    return AddressType.WALLET if key.is_on_curve() else AddressType.OFF_CURVE
