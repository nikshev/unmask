# verifies: FR-002-07
"""Критерії-джерела хаба: «відомий список / PDA» і «позначена збором» (T-034; research R-11, R-15, R-9, R-22;
contracts/graph-service.md §4).

Критична задача: тут помилка не ламає збірку, а тихо змінює, хто хаб. Тому, крім щасливого шляху:

- `known_list` за списком — лише коли `config.lists` не `None`; `lists_version` — з конфігу, не константа;
  `lists=None` вимикає саме критерій за списком, а не `off_curve` і не `ingest_high_degree`;
- `known_list` за типом адреси — лише з `prune_off_curve`; тип береться з вершини (`ingest`/`build`), не
  перераховується з адреси;
- `ingest_high_degree` — лише з `prune_ingest_high_degree` і лише для `reason == high_degree`; поріг — аргумент
  `ingest_counterparty_threshold` (метадані збору 001), не конфіг; `signature_cap` — ніколи, хоч би який
  `counterparties_seen`; позначка, що суперечить порогу збору (`counterparties_seen <= threshold`), — порушення
  контракту 001 → `GraphInputError` (не голий `ValueError` з `CriterionHit`);
- незалежність критеріїв: кожен спрацьовує сам на окремій вершині; X з `g_buyer_hub` має всі п'ять — у порядку
  рядка `criterion` (нічия двох `known_list` — за `detail`, як в оракулі генератора);
- еталони — незалежний оракул генератора фікстур (`expected.json` усіх сценаріїв графа) з повним конфігом
  сценарію (списки й перемикачі увімкнені); golden — проти зафіксованих `config/hubs.yaml` і
  `config/hub_addresses.yaml` (принцип III).
"""

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from unmask.graph.model import GraphInputError, HubCriterion, Node, NodeMeasures, NodeRole, UnexpandedMark
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.hubs.criteria import CriterionHit, evaluate
from unmask.ingest.model import AddressType, UnexpandedReason

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "graph"
SCENARIOS = sorted(p.name for p in FIXTURES.iterdir() if (p / "expected.json").is_file())

A = "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq"  # звичайний ключ на кривій
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # launchpads у config/hub_addresses.yaml v1
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"  # dex_routers
INGEST_THRESHOLD = 200

DEGREE = HubCriterion.DEGREE
ONE_OFF = HubCriterion.ONE_OFF_SENDERS
INGEST = HubCriterion.INGEST_HIGH_DEGREE
DUST = HubCriterion.DUST_FANOUT
KNOWN = HubCriterion.KNOWN_LIST

OFF_CURVE = "address_type:off_curve"
HIGH_DEGREE = "unexpanded:high_degree"


# --- Будівельники ------------------------------------------------------------------


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _config(*, lists: AddressLists | None = None, **changes) -> HubConfig:
    """Конфіг у пам'яті: критерії за вимірами далеко від тестових вершин, критерії-джерела ввімкнені."""
    kw = dict(
        version=2,
        degree_threshold=100,
        one_off_senders_share=0.8,
        one_off_min_senders=10,
        giant_component_warn_share=0.5,
        prune_off_curve=True,
        prune_ingest_high_degree=True,
        dust_amount_lamports=1_000_000,
        dust_min_fanout=5,
    )
    kw.update(changes)
    return HubConfig(
        thresholds=HubThresholds(**kw),
        lists=lists,
        thresholds_digest="0" * 64,
        lists_digest=None if lists is None else "1" * 64,
    )


def _mark(reason=UnexpandedReason.HIGH_DEGREE, counterparties_seen=INGEST_THRESHOLD + 1, *, signatures_seen=None,
          signatures_truncated=False) -> UnexpandedMark:
    return UnexpandedMark(
        reason=reason,
        counterparties_seen=counterparties_seen,
        signatures_seen=counterparties_seen if signatures_seen is None else signatures_seen,
        signatures_truncated=signatures_truncated,
    )


def _node(*, address=A, address_type=AddressType.WALLET, unexpanded=None, roles=(NodeRole.FUNDER,)) -> Node:
    roles = frozenset(roles)
    is_buyer = NodeRole.BUYER in roles
    return Node(
        address=address,
        roles=roles,
        depth=0 if is_buyer else 1,
        buyer_rank=1 if is_buyer else None,
        address_type=address_type,
        unexpanded=unexpanded,
        measures=NodeMeasures(degree=1, unique_senders=1, one_off_senders=1, one_off_share=1.0,
                              buyer_fanout=0, median_to_buyers=None),
    )


def _eval(node, config, threshold=INGEST_THRESHOLD):
    return evaluate(node, config, ingest_counterparty_threshold=threshold)


def _known_list(category: str, version: int) -> CriterionHit:
    return CriterionHit(KNOWN, measured=None, threshold=None, detail=f"list:{category}", lists_version=version)


def _off_curve() -> CriterionHit:
    return CriterionHit(KNOWN, measured=None, threshold=None, detail=OFF_CURVE, lists_version=None)


def _ingest(measured: int, threshold: int) -> CriterionHit:
    return CriterionHit(INGEST, measured=measured, threshold=threshold, detail=HIGH_DEGREE, lists_version=None)


# --- Фікстури графа: вершини, конфіг і еталони з expected.json ----------------------


def _expected(scenario: str) -> dict:
    return json.loads((FIXTURES / scenario / "expected.json").read_text())


def _node_from_dict(d: dict) -> Node:
    unexpanded = d["unexpanded"]
    return Node(
        address=d["address"],
        roles=frozenset(NodeRole(r) for r in d["roles"]),
        depth=d["depth"],
        buyer_rank=d["buyer_rank"],
        address_type=AddressType(d["address_type"]),
        unexpanded=None if unexpanded is None else UnexpandedMark(**unexpanded),
        measures=NodeMeasures(**d["measures"]),
    )


def _scenario_config(expected: dict, *, with_lists: bool = True, **switches) -> HubConfig:
    """Повний конфіг сценарію з `expected.json` (пороги, перемикачі, списки); перемикачі можна перевизначити."""
    raw = dict(expected["config"]["thresholds"])
    raw.update(switches)
    lists = expected["config"]["lists"]
    return _config(
        lists=_lists(lists["version"], **lists["categories"]) if with_lists else None,
        **{"version": expected["config"]["version"], **raw},
    )


def _oracle_hits(expected: dict) -> dict[str, list[tuple]]:
    prune = expected["prune"]
    return {
        item["address"]: [
            (c["criterion"], c["measured"], c["threshold"], c["detail"], c["lists_version"]) for c in item["criteria"]
        ]
        for item in [*prune["records"], *prune["buyer_flags"]]
    }


def _as_tuples(hits) -> list[tuple]:
    return [(h.criterion.value, h.measured, h.threshold, h.detail, h.lists_version) for h in hits]


def _scenario_hits(scenario: str, **config_kw) -> tuple[dict, dict[str, tuple]]:
    expected = _expected(scenario)
    config = _scenario_config(expected, **config_kw)
    hits = {
        n["address"]: evaluate(_node_from_dict(n), config,
                               ingest_counterparty_threshold=expected["ingest_counterparty_threshold"])
        for n in expected["graph"]["nodes"]
    }
    return expected, hits


def _scenario_node(expected: dict, name: str) -> Node:
    address = expected["wallets"][name]
    return _node_from_dict(next(n for n in expected["graph"]["nodes"] if n["address"] == address))


# --- known_list за списком ------------------------------------------------------------


def test_address_in_launchpads_list_hits_known_list_with_category_and_lists_version():
    expected, hits = _scenario_hits("g_known")
    pump = expected["wallets"]["PUMP"]
    assert hits[pump] == (_known_list("launchpads", 1),)
    assert _as_tuples(hits[pump]) == _oracle_hits(expected)[pump]


def test_list_hit_category_and_version_come_from_config_lists_not_constants():
    node = _node(address=JUP)
    assert _eval(node, _config(lists=_lists(7, dex_routers=[JUP]))) == (_known_list("dex_routers", 7),)
    assert _eval(node, _config(lists=_lists(3, exchanges=[JUP]))) == (_known_list("exchanges", 3),)
    assert _eval(node, _config(lists=_lists(3, exchanges=[PUMP]))) == ()  # інша адреса в списку — не хіт


@pytest.mark.parametrize("category", ADDRESS_CATEGORIES)
def test_every_category_is_reported_by_name(category):
    assert _eval(_node(address=JUP), _config(lists=_lists(2, **{category: [JUP]}))) == (_known_list(category, 2),)


def test_shipped_lists_file_drives_list_hits():
    """Golden: зафіксований `config/hub_addresses.yaml` (version 1) — Pump.fun у `launchpads`, Jupiter у `dex_routers`."""
    config = load_hub_config(ROOT / "config/hubs.yaml", ROOT / "config/hub_addresses.yaml")
    assert config.lists is not None and config.lists.version == 1
    assert _eval(_node(address=PUMP), config) == (_known_list("launchpads", 1),)
    assert _eval(_node(address=JUP), config) == (_known_list("dex_routers", 1),)
    assert _eval(_node(address=A), config) == ()


def test_lists_none_gives_no_list_hit_but_off_curve_and_ingest_still_fire():
    """`lists=None` — «списки не завантажено», а не «хабів немає»: вимкнено лише критерій за списком (FR-002-12)."""
    expected, hits = _scenario_hits("g_known", with_lists=False)
    w = expected["wallets"]
    assert hits[w["PUMP"]] == ()
    assert hits[w["PDA_SRC"]] == (_off_curve(),)
    assert all(not h.detail.startswith("list:") for hs in hits.values() for h in hs)

    node = _node(address=PUMP, unexpanded=_mark())
    assert _eval(node, _config(lists=None)) == (_ingest(INGEST_THRESHOLD + 1, INGEST_THRESHOLD),)


def test_empty_categories_give_no_list_hit_and_no_error():
    assert _eval(_node(address=PUMP), _config(lists=_lists(3))) == ()


def test_buyer_in_list_gets_hit_too():
    """`evaluate` не знає про захист покупців: «лишити з позначкою» вирішує `prune` (FR-002-10)."""
    node = _node(address=PUMP, roles=(NodeRole.BUYER,))
    assert _eval(node, _config(lists=_lists(1, launchpads=[PUMP]))) == (_known_list("launchpads", 1),)


# --- known_list за типом адреси (PDA) ----------------------------------------------


def test_off_curve_node_hits_known_list_with_address_type_detail_without_lists_version():
    expected, hits = _scenario_hits("g_known")
    pda = expected["wallets"]["PDA_SRC"]
    assert hits[pda] == (_off_curve(),)
    assert hits[pda][0].lists_version is None
    assert _as_tuples(hits[pda]) == _oracle_hits(expected)[pda]
    # Навіть коли списки завантажено з іншою версією — у хіті за типом адреси версії списків немає.
    assert _eval(_node(address_type=AddressType.OFF_CURVE), _config(lists=_lists(9))) == (_off_curve(),)


def test_off_curve_not_hit_when_prune_off_curve_false():
    expected, hits = _scenario_hits("g_known", prune_off_curve=False)
    w = expected["wallets"]
    assert hits[w["PDA_SRC"]] == ()
    assert hits[w["PUMP"]] == (_known_list("launchpads", 1),)  # перемикач не чіпає список
    assert _eval(_node(address_type=AddressType.OFF_CURVE), _config(prune_off_curve=False)) == ()


def test_address_type_is_taken_from_node_not_recomputed_from_address():
    """Тип адреси дають `ingest`/`build` (R-11); `evaluate` не перераховує його з рядка адреси."""
    expected = _expected("g_known")
    pda_address = expected["wallets"]["PDA_SRC"]  # справді поза кривою
    assert _eval(_node(address=pda_address, address_type=AddressType.WALLET), _config()) == ()
    assert _eval(_node(address=A, address_type=AddressType.OFF_CURVE), _config()) == (_off_curve(),)


def test_off_curve_address_in_list_gets_both_known_list_hits_ordered_by_detail():
    """Два джерела одного критерію — два хіти (обидва видимі в записі, R-11); нічия за `detail`, як в оракулі."""
    node = _node(address=PUMP, address_type=AddressType.OFF_CURVE)
    hits = _eval(node, _config(lists=_lists(4, launchpads=[PUMP])))
    assert hits == (_off_curve(), _known_list("launchpads", 4))


# --- ingest_high_degree --------------------------------------------------------------


def test_high_degree_unexpanded_hits_ingest_high_degree_with_counterparties_and_ingest_threshold():
    expected, hits = _scenario_hits("g_unexpanded")
    uh = expected["wallets"]["UH"]
    assert expected["ingest_counterparty_threshold"] == 3
    assert hits[uh] == (_ingest(4, 3),)
    assert _as_tuples(hits[uh]) == _oracle_hits(expected)[uh]


@pytest.mark.parametrize("threshold", [1, 12, 200])
def test_ingest_threshold_is_the_argument_and_one_above_is_a_hit(threshold):
    """Поріг хіта — `ingest_counterparty_threshold` (метадані 001), не значення конфігу; 001 позначає `поріг + 1`."""
    node = _node(unexpanded=_mark(counterparties_seen=threshold + 1))
    assert _eval(node, _config(), threshold) == (_ingest(threshold + 1, threshold),)
    far = _node(unexpanded=_mark(counterparties_seen=threshold + 1000))
    assert _eval(far, _config(), threshold) == (_ingest(threshold + 1000, threshold),)


def test_high_degree_with_truncated_signatures_still_hits():
    """Обидва види обрізання в одній вершині: 001 ставить `reason=high_degree`, `signatures_truncated=True`."""
    node = _node(unexpanded=_mark(counterparties_seen=INGEST_THRESHOLD + 1, signatures_seen=1000,
                                  signatures_truncated=True))
    assert _eval(node, _config()) == (_ingest(INGEST_THRESHOLD + 1, INGEST_THRESHOLD),)


def test_signature_cap_unexpanded_is_not_a_hit():
    expected, hits = _scenario_hits("g_unexpanded")
    us = expected["wallets"]["US"]
    assert _scenario_node(expected, "US").unexpanded.reason is UnexpandedReason.SIGNATURE_CAP
    assert hits[us] == ()
    assert {a for a, h in hits.items() if h} == {expected["wallets"]["UH"]}
    # Довга історія — не багато контрагентів (R-15): навіть з великим `counterparties_seen` хіта немає.
    for seen in (0, INGEST_THRESHOLD, INGEST_THRESHOLD + 1, 10 * INGEST_THRESHOLD):
        node = _node(unexpanded=_mark(UnexpandedReason.SIGNATURE_CAP, seen, signatures_seen=300,
                                      signatures_truncated=True))
        assert _eval(node, _config()) == (), seen


def test_ingest_high_degree_disabled_by_config_flag():
    expected, hits = _scenario_hits("g_unexpanded", prune_ingest_high_degree=False)
    assert all(h == () for h in hits.values())
    node = _node(unexpanded=_mark())
    assert _eval(node, _config(prune_ingest_high_degree=False)) == ()
    assert _eval(node, _config(prune_ingest_high_degree=True)) == (_ingest(INGEST_THRESHOLD + 1, INGEST_THRESHOLD),)


@pytest.mark.parametrize("seen", [0, 1, INGEST_THRESHOLD - 1, INGEST_THRESHOLD])
@pytest.mark.parametrize("enabled", [True, False])
def test_high_degree_mark_not_above_ingest_threshold_is_graph_input_error(seen, enabled):
    """Позначка `high_degree` при `counterparties_seen <= threshold` суперечить збору 001 (той позначає лише
    понад поріг) — порушення контракту на вході, `GraphInputError` з адресою й обома числами, не голий
    `ValueError` з `CriterionHit`. Перевіряється й за вимкненого перемикача: вимкнений критерій не ховає дефект
    входу (найчастіше — не той поріг у `ingest_counterparty_threshold`)."""
    node = _node(unexpanded=_mark(counterparties_seen=seen))
    with pytest.raises(GraphInputError) as info:
        _eval(node, _config(prune_ingest_high_degree=enabled))
    assert not isinstance(info.value, ValueError)
    message = str(info.value)
    assert A in message and "high_degree" in message
    assert f"counterparties_seen={seen}" in message and f"threshold={INGEST_THRESHOLD}" in message


# --- незалежність і порядок ---------------------------------------------------------


DEDICATED = [
    ("g_all_hubs", "S_DEG", DEGREE),
    ("g_dust", "D", DUST),
    ("g_all_hubs", "S_UNEXP", INGEST),
    ("g_known", "PUMP", KNOWN),
    ("g_hub", "H", ONE_OFF),
    ("g_all_hubs", "S_PDA", KNOWN),
]


@pytest.mark.parametrize("scenario, name, criterion", DEDICATED, ids=[f"{s}-{n}" for s, n, _ in DEDICATED])
def test_each_criterion_fires_alone_on_dedicated_node(scenario, name, criterion):
    expected, hits = _scenario_hits(scenario)
    address = expected["wallets"][name]
    assert [h.criterion for h in hits[address]] == [criterion]
    assert _as_tuples(hits[address]) == _oracle_hits(expected)[address]


def test_dedicated_nodes_cover_all_five_criteria():
    assert {c for _, _, c in DEDICATED} == set(HubCriterion)


def test_node_matching_all_five_gets_five_hits_in_criterion_order():
    expected, hits = _scenario_hits("g_buyer_hub")
    x = expected["wallets"]["X"]
    assert [h.criterion for h in hits[x]] == [DEGREE, DUST, INGEST, KNOWN, ONE_OFF]
    assert _as_tuples(hits[x]) == _oracle_hits(expected)[x]
    assert hits[x][2] == _ingest(13, 12)
    assert hits[x][3] == _off_curve()


def test_all_sources_in_g_all_hubs_each_hit_by_own_criterion():
    expected, hits = _scenario_hits("g_all_hubs")
    oracle = _oracle_hits(expected)
    assert {a for a, h in hits.items() if h} == set(oracle)
    for address, want in oracle.items():
        assert _as_tuples(hits[address]) == want, address


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_all_hits_match_oracle_on_every_scenario(scenario):
    """Повна рівність усіх хітів (усі п'ять критеріїв, списки й перемикачі сценарію) з незалежним оракулом."""
    expected, hits = _scenario_hits(scenario)
    oracle = _oracle_hits(expected)
    assert {a for a, h in hits.items() if h} == set(oracle)
    for address, got in hits.items():
        assert _as_tuples(got) == oracle.get(address, []), address


def _all_criteria_node() -> Node:
    """PUMP поза кривою, позначена `high_degree` (201 > 200), у `launchpads`; ще й degree, dust і one_off."""
    return Node(
        address=PUMP,
        roles=frozenset({NodeRole.FUNDER}),
        depth=1,
        buyer_rank=None,
        address_type=AddressType.OFF_CURVE,
        unexpanded=_mark(counterparties_seen=INGEST_THRESHOLD + 1),
        measures=NodeMeasures(degree=150, unique_senders=40, one_off_senders=39, one_off_share=39 / 40,
                              buyer_fanout=6, median_to_buyers=10),
    )


@pytest.mark.parametrize("with_lists", [True, False], ids=["lists", "no_lists"])
@pytest.mark.parametrize("ingest_on", [True, False], ids=["ingest_on", "ingest_off"])
@pytest.mark.parametrize("off_curve_on", [True, False], ids=["off_curve_on", "off_curve_off"])
def test_switches_and_lists_enable_exactly_their_own_hits(off_curve_on, ingest_on, with_lists):
    """Незалежність (T-034): кожен перемикач / наявність списків вмикає рівно свій хіт і не чіпає жодного іншого —
    ні інших критеріїв-джерел, ні критеріїв за вимірами. Очікування — повні `CriterionHit` у порядку сортування."""
    config = _config(
        lists=_lists(1, launchpads=[PUMP]) if with_lists else None,
        prune_off_curve=off_curve_on,
        prune_ingest_high_degree=ingest_on,
    )
    want = [
        CriterionHit(DEGREE, measured=150, threshold=100, detail="measured", lists_version=None),
        CriterionHit(DUST, measured=10, threshold=1_000_000, detail="measured", lists_version=None),
    ]
    if ingest_on:
        want.append(_ingest(INGEST_THRESHOLD + 1, INGEST_THRESHOLD))
    if off_curve_on:
        want.append(_off_curve())
    if with_lists:
        want.append(_known_list("launchpads", 1))
    want.append(CriterionHit(ONE_OFF, measured=39 / 40, threshold=0.8, detail="measured", lists_version=None))
    assert _eval(_all_criteria_node(), config) == tuple(want)


def test_hits_are_sorted_by_criterion_then_detail():
    node = Node(
        address=PUMP,
        roles=frozenset({NodeRole.FUNDER}),
        depth=1,
        buyer_rank=None,
        address_type=AddressType.OFF_CURVE,
        unexpanded=_mark(counterparties_seen=INGEST_THRESHOLD + 5),
        measures=NodeMeasures(degree=150, unique_senders=40, one_off_senders=39, one_off_share=39 / 40,
                              buyer_fanout=6, median_to_buyers=10),
    )
    hits = _eval(node, _config(lists=_lists(2, launchpads=[PUMP])))
    keys = [(h.criterion.value, h.detail) for h in hits]
    assert keys == sorted(keys)
    assert [h.criterion for h in hits] == [DEGREE, DUST, INGEST, KNOWN, KNOWN, ONE_OFF]
