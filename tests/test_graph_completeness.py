# verifies: FR-002-05, FR-002-06
"""Успадкування неповноти й нерозгорнутості графом (T-030; research R-6, R-16; принцип V).

FR-002-05: `GraphCompleteness.derive(result)` копіює `missing[]`, повноту перелічення покупців
і повноту аналізу делегованих купівель; граф над неповним збором ніколи не `complete`.
FR-002-06: нерозгорнуті вершини 001 (обидва види) зберігаються як атрибут вершини
(`Node.unexpanded`) і на повноту не впливають.

`Node.unexpanded` будується з T-028 (`Node` без нього не існує), тож тести FR-002-06 зелені
одразу; їх доведено мутаціями (звіт T-030). Поведінка `derive` над реальним
`DelegatedAnalysis` (а не над підставними об'єктами `test_graph_model`) — предмет цієї задачі.
"""

import dataclasses
import itertools
import json

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import (
    GraphCompleteness,
    GraphCompletenessStatus,
    MissingRef,
    UnexpandedMark,
)
from unmask.ingest.model import (
    NOT_ANALYZED_REASON,
    BuyersCompleteness,
    Completeness,
    CompletenessStatus,
    DelegatedAnalysis,
    MissingHistory,
    MissingReason,
    UnexpandedNode,
    UnexpandedReason,
)

FIXTURES = sorted(p.name for p in GRAPH_FIXTURES.iterdir() if (p / "expected.json").is_file())
BUYERS_OK = BuyersCompleteness(complete=True, reason=None, detail="")


def _expected(name: str) -> dict:
    return json.loads((GRAPH_FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def _ingest_doc(name: str) -> dict:
    doc = json.loads((GRAPH_FIXTURES / name / "ingest.json").read_text(encoding="utf-8"))
    return doc.get("result", doc)


def _view(gc: GraphCompleteness) -> dict:
    """Вигляд `GraphCompleteness` у полях схеми `graph-result.schema.json#/$defs/completeness`."""
    return {
        "status": gc.status.value,
        "ingest_status": gc.ingest_status.value,
        "missing": [
            {"wallet": m.wallet, "depth": m.depth, "reason": m.reason.value, "detail": m.detail}
            for m in gc.missing
        ],
        "buyers_complete": gc.buyers_complete,
        "buyers_reason": gc.buyers_reason,
        "delegated_complete": gc.delegated_complete,
        "delegated_reason": gc.delegated_reason,
    }


def _with(result, missing=None, buyers=None, delegated="same"):
    """Валідний `IngestResult` з іншою повнотою; аналіз делегованих — похідний від нових `buyers`."""
    missing = result.completeness.missing if missing is None else tuple(missing)
    buyers = result.completeness.buyers if buyers is None else buyers
    if delegated == "same":
        delegated = DelegatedAnalysis.derive(result.delegated.links, result.delegated.unpaired, buyers)
    return dataclasses.replace(
        result, completeness=Completeness.derive(missing, buyers), delegated=delegated
    )


def _miss(wallet: str, depth: int = 1, reason=MissingReason.TIMEOUT, detail="getSignaturesForAddress"):
    return MissingHistory(wallet=wallet, depth=depth, reason=reason, detail=detail)


# --- FR-002-05: успадкування неповноти ----------------------------------------------


def test_g_incomplete_yields_incomplete_status_with_same_missing_entries():
    result = load_ingest_fixture("g_incomplete")
    gc = GraphCompleteness.derive(result)
    expected = _expected("g_incomplete")["completeness"]
    assert expected["status"] == "incomplete"  # фікстура справді про неповноту
    assert gc.status is GraphCompletenessStatus.INCOMPLETE
    assert gc.ingest_status is CompletenessStatus.INCOMPLETE
    # Ті самі записи, поле в поле, у порядку 001 — не «кількість причин» і не перший запис.
    assert [(m.wallet, m.depth, m.reason, m.detail) for m in gc.missing] == [
        (m.wallet, m.depth, m.reason, m.detail) for m in result.completeness.missing
    ]
    assert all(type(m) is MissingRef for m in gc.missing)
    view = _view(gc)
    for key in ("status", "ingest_status", "missing", "buyers_complete", "buyers_reason"):
        assert view[key] == expected[key], key
    # Граф над неповним збором будується (неповнота — дані, не помилка) і повноти не додає.
    graph = build_graph(result)
    assert len(graph.buyers()) == result.metadata.wallets_analyzed


@pytest.mark.parametrize("name", FIXTURES)
def test_completeness_of_every_graph_fixture_matches_expected_and_ingest_delegated(name):
    gc = GraphCompleteness.derive(load_ingest_fixture(name))
    view = _view(gc)
    expected = _expected(name)["completeness"]
    # `expected.json` (T-026) несе п'ять полів повноти; delegated_* звіряються з `ingest.json`.
    for key in ("status", "ingest_status", "missing", "buyers_complete", "buyers_reason"):
        assert view[key] == expected[key], (name, key)
    delegated = _ingest_doc(name)["delegated"]
    assert (view["delegated_complete"], view["delegated_reason"]) == (delegated["complete"], delegated["reason"])


def test_single_missing_entry_is_enough_for_incomplete():
    base = load_ingest_fixture("g_basic")
    assert GraphCompleteness.derive(base).status is GraphCompletenessStatus.COMPLETE
    victim = base.buyers[-1].wallet
    for entry in (_miss(victim, depth=0), _miss(victim, depth=2, reason=MissingReason.CORRUPT_DATA, detail="")):
        result = _with(base, missing=(entry,), buyers=BUYERS_OK)
        # Перелічення покупців і аналіз делегованих повні — неповнота лише в одному записі.
        assert result.completeness.buyers.complete and result.delegated.complete
        gc = GraphCompleteness.derive(result)
        assert gc.status is GraphCompletenessStatus.INCOMPLETE
        assert gc.ingest_status is CompletenessStatus.INCOMPLETE
        assert (gc.buyers_complete, gc.delegated_complete) == (True, True)
        assert [(m.wallet, m.depth, m.reason, m.detail) for m in gc.missing] == [
            (entry.wallet, entry.depth, entry.reason, entry.detail)
        ]


def test_buyers_incomplete_alone_yields_incomplete():
    base = load_ingest_fixture("g_basic")
    for reason in MissingReason:
        cut = BuyersCompleteness(complete=False, reason=reason, detail="cursor")
        result = _with(base, missing=(), buyers=cut)
        gc = GraphCompleteness.derive(result)
        assert gc.missing == ()
        assert (gc.buyers_complete, gc.buyers_reason) == (False, reason.value)
        assert gc.ingest_status is CompletenessStatus.INCOMPLETE
        assert gc.status is GraphCompletenessStatus.INCOMPLETE
        # Делеговані дзеркалять покупців (той самий розбір) — причина та сама.
        assert (gc.delegated_complete, gc.delegated_reason) == (False, reason.value)
    # І навіть якщо делегований аналіз підставлено «повним» в обхід інваріанта 001 —
    # неповне перелічення покупців саме по собі робить граф неповним.
    cut = BuyersCompleteness(complete=False, reason=MissingReason.BUDGET_EXHAUSTED, detail="cursor")
    forced = _with(base, missing=(), buyers=cut)
    object.__setattr__(forced, "delegated", base.delegated)
    assert forced.delegated.complete is True
    gc = GraphCompleteness.derive(forced)
    assert gc.delegated_complete is True
    assert gc.status is GraphCompletenessStatus.INCOMPLETE


def test_not_analyzed_delegated_yields_incomplete_with_reason_not_analyzed():
    result = dataclasses.replace(load_ingest_fixture("g_basic"), delegated=DelegatedAnalysis.NOT_ANALYZED)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    gc = GraphCompleteness.derive(result)
    assert gc.ingest_status is CompletenessStatus.COMPLETE
    assert gc.missing == ()
    assert (gc.buyers_complete, gc.buyers_reason) == (True, None)
    assert gc.delegated_complete is False
    assert gc.delegated_reason == NOT_ANALYZED_REASON == "not_analyzed"
    assert type(gc.delegated_reason) is str
    assert gc.status is GraphCompletenessStatus.INCOMPLETE


def test_complete_ingest_with_complete_delegated_is_complete():
    result = load_ingest_fixture("g_basic")
    assert result.completeness.status is CompletenessStatus.COMPLETE and result.delegated.complete
    gc = GraphCompleteness.derive(result)
    assert gc.status is GraphCompletenessStatus.COMPLETE
    assert _view(gc) == {
        "status": "complete", "ingest_status": "complete", "missing": [],
        "buyers_complete": True, "buyers_reason": None,
        "delegated_complete": True, "delegated_reason": None,
    }


def test_status_complete_iff_no_missing_and_buyers_and_delegated_complete_over_valid_results():
    # Решітка над валідними `IngestResult` з реальним `DelegatedAnalysis` (не підставними
    # об'єктами): `complete` — лише в одній точці, і ніде граф над неповним не «повний».
    base = load_ingest_fixture("g_basic")
    wallets = [b.wallet for b in base.buyers]
    missings = ((), (_miss(wallets[0]),), (_miss(wallets[0]), _miss(wallets[1], 2, MissingReason.RATE_LIMITED)))
    buyerses = (BUYERS_OK,) + tuple(BuyersCompleteness(complete=False, reason=r, detail="x") for r in MissingReason)
    seen_complete = 0
    for missing, buyers, analyzed in itertools.product(missings, buyerses, (True, False)):
        result = _with(base, missing=missing, buyers=buyers,
                       delegated="same" if analyzed else DelegatedAnalysis.NOT_ANALYZED)
        gc = GraphCompleteness.derive(result)
        complete = not missing and buyers.complete and analyzed
        assert (gc.status is GraphCompletenessStatus.COMPLETE) is complete, (missing, buyers, analyzed)
        assert gc.ingest_status is result.completeness.status
        assert (gc.delegated_complete, gc.delegated_reason) == (
            result.delegated.complete, None if result.delegated.reason is None else str(result.delegated.reason)
        )
        if result.completeness.status is CompletenessStatus.INCOMPLETE or not result.delegated.complete:
            assert gc.status is GraphCompletenessStatus.INCOMPLETE
        seen_complete += complete
    assert seen_complete == 1


def test_derive_rejects_result_without_delegated_analysis_instead_of_guessing():
    # Після T-044 `IngestResult.delegated` обов'язковий (умовчання — NOT_ANALYZED у самому 001).
    # Результат, у якого поле зламане (None), — порушення контракту 001: гучно, а не «not_analyzed»
    # навздогад графом (друге джерело істини для умовчання).
    result = load_ingest_fixture("g_basic")
    object.__setattr__(result, "delegated", None)
    with pytest.raises(TypeError, match="delegated"):
        GraphCompleteness.derive(result)


# --- FR-002-06: нерозгорнуті вершини як атрибут ----------------------------------------


def test_unexpanded_high_degree_and_signature_cap_both_kept_as_node_attribute():
    result = load_ingest_fixture("g_unexpanded")
    assert {u.reason for u in result.unexpanded} == set(UnexpandedReason)  # фікстура має обидва види
    graph = build_graph(result)
    marks = {n.address: n.unexpanded for n in graph.nodes}
    by_wallet = {u.wallet: u for u in result.unexpanded}
    for wallet, u in by_wallet.items():
        mark = marks[wallet]
        assert type(mark) is UnexpandedMark
        assert (mark.reason, mark.counterparties_seen, mark.signatures_seen, mark.signatures_truncated) == (
            u.reason, u.counterparties_seen, u.signatures_seen, u.signatures_truncated
        )
    assert all(marks[a] is None for a in marks if a not in by_wallet)
    # Звірка з еталоном фікстури: атрибут кожної вершини поле в поле.
    expected = {n["address"]: n["unexpanded"] for n in _expected("g_unexpanded")["graph"]["nodes"]}
    actual = {
        a: None if m is None else {
            "reason": m.reason.value, "counterparties_seen": m.counterparties_seen,
            "signatures_seen": m.signatures_seen, "signatures_truncated": m.signatures_truncated,
        }
        for a, m in marks.items()
    }
    assert actual == expected


def test_unexpanded_does_not_affect_completeness():
    result = load_ingest_fixture("g_unexpanded")
    assert result.unexpanded
    gc = GraphCompleteness.derive(result)
    assert gc.status is GraphCompletenessStatus.COMPLETE
    assert gc.missing == ()
    # Ті самі дані без нерозгорнутих вершин — та сама повнота: нерозгорнутість — атрибут, не неповнота.
    assert GraphCompleteness.derive(dataclasses.replace(result, unexpanded=())) == gc
    # І навпаки: позначка нерозгорнутості на повному g_basic повноти не забирає.
    basic = load_ingest_fixture("g_basic")
    funder = next(t.sender for t in basic.transfers if t.depth == 1)
    marked = dataclasses.replace(basic, unexpanded=(
        UnexpandedNode(wallet=funder, depth=1, reason=UnexpandedReason.SIGNATURE_CAP,
                       counterparties_seen=1, signatures_seen=300, signatures_truncated=True),
    ))
    assert build_graph(marked).node(funder).unexpanded is not None
    assert GraphCompleteness.derive(marked) == GraphCompleteness.derive(basic)
    assert GraphCompleteness.derive(marked).status is GraphCompletenessStatus.COMPLETE
