# verifies: FR-002-07, FR-002-14
"""Критерії порогу й правило «рівно поріг» (T-033; research R-9, R-22 — напрямок; contracts/graph-service.md §4).

Критична задача: `>` замість `>=` (чи навпаки) не ламає збірку, а тихо відсікає або лишає вершину на межі. Тому
кожен поріг перевіряється у трьох точках (`поріг−1`, `поріг`, `поріг+1`), передумова — в обидва боки окремо від
порогу, а сам тип `CriterionHit` не дає сконструювати хіт на порозі (напрямок закодовано в типі).

Конфіг будується в пам'яті: значення порогів беруться з `HubConfig`, не з констант коду (принцип III). Критерії
`known_list`/`ingest_high_degree` у `evaluate` додає T-034, `dust_fanout` — T-057; тут — лише `degree` і
`one_off_senders` та інваріант типу.
"""

import dataclasses
from pathlib import Path

import pytest

from unmask.graph.model import HubCriterion, Node, NodeMeasures, NodeRole, UnexpandedMark
from unmask.hubs.config import HubConfig, HubThresholds, load_hub_config
from unmask.hubs.criteria import CriterionHit, evaluate
from unmask.ingest.model import AddressType, UnexpandedReason

ROOT = Path(__file__).resolve().parents[1]

A = "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq"
INGEST_THRESHOLD = 200

DEGREE = HubCriterion.DEGREE
ONE_OFF = HubCriterion.ONE_OFF_SENDERS
INGEST = HubCriterion.INGEST_HIGH_DEGREE
DUST = HubCriterion.DUST_FANOUT
KNOWN = HubCriterion.KNOWN_LIST


# --- Будівельники ------------------------------------------------------------------


def _config(**changes) -> HubConfig:
    """Конфіг у пам'яті; за умовчанням жоден критерій-джерело не ввімкнено і списків немає."""
    kw = dict(
        version=2,
        degree_threshold=100,
        one_off_senders_share=0.8,
        one_off_min_senders=10,
        giant_component_warn_share=0.5,
        prune_off_curve=False,
        prune_ingest_high_degree=False,
        dust_amount_lamports=1_000_000,
        dust_min_fanout=5,
    )
    kw.update(changes)
    return HubConfig(thresholds=HubThresholds(**kw), lists=None, thresholds_digest="0" * 64, lists_digest=None)


def _node(*, degree=0, unique=0, one_off=0, roles=(NodeRole.FUNDER,), **changes) -> Node:
    roles = frozenset(roles)
    is_buyer = NodeRole.BUYER in roles
    kw = dict(
        address=A,
        roles=roles,
        depth=0 if is_buyer else 1,
        buyer_rank=1 if is_buyer else None,
        address_type=AddressType.WALLET,
        unexpanded=None,
        measures=NodeMeasures(
            degree=degree,
            unique_senders=unique,
            one_off_senders=one_off,
            one_off_share=None if unique == 0 else one_off / unique,
            buyer_fanout=0,
            median_to_buyers=None,
        ),
    )
    kw.update(changes)
    return Node(**kw)


def _criteria(hits):
    return [h.criterion for h in hits]


# --- degree ------------------------------------------------------------------------


@pytest.mark.parametrize("threshold", [1, 10, 100])
@pytest.mark.parametrize("offset, expected", [(-1, False), (0, False), (1, True)])
def test_degree_below_at_and_above_threshold(threshold, offset, expected):
    degree = threshold + offset
    hits = evaluate(_node(degree=degree), _config(degree_threshold=threshold), ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert (DEGREE in _criteria(hits)) is expected
    if expected:
        (hit,) = hits
        assert hit == CriterionHit(DEGREE, measured=degree, threshold=threshold, detail="measured", lists_version=None)
    else:
        assert hits == ()


# --- one_off_senders ---------------------------------------------------------------


@pytest.mark.parametrize(
    "share_threshold, one_off, unique, expected",
    [
        (0.7, 6, 10, False),  # поріг − 1/10
        (0.7, 7, 10, False),  # рівно поріг: 7/10 == 0.7 — не хаб
        (0.7, 8, 10, True),  # поріг + 1/10
        (0.5, 5, 10, False),
        (0.5, 6, 10, True),
        (0.8, 8, 10, False),  # значення з config/hubs.yaml v2
        (0.8, 9, 10, True),
        (0.0, 0, 10, False),  # частка 0 при порозі 0 — не хаб
        (0.0, 1, 10, True),
        (1.0, 10, 10, False),  # поріг 1.0 — критерій ніколи не спрацьовує (частка ≤ 1)
        (0.25, 1, 4, False),  # 1/4 == 0.25 точно — рівно поріг (min_senders нижче знижено до 2)
        (0.25, 2, 4, True),
    ],
)
def test_one_off_share_below_at_and_above_threshold(share_threshold, one_off, unique, expected):
    config = _config(one_off_senders_share=share_threshold, one_off_min_senders=2)
    node = _node(unique=unique, one_off=one_off)
    hits = evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert (ONE_OFF in _criteria(hits)) is expected
    if expected:
        (hit,) = hits
        assert hit.criterion is ONE_OFF
        assert hit.measured == one_off / unique
        assert hit.measured == node.measures.one_off_share
        assert hit.threshold == share_threshold
        assert hit.detail == "measured"
        assert hit.lists_version is None
    else:
        assert hits == ()


@pytest.mark.parametrize("min_senders", [2, 10, 37])
@pytest.mark.parametrize("offset, applicable", [(-1, False), (0, True), (1, True)])
def test_one_off_precondition_inclusive_at_min_senders(min_senders, offset, applicable):
    unique = min_senders + offset
    # Частка 1.0 — максимально «біржова»; спрацювання вирішує лише передумова.
    config = _config(one_off_senders_share=0.5, one_off_min_senders=min_senders)
    hits = evaluate(_node(unique=unique, one_off=unique), config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert (ONE_OFF in _criteria(hits)) is applicable


def test_one_off_not_applicable_without_senders():
    """`unique_senders == 0` (`one_off_share is None`) — критерій не застосовний навіть при нульовій передумові."""
    config = _config(one_off_senders_share=0.0, one_off_min_senders=2)
    assert evaluate(_node(degree=5), config, ingest_counterparty_threshold=INGEST_THRESHOLD) == ()
    # HubThresholds у пам'яті не перевіряє межу `>= 2` (це робить завантажувач) — і тоді без відправників не хаб.
    lax = _config(one_off_senders_share=0.0, one_off_min_senders=0)
    assert evaluate(_node(degree=5), lax, ingest_counterparty_threshold=INGEST_THRESHOLD) == ()


def test_one_off_precondition_uses_unique_senders_not_degree():
    """Передумова — кількість унікальних відправників, а не ступінь (ступінь включає отримувачів)."""
    config = _config(one_off_senders_share=0.5, one_off_min_senders=10, degree_threshold=1000)
    node = _node(degree=50, unique=9, one_off=9)
    assert evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD) == ()


# --- значення з конфігу, не з констант ---------------------------------------------


def test_hit_carries_measured_and_threshold_from_config_not_constants():
    node = _node(degree=60, unique=20, one_off=19)  # частка 0.95
    first = evaluate(node, _config(degree_threshold=17, one_off_senders_share=0.6, one_off_min_senders=3),
                     ingest_counterparty_threshold=INGEST_THRESHOLD)
    second = evaluate(node, _config(degree_threshold=41, one_off_senders_share=0.9, one_off_min_senders=20),
                      ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert first == (
        CriterionHit(DEGREE, measured=60, threshold=17, detail="measured", lists_version=None),
        CriterionHit(ONE_OFF, measured=0.95, threshold=0.6, detail="measured", lists_version=None),
    )
    assert second == (
        CriterionHit(DEGREE, measured=60, threshold=41, detail="measured", lists_version=None),
        CriterionHit(ONE_OFF, measured=0.95, threshold=0.9, detail="measured", lists_version=None),
    )


def test_shipped_config_thresholds_drive_the_rule():
    """Golden-перевірка проти зафіксованого `config/hubs.yaml`: межа — значення з файла, рівно поріг не хаб."""
    config = load_hub_config(ROOT / "config/hubs.yaml", ROOT / "config/hub_addresses.yaml")
    t = config.thresholds
    assert (t.version, t.degree_threshold, t.one_off_senders_share, t.one_off_min_senders) == (2, 100, 0.8, 10)
    at = evaluate(_node(degree=t.degree_threshold), config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    above = evaluate(_node(degree=t.degree_threshold + 1), config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert DEGREE not in _criteria(at)
    assert [(h.criterion, h.measured, h.threshold) for h in above if h.criterion is DEGREE] == [(DEGREE, 101, 100)]
    share_at = evaluate(_node(unique=10, one_off=8), config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    share_above = evaluate(_node(unique=10, one_off=9), config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert ONE_OFF not in _criteria(share_at)
    assert ONE_OFF in _criteria(share_above)


# --- інваріант типу CriterionHit ---------------------------------------------------


@pytest.mark.parametrize("criterion", [DEGREE, INGEST])
@pytest.mark.parametrize("measured, threshold", [(100, 100), (99, 100), (0, 1), (1, 1)])
def test_criterion_hit_rejects_measured_not_above_threshold(criterion, measured, threshold):
    detail = "measured" if criterion is DEGREE else "unexpanded:high_degree"
    with pytest.raises(ValueError):
        CriterionHit(criterion, measured=measured, threshold=threshold, detail=detail, lists_version=None)


@pytest.mark.parametrize("measured, threshold", [(0.7, 0.7), (0.5, 0.7), (0.0, 0.0), (1.0, 1.0), (float("nan"), 0.5)])
def test_criterion_hit_rejects_share_not_above_threshold(measured, threshold):
    with pytest.raises(ValueError):
        CriterionHit(ONE_OFF, measured=measured, threshold=threshold, detail="measured", lists_version=None)


def test_criterion_hit_accepts_measured_above_threshold():
    assert CriterionHit(DEGREE, 101, 100, "measured", None).measured == 101
    assert CriterionHit(ONE_OFF, 0.9, 0.8, "measured", None).threshold == 0.8
    assert CriterionHit(INGEST, 201, 200, "unexpanded:high_degree", None).detail == "unexpanded:high_degree"
    assert CriterionHit("degree", 2, 1, "measured", None).criterion is DEGREE  # рядок приводиться до перелічення


@pytest.mark.parametrize("measured, threshold", [(1_000_000, 1_000_000), (1_000_001, 1_000_000), (5, 1)])
def test_criterion_hit_rejects_dust_measured_not_below_threshold(measured, threshold):
    """Для `dust_fanout` хіт на порозі чи вище нього не конструюється (напрямок «менше» — R-22)."""
    with pytest.raises(ValueError):
        CriterionHit(DUST, measured=measured, threshold=threshold, detail="measured", lists_version=None)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(criterion=DEGREE, measured=True, threshold=0, detail="measured", lists_version=None),  # bool — не int
        dict(criterion=DEGREE, measured=101.0, threshold=100, detail="measured", lists_version=None),
        dict(criterion=DEGREE, measured=101, threshold=None, detail="measured", lists_version=None),
        dict(criterion=DEGREE, measured=None, threshold=100, detail="measured", lists_version=None),
        dict(criterion=ONE_OFF, measured="0.9", threshold=0.8, detail="measured", lists_version=None),
        dict(criterion=INGEST, measured=201, threshold=200.0, detail="unexpanded:high_degree", lists_version=None),
    ],
)
def test_criterion_hit_rejects_wrong_types(kwargs):
    with pytest.raises(TypeError):
        CriterionHit(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(criterion=DEGREE, measured=101, threshold=100, detail="list:exchanges", lists_version=None),
        dict(criterion=DEGREE, measured=101, threshold=100, detail="measured", lists_version=3),
        dict(criterion=ONE_OFF, measured=0.9, threshold=0.8, detail="unexpanded:high_degree", lists_version=None),
        dict(criterion=INGEST, measured=201, threshold=200, detail="measured", lists_version=None),
        dict(criterion=KNOWN, measured=None, threshold=None, detail="measured", lists_version=None),
        dict(criterion=KNOWN, measured=None, threshold=None, detail="list:exchanges", lists_version=None),
        dict(criterion=KNOWN, measured=None, threshold=None, detail="list:casinos", lists_version=1),
        dict(criterion=KNOWN, measured=None, threshold=None, detail="address_type:off_curve", lists_version=1),
        dict(criterion=KNOWN, measured=5, threshold=None, detail="address_type:off_curve", lists_version=None),
        dict(criterion=KNOWN, measured=None, threshold=0, detail="address_type:off_curve", lists_version=None),
        dict(criterion=KNOWN, measured=None, threshold=None, detail="list:exchanges", lists_version=0),
        dict(criterion="signature_cap", measured=None, threshold=None, detail="measured", lists_version=None),
    ],
)
def test_criterion_hit_rejects_inconsistent_detail_and_lists_version(kwargs):
    with pytest.raises(ValueError):
        CriterionHit(**kwargs)


def test_criterion_hit_known_list_shapes_from_data_model():
    """Форма `known_list` за data-model (сам критерій у `evaluate` додає T-034)."""
    listed = CriterionHit(KNOWN, None, None, "list:exchanges", 3)
    pda = CriterionHit(KNOWN, None, None, "address_type:off_curve", None)
    assert (listed.lists_version, pda.lists_version) == (3, None)


def test_criterion_hit_cannot_be_moved_onto_threshold_via_replace():
    hit = CriterionHit(DEGREE, 101, 100, "measured", None)
    with pytest.raises(ValueError):
        dataclasses.replace(hit, measured=100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.measured = 100  # type: ignore[misc]


# --- evaluate: загальні властивості ------------------------------------------------


def test_no_hits_for_plain_wallet():
    node = _node(degree=3, unique=2, one_off=2)  # частка 1.0, але відправників < 10
    assert evaluate(node, _config(), ingest_counterparty_threshold=INGEST_THRESHOLD) == ()


def test_no_hits_for_isolated_node():
    assert evaluate(_node(), _config(degree_threshold=1), ingest_counterparty_threshold=INGEST_THRESHOLD) == ()


def test_hits_independent_and_ordered_by_criterion_string():
    node = _node(degree=150, unique=40, one_off=39)
    hits = evaluate(node, _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert _criteria(hits) == [DEGREE, ONE_OFF]
    assert [h.criterion.value for h in hits] == sorted(h.criterion.value for h in hits)
    # Кожен окремо — незалежно від іншого.
    only_degree = evaluate(_node(degree=150, unique=40, one_off=1), _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)
    only_share = evaluate(_node(degree=40, unique=40, one_off=39), _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert _criteria(only_degree) == [DEGREE]
    assert _criteria(only_share) == [ONE_OFF]


def test_buyer_gets_hits_too():
    """`evaluate` не знає про захист покупців: рішення «не відсікати» — у `prune` (FR-002-10)."""
    node = _node(degree=150, roles=(NodeRole.BUYER,))
    assert _criteria(evaluate(node, _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)) == [DEGREE]


def test_returns_tuple_and_is_pure():
    node = _node(degree=150)
    config = _config()
    first = evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    second = evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert isinstance(first, tuple)
    assert first == second


def test_ingest_counterparty_threshold_is_keyword_only_and_validated():
    node, config = _node(), _config()
    with pytest.raises(TypeError):
        evaluate(node, config, INGEST_THRESHOLD)  # type: ignore[misc]
    for bad in (True, 1.5, "200", None):
        with pytest.raises(TypeError):
            evaluate(node, config, ingest_counterparty_threshold=bad)
    with pytest.raises(ValueError):
        evaluate(node, config, ingest_counterparty_threshold=0)


def test_rejects_wrong_input_types():
    with pytest.raises(TypeError):
        evaluate(object(), _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)
    with pytest.raises(TypeError):
        evaluate(_node(), _config().thresholds, ingest_counterparty_threshold=INGEST_THRESHOLD)


def test_unexpanded_mark_does_not_change_measured_criteria_when_source_flags_off():
    """Нерозгорнутість при вимкнених прапорцях критеріїв-джерел не дає хітів і не змінює хіти за вимірами."""
    mark = UnexpandedMark(UnexpandedReason.HIGH_DEGREE, counterparties_seen=201, signatures_seen=10,
                          signatures_truncated=False)
    node = _node(degree=150, unexpanded=mark)
    base = evaluate(node, _config(), ingest_counterparty_threshold=INGEST_THRESHOLD)
    assert _criteria(base) == [DEGREE]
