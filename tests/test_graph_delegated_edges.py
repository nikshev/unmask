# verifies: FR-002-18
"""Ребра `delegated_buy` у графі фінансування (T-047; research R-5, R-6; FR-002-18).

Критична задача: делегована купівля, злита з переказом тієї ж пари, чи кандидат без пари, що став
вершиною, тихо спотворюють ребра, виміри, компоненти й докази кластерів, не ламаючи збірку. Тому:

- golden-звірка `g_delegated` з `expected.json` (еталон — окремий словник за `(payer, receiver)` у
  генераторі, R-18) і ще один незалежний перерахунок делегованих ребер із `ingest.json` тут;
- одна пара з переказом І делегованою купівлею — два ребра з різними `kind`, суми не змішуються;
- два зв'язки однієї пари — одне ребро `count=2`, `refs` за `(slot, signature)`, межі з крайніх refs,
  відсутній `block_time` лишається `None` на своєму кінці;
- ролі й глибина (отримувач <= 0, платник <= 1, мінімум з правилами переказів) і взаємодія з перевіркою
  суперечності глибини T-029: валідний делегований вхід не дає хибної `GraphInputError`, а порушення
  контракту переказів лишається помилкою й тоді, коли вершина ще й делегована;
- кандидати без пари не створюють ні вершин, ні ребер;
- результат без аналізу (`NOT_ANALYZED`) будує граф без делегованих ребер, а неповний аналіз не
  викидає знайдених зв'язків — неповноту несе `GraphCompleteness` (принцип V), не граф.
"""

import dataclasses
import json
from collections import defaultdict

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import (
    EdgeKind,
    EdgeRef,
    GraphCompleteness,
    GraphCompletenessStatus,
    GraphInputError,
    NodeRole,
)
from unmask.ingest.model import (
    NOT_ANALYZED_REASON,
    BuyersCompleteness,
    Completeness,
    DelegatedAnalysis,
    DelegatedLink,
    MissingReason,
    Transfer,
)

NAME = "g_delegated"
_EXPECTED = json.loads((GRAPH_FIXTURES / NAME / "expected.json").read_text(encoding="utf-8"))
_INGEST = json.loads((GRAPH_FIXTURES / NAME / "ingest.json").read_text(encoding="utf-8"))
W = _EXPECTED["wallets"]
DELEGATED, TRANSFER = EdgeKind.DELEGATED_BUY, EdgeKind.TRANSFER


def _edge_dict(e) -> dict:
    return {
        "kind": e.kind.value, "sender": e.sender, "receiver": e.receiver,
        "asset": None if e.asset is None else str(e.asset), "amount": e.amount,
        "decimals": e.decimals, "count": e.count, "first_slot": e.first_slot,
        "last_slot": e.last_slot, "first_time": e.first_time, "last_time": e.last_time,
        "refs": [{"signature": r.signature, "slot": r.slot, "instruction_path": r.instruction_path}
                 for r in e.refs],
    }


def _node_dict(n) -> dict:
    u, m = n.unexpanded, n.measures
    return {
        "address": n.address, "roles": sorted(r.value for r in n.roles), "depth": n.depth,
        "buyer_rank": n.buyer_rank, "address_type": n.address_type.value,
        "unexpanded": None if u is None else {
            "reason": u.reason.value, "counterparties_seen": u.counterparties_seen,
            "signatures_seen": u.signatures_seen, "signatures_truncated": u.signatures_truncated,
        },
        "measures": {
            "degree": m.degree, "unique_senders": m.unique_senders,
            "one_off_senders": m.one_off_senders, "one_off_share": m.one_off_share,
            "buyer_fanout": m.buyer_fanout, "median_to_buyers": m.median_to_buyers,
        },
    }


def _edges(graph, kind=None, sender=None, receiver=None):
    return [e for e in graph.edges
            if (kind is None or e.kind is kind)
            and (sender is None or e.sender == W[sender])
            and (receiver is None or e.receiver == W[receiver])]


def _with_delegated(result, links, unpaired=(), buyers=None):
    """Валідний `IngestResult` з іншим аналізом делегованих (і, за потреби, іншою повнотою покупців)."""
    buyers = result.completeness.buyers if buyers is None else buyers
    return dataclasses.replace(
        result,
        completeness=Completeness.derive(result.completeness.missing, buyers),
        delegated=DelegatedAnalysis.derive(tuple(links), tuple(unpaired), buyers),
    )


def _link(sig, slot, payer, receiver, block_time="auto") -> DelegatedLink:
    return DelegatedLink(
        signature=sig, slot=slot, block_time=1_759_400_000 + slot if block_time == "auto" else block_time,
        payer=W.get(payer, payer), receiver=W.get(receiver, receiver),
    )


def _t(sig, slot, sender, receiver, depth, amount=10**9) -> Transfer:
    return Transfer(signature=sig, slot=slot, block_time=1_759_400_000 + slot, instruction_path="0",
                    sender=W[sender], receiver=W[receiver], asset="sol", amount=amount, decimals=None,
                    depth=depth)


# --- golden ---------------------------------------------------------------------------------------


def test_g_delegated_matches_expected_golden():
    graph = build_graph(load_ingest_fixture(NAME))
    assert [_edge_dict(e) for e in graph.edges] == _EXPECTED["graph"]["edges"]
    assert [_node_dict(n) for n in graph.nodes] == _EXPECTED["graph"]["nodes"]


def test_g_delegated_expected_delegated_edges_equal_independent_recount_of_ingest_links():
    """Другий, незалежний від генератора перерахунок: лічильники за парою з `ingest.json`."""
    by_pair = defaultdict(list)
    for link in _INGEST["delegated"]["links"]:
        by_pair[(link["payer"], link["receiver"])].append(link)
    got = {(e["sender"], e["receiver"]): e for e in _EXPECTED["graph"]["edges"] if e["kind"] == "delegated_buy"}
    assert set(got) == set(by_pair)
    for pair, links in by_pair.items():
        e = got[pair]
        slots = [l["slot"] for l in links]
        assert e["count"] == len(links) == len(e["refs"])
        assert (e["first_slot"], e["last_slot"]) == (min(slots), max(slots))
        assert {(r["signature"], r["slot"]) for r in e["refs"]} == {(l["signature"], l["slot"]) for l in links}
        assert all(r["instruction_path"] is None for r in e["refs"])
        assert (e["asset"], e["amount"], e["decimals"]) == (None, None, None)
    # transfer-ребра не несуть жодного підпису делегованих зв'язків
    link_sigs = {l["signature"] for l in _INGEST["delegated"]["links"]}
    for e in _EXPECTED["graph"]["edges"]:
        if e["kind"] == "transfer":
            assert not link_sigs & {r["signature"] for r in e["refs"]}


def test_g_delegated_fixture_is_not_vacuous():
    """Сценарій справді містить усі три випадки T-047."""
    links = _INGEST["delegated"]["links"]
    pairs = [(l["payer"], l["receiver"]) for l in links]
    transfer_pairs = {(t["sender"], t["receiver"]) for t in _INGEST["transfers"]}
    assert set(pairs) & transfer_pairs, "немає пари з переказом і делегованою купівлею"
    assert any(pairs.count(p) == 2 for p in pairs), "немає двох зв'язків однієї пари"
    assert _INGEST["delegated"]["unpaired"], "немає кандидатів без пари"
    assert _INGEST["delegated"]["complete"] is True


# --- FR-002-18: окремий вид ребра -----------------------------------------------------------------


def test_same_pair_transfer_and_delegated_buy_are_two_edges():
    graph = build_graph(load_ingest_fixture(NAME))
    pair = _edges(graph, sender="A", receiver="R")
    assert sorted(e.kind.value for e in pair) == ["delegated_buy", "transfer"]
    transfer = next(e for e in pair if e.kind is TRANSFER)
    delegated = next(e for e in pair if e.kind is DELEGATED)
    # суми, лічильники й докази кожного виду — лише свої
    assert (transfer.asset, transfer.amount, transfer.count) == ("sol", 10**9, 1)
    assert [r.instruction_path for r in transfer.refs] == ["0"]
    assert (delegated.asset, delegated.amount, delegated.count) == (None, None, 1)
    assert [(r.slot, r.instruction_path) for r in delegated.refs] == [(215, None)]
    assert transfer.refs[0].signature != delegated.refs[0].signature
    assert (transfer.first_slot, delegated.first_slot) == (120, 215)
    # ключ унікальності — `(kind, sender, receiver, asset)` (data-model): вид входить у ключ явно, а не
    # лише через `asset is None`
    assert transfer.key == (TRANSFER, W["A"], W["R"], "sol")
    assert delegated.key == (DELEGATED, W["A"], W["R"], None)
    # пара з двома видами — один контрагент: degree не подвоюється, а відправником робить лише переказ
    r = graph.node(W["R"]).measures
    assert (r.degree, r.unique_senders, r.one_off_senders) == (1, 1, 1)


def test_same_pair_in_memory_two_links_and_two_transfers_stay_apart():
    """Без фікстури: дві делеговані й два перекази однієї пари — рівно два ребра, лічильники окремі."""
    base = load_ingest_fixture(NAME)
    result = dataclasses.replace(
        _with_delegated(base, [_link("dX1", 205, "A", "R"), _link("dX2", 206, "A", "R")]),
        transfers=(_t("tX1", 120, "A", "R", 1), _t("tX2", 121, "A", "R", 1, amount=5)),
    )
    graph = build_graph(result)
    pair = {e.kind: e for e in _edges(graph, sender="A", receiver="R")}
    assert set(pair) == {TRANSFER, DELEGATED} and len(graph.edges) == 2
    assert (pair[TRANSFER].count, pair[TRANSFER].amount) == (2, 10**9 + 5)
    assert pair[DELEGATED].count == 2 and pair[DELEGATED].amount is None
    assert [r.signature for r in pair[DELEGATED].refs] == ["dX1", "dX2"]
    assert [r.signature for r in pair[TRANSFER].refs] == ["tX1", "tX2"]


def test_two_links_same_pair_aggregate_into_one_delegated_edge_with_two_refs():
    graph = build_graph(load_ingest_fixture(NAME))
    [edge] = _edges(graph, sender="Q", receiver="V")
    assert edge.kind is DELEGATED
    links = sorted((l for l in load_ingest_fixture(NAME).delegated.links
                    if (l.payer, l.receiver) == (W["Q"], W["V"])), key=lambda l: l.slot)
    assert len(links) == 2
    assert edge.count == 2 and len(edge.refs) == 2
    assert edge.refs == tuple(EdgeRef(l.signature, l.slot, None) for l in links)
    assert (edge.first_slot, edge.last_slot) == (205, 225)
    # другий зв'язок без block_time: кінець лишається None, час сусіда не підставляється
    assert (edge.first_time, edge.last_time) == (1_759_400_205, None)


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_two_links_in_same_slot_ordered_by_signature_not_input(order):
    # `DelegatedAnalysis` сам упорядковує зв'язки, тож порядок входу підміняється в обхід нього:
    # побудова не має покладатися на канонічний порядок контейнера (FR-002-04).
    links = [_link("sigB", 300, "Q", "V", block_time=1), _link("sigA", 300, "Q", "V", block_time=2)]
    graph = build_graph(_forced_links(*(links if order == "forward" else links[::-1])))
    [edge] = _edges(graph, kind=DELEGATED, sender="Q", receiver="V")
    assert [r.signature for r in edge.refs] == ["sigA", "sigB"]
    assert (edge.first_time, edge.last_time) == (2, 1)


def test_delegated_edge_has_null_asset_amount_decimals_and_path():
    graph = build_graph(load_ingest_fixture(NAME))
    delegated = _edges(graph, kind=DELEGATED)
    assert len(delegated) == 3
    for e in delegated:
        assert (e.asset, e.amount, e.decimals) == (None, None, None)
        assert all(r.instruction_path is None for r in e.refs)
        assert e.count == len(e.refs) >= 1
    # порядок ребер: kind рядком — delegated_buy < transfer
    kinds = [e.kind.value for e in graph.edges]
    assert kinds == sorted(kinds) and kinds[0] == "delegated_buy"


# --- вершини: ролі й глибина (R-6) ----------------------------------------------------------------


def test_payer_and_receiver_roles_and_depths():
    graph = build_graph(load_ingest_fixture(NAME))

    def view(label):
        n = graph.node(W[label])
        return sorted(r.value for r in n.roles), n.depth, n.buyer_rank

    assert view("V") == (["delegated_receiver"], 0, None)      # вершина лише з делегованих ребер
    assert view("W") == (["delegated_receiver"], 0, None)
    assert view("Q") == (["delegated_payer", "funder"], 1, None)  # min(відправник d2, платник 1) = 1
    assert view("A") == (["delegated_payer", "funder"], 1, None)
    assert view("R") == (["buyer", "delegated_receiver"], 0, 3)
    assert view("P3") == (["buyer", "delegated_payer"], 0, 4)   # покупець лишається на 0
    assert view("S") == (["funder"], 1, None)
    # тип адреси не-покупця обчислено, як і для решти вершин
    assert graph.node(W["V"]).address_type.value == "wallet"


def test_payer_only_node_has_depth_one_and_receiver_only_node_depth_zero_without_input_error():
    base = load_ingest_fixture(NAME)
    graph = build_graph(dataclasses.replace(
        _with_delegated(base, [_link("d1", 205, "U1", "U2")]), transfers=()))
    assert sorted((sorted(r.value for r in graph.node(W[x]).roles), graph.node(W[x]).depth)
                  for x in ("U1", "U2")) == [(["delegated_payer"], 1), (["delegated_receiver"], 0)]
    # без переказів: покупці + рівно дві делеговані вершини, одне ребро
    assert len(graph.nodes) == len(base.buyers) + 2 and len(graph.edges) == 1


def test_delegated_receiver_that_is_also_a_depth_one_funder_gets_depth_zero_without_input_error():
    """Отримувач (<= 0) і відправник переказу покупцю (<= 1): мінімум 0, не «суперечність глибини»."""
    base = load_ingest_fixture(NAME)
    result = dataclasses.replace(_with_delegated(base, [_link("d1", 205, "Q", "V")]),
                                 transfers=(_t("t1", 120, "V", "P1", 1),))
    node = build_graph(result).node(W["V"])
    assert sorted(r.value for r in node.roles) == ["delegated_receiver", "funder"]
    assert node.depth == 0 and node.buyer_rank is None


def test_delegated_payer_that_is_a_buyer_stays_at_depth_zero():
    base = load_ingest_fixture(NAME)
    graph = build_graph(_with_delegated(base, [_link("d1", 205, "P1", "V")]))
    node = graph.node(W["P1"])
    assert sorted(r.value for r in node.roles) == ["buyer", "delegated_payer"] and node.depth == 0


def test_transfer_depth_contradiction_still_raises_when_node_is_also_delegated_receiver():
    """Переказ глибини 1 не-покупцю — порушення контракту 001, роль отримувача його не легалізує."""
    base = load_ingest_fixture(NAME)
    # V ще й надсилає переказ (роль funder є), тож спрацьовує саме правило глибини, а не «без ролі»
    result = dataclasses.replace(_with_delegated(base, [_link("d1", 205, "Q", "V")]),
                                 transfers=(_t("t1", 120, "S", "V", 1), _t("t2", 121, "V", "P1", 1)))
    with pytest.raises(GraphInputError, match="contradictory depth"):
        build_graph(result)


def test_transfer_receiver_without_transfer_role_still_raises_when_node_is_also_delegated_payer():
    """Отримувач переказу глибини 2, що не надсилає жодного переказу, — дефект 001 і для платника."""
    base = load_ingest_fixture(NAME)
    result = dataclasses.replace(_with_delegated(base, [_link("d1", 205, "V", "W")]),
                                 transfers=(_t("t1", 100, "S", "V", 2),))
    with pytest.raises(GraphInputError, match="neither a buyer nor a sender"):
        build_graph(result)


# --- кандидати без пари ---------------------------------------------------------------------------


def test_unpaired_candidates_do_not_create_nodes_or_edges():
    result = load_ingest_fixture(NAME)
    candidates = {c.wallet for c in result.delegated.unpaired}
    assert candidates == {W["U1"], W["U2"], W["U3"]}
    graph = build_graph(result)
    assert not candidates & {n.address for n in graph.nodes}
    assert not candidates & ({e.sender for e in graph.edges} | {e.receiver for e in graph.edges})
    unpaired_sigs = {c.signature for c in result.delegated.unpaired}
    assert not unpaired_sigs & {r.signature for e in graph.edges for r in e.refs}
    # граф той самий, що й без кандидатів узагалі
    assert build_graph(_with_delegated(result, result.delegated.links, ())) == graph


# --- повнота аналізу: граф не вгадує ---------------------------------------------------------------


def test_not_analyzed_result_builds_graph_without_delegated_edges():
    full = load_ingest_fixture(NAME)
    result = dataclasses.replace(full, delegated=DelegatedAnalysis.NOT_ANALYZED)
    graph = build_graph(result)
    assert not _edges(graph, kind=DELEGATED)
    assert all(not ({NodeRole.DELEGATED_PAYER, NodeRole.DELEGATED_RECEIVER} & n.roles) for n in graph.nodes)
    addresses = {n.address for n in graph.nodes}
    assert W["V"] not in addresses and W["W"] not in addresses  # вершини лише з делегованих ребер зникли
    assert [_edge_dict(e) for e in graph.edges] == [
        e for e in _EXPECTED["graph"]["edges"] if e["kind"] == "transfer"]
    # «не аналізували» — це неповнота графа, а не «делегованих зв'язків немає»
    gc = GraphCompleteness.derive(result)
    assert gc.status is GraphCompletenessStatus.INCOMPLETE
    assert (gc.delegated_complete, gc.delegated_reason) == (False, NOT_ANALYZED_REASON)


def test_incomplete_analysis_keeps_found_links_as_edges_and_reports_incompleteness():
    """Неповний аналіз (обірване перелічення) не викидає знайдених доказів; неповноту несе `derive`."""
    full = load_ingest_fixture(NAME)
    broken = BuyersCompleteness(complete=False, reason=MissingReason.TIMEOUT, detail="getSignaturesForAddress(mint)")
    result = _with_delegated(full, full.delegated.links, full.delegated.unpaired, buyers=broken)
    graph = build_graph(result)
    assert graph == build_graph(full)
    gc = GraphCompleteness.derive(result)
    assert gc.status is GraphCompletenessStatus.INCOMPLETE
    assert (gc.delegated_complete, gc.delegated_reason) == (False, "timeout")


# --- перевірки входу (подвійні: `DelegatedAnalysis` це вже не пропускає) ---------------------------


def _forced_links(*links):
    result = load_ingest_fixture(NAME)
    object.__setattr__(result.delegated, "links", tuple(links))
    return result


def test_forced_duplicate_link_signature_is_graph_input_error():
    a = _link("dup", 205, "Q", "V")
    b = _link("dup", 206, "A", "R")
    with pytest.raises(GraphInputError, match="duplicate delegated link"):
        build_graph(_forced_links(a, b))


def test_forced_self_link_is_graph_input_error():
    link = _link("d1", 205, "Q", "V")
    object.__setattr__(link, "receiver", link.payer)
    with pytest.raises(GraphInputError, match="self-link"):
        build_graph(_forced_links(link))
