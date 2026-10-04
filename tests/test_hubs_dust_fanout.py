# verifies: FR-002-22, FR-002-07, FR-002-14
"""Критерій пилового роздавання `dust_fanout` (T-057; research R-22; calibration.md; contracts/graph-service.md §4).

Критична задача: `<` замість `<=` (чи `>` замість `>=` у передумові) не ламає збірку, а тихо відсікає справжнього
фінансиста або лишає пилове джерело, яке склеює покупців в одну компоненту. Тому:

- поріг суми — у трьох точках (`поріг−1` хаб, `поріг` не хаб, `поріг+1` не хаб) для кількох порогів, зокрема
  `dust_amount_lamports = 1` (критерій вимкнено: сума ребра ≥ 1);
- передумова fan-out — включно, в обидва боки окремо від порогу;
- напрямок «строго менше» закодовано в типі `CriterionHit` (хіт на порозі не конструюється);
- значення — з `HubConfig` у пам'яті і з зафіксованого `config/hubs.yaml` (golden, принцип III), не з констант коду;
- еталони — незалежний оракул генератора фікстур (`expected.json` усіх сценаріїв графа).

Критерії-джерела `known_list`/`ingest_high_degree` у `evaluate` додає T-034; тут вони вимкнені (`lists=None`,
перемикачі `false`), і з еталонами порівнюються лише критерії за вимірами (`degree`, `dust_fanout`,
`one_off_senders`).
"""

import json
from pathlib import Path

import pytest

from unmask.graph.model import HubCriterion, Node, NodeMeasures, NodeRole, UnexpandedMark
from unmask.hubs.config import HubConfig, HubThresholds, load_hub_config
from unmask.hubs.criteria import CriterionHit, evaluate
from unmask.ingest.model import AddressType

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "graph"
SCENARIOS = sorted(p.name for p in FIXTURES.iterdir() if (p / "expected.json").is_file())

A = "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq"
INGEST_THRESHOLD = 200

DEGREE = HubCriterion.DEGREE
ONE_OFF = HubCriterion.ONE_OFF_SENDERS
INGEST = HubCriterion.INGEST_HIGH_DEGREE
DUST = HubCriterion.DUST_FANOUT
KNOWN = HubCriterion.KNOWN_LIST

# Критерії, які `evaluate` рахує з вимірів вершини (без списків і без перемикачів критеріїв-джерел).
MEASURED_CRITERIA = frozenset({DEGREE.value, DUST.value, ONE_OFF.value})


# --- Будівельники ------------------------------------------------------------------


def _config(**changes) -> HubConfig:
    """Конфіг у пам'яті; критерії-джерела вимкнені, списків немає; degree/one_off далеко від тестових вершин."""
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


def _node(*, fanout=0, median=None, degree=None, unique=0, one_off=0, roles=(NodeRole.FUNDER,), **changes) -> Node:
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
            degree=fanout + unique if degree is None else degree,
            unique_senders=unique,
            one_off_senders=one_off,
            one_off_share=None if unique == 0 else one_off / unique,
            buyer_fanout=fanout,
            median_to_buyers=median,
        ),
    )
    kw.update(changes)
    return Node(**kw)


def _eval(node, config):
    return evaluate(node, config, ingest_counterparty_threshold=INGEST_THRESHOLD)


def _criteria(hits):
    return [h.criterion for h in hits]


def _dust(measured, threshold):
    return CriterionHit(DUST, measured=measured, threshold=threshold, detail="measured", lists_version=None)


# --- Фікстури графа: вершини й еталони з expected.json ------------------------------


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
        unexpanded=None if unexpanded is None else UnexpandedMark(
            reason=unexpanded["reason"],
            counterparties_seen=unexpanded["counterparties_seen"],
            signatures_seen=unexpanded["signatures_seen"],
            signatures_truncated=unexpanded["signatures_truncated"],
        ),
        measures=NodeMeasures(**d["measures"]),
    )


def _scenario_config(expected: dict, **switches) -> HubConfig:
    """Пороги сценарію з `expected.json`; списки й перемикачі критеріїв-джерел — за аргументами (умовчання: вимкнено)."""
    raw = dict(expected["config"]["thresholds"])
    raw.update(prune_off_curve=False, prune_ingest_high_degree=False)
    raw.update(switches)
    return HubConfig(
        thresholds=HubThresholds(version=expected["config"]["version"], **raw),
        lists=None,
        thresholds_digest="0" * 64,
        lists_digest=None,
    )


def _oracle_hits(expected: dict) -> dict[str, list[tuple]]:
    """Адреса -> хіти оракула (записи відсікання й позначки покупців) як кортежі полів."""
    prune = expected["prune"]
    out: dict[str, list[tuple]] = {}
    for item in [*prune["records"], *prune["buyer_flags"]]:
        out[item["address"]] = [
            (c["criterion"], c["measured"], c["threshold"], c["detail"], c["lists_version"]) for c in item["criteria"]
        ]
    return out


def _as_tuples(hits) -> list[tuple]:
    return [(h.criterion.value, h.measured, h.threshold, h.detail, h.lists_version) for h in hits]


def _scenario_hits(scenario: str, **switches) -> tuple[dict, dict[str, tuple]]:
    expected = _expected(scenario)
    config = _scenario_config(expected, **switches)
    hits = {
        n["address"]: evaluate(_node_from_dict(n), config,
                               ingest_counterparty_threshold=expected["ingest_counterparty_threshold"])
        for n in expected["graph"]["nodes"]
    }
    return expected, hits


# --- поріг суми: строго менше ------------------------------------------------------


@pytest.mark.parametrize(
    "threshold, median, expected",
    [
        (1_000_000, 999_999, True),  # поріг − 1
        (1_000_000, 1_000_000, False),  # рівно поріг — не хаб
        (1_000_000, 1_000_001, False),  # поріг + 1
        (2, 1, True),
        (2, 2, False),
        (2, 3, False),
        (1, 1, False),  # поріг 1 — критерій вимкнено: медіана суми ребра ≥ 1, «поріг − 1» недосяжний
        (1, 2, False),
    ],
)
def test_median_below_at_and_above_threshold(threshold, median, expected):
    config = _config(dust_amount_lamports=threshold)
    hits = _eval(_node(fanout=7, median=median), config)
    assert (DUST in _criteria(hits)) is expected
    if expected:
        assert hits == (_dust(median, threshold),)
    else:
        assert hits == ()


def test_threshold_one_disables_criterion_for_any_fanout():
    """Вимкнення без перемикача (R-22): `dust_amount_lamports: 1` — найменша можлива медіана (1) не менша за 1."""
    config = _config(dust_amount_lamports=1, dust_min_fanout=2, degree_threshold=10**9)
    for fanout in (2, 5, 89, 10_000):
        assert _eval(_node(fanout=fanout, median=1), config) == ()


# --- передумова fan-out: включно ---------------------------------------------------


@pytest.mark.parametrize("min_fanout", [2, 5, 37])
@pytest.mark.parametrize("offset, applicable", [(-1, False), (0, True), (1, True)])
def test_fanout_precondition_inclusive_at_min(min_fanout, offset, applicable):
    fanout = min_fanout + offset
    # Медіана 1 — максимально «пилова»; спрацювання вирішує лише передумова.
    hits = _eval(_node(fanout=fanout, median=1), _config(dust_min_fanout=min_fanout))
    assert (DUST in _criteria(hits)) is applicable
    if applicable:
        assert hits == (_dust(1, 1_000_000),)


@pytest.mark.parametrize(
    "fanout, median, expected",
    [
        (4, 999_999, False),
        (4, 1_000_000, False),
        (4, 1_000_001, False),
        (5, 999_999, True),
        (5, 1_000_000, False),
        (5, 1_000_001, False),
        (6, 999_999, True),
        (6, 1_000_000, False),
        (6, 1_000_001, False),
    ],
)
def test_fanout_and_median_grid_at_shipped_boundaries(fanout, median, expected):
    """Решітка 3×3 навколо обох меж значень v2 (`dust_min_fanout = 5`, `dust_amount_lamports = 1_000_000`)."""
    hits = _eval(_node(fanout=fanout, median=median), _config())
    if expected:
        assert hits == (_dust(median, 1_000_000),)
    else:
        assert hits == ()


def test_zero_fanout_is_not_applicable():
    """`buyer_fanout == 0` (`median_to_buyers is None`) — не застосовний за будь-якої передумови й порогу."""
    assert _eval(_node(fanout=0, median=None, degree=40), _config()) == ()
    # HubThresholds у пам'яті не перевіряє межу `dust_min_fanout >= 2` (це робить завантажувач) — і тоді теж ні.
    lax = _config(dust_min_fanout=0, dust_amount_lamports=10**18)
    assert _eval(_node(fanout=0, median=None, degree=40), lax) == ()


def test_precondition_uses_buyer_fanout_not_degree():
    """Передумова — кількість різних покупців, яким надіслано SOL, а не ступінь (ступінь включає відправників)."""
    config = _config(degree_threshold=1000)
    # Ступінь 40 (≫ 5), але покупців-отримувачів SOL лише 4 — не застосовний.
    assert _eval(_node(fanout=4, median=1, degree=40, unique=36, one_off=0), config) == ()
    # Ступінь 5 рівно fan-out — застосовний.
    assert _criteria(_eval(_node(fanout=5, median=1, degree=5), config)) == [DUST]


# --- значення з конфігу, не з констант ---------------------------------------------


def test_hit_carries_median_and_threshold_from_config_not_constants():
    node = _node(fanout=9, median=400_000)
    first = _eval(node, _config(dust_amount_lamports=500_000, dust_min_fanout=3))
    second = _eval(node, _config(dust_amount_lamports=7_777_777, dust_min_fanout=9))
    assert first == (_dust(400_000, 500_000),)
    assert second == (_dust(400_000, 7_777_777),)
    # Інша передумова з конфігу: fan-out 9 < 10 — не застосовний.
    assert _eval(node, _config(dust_amount_lamports=7_777_777, dust_min_fanout=10)) == ()
    # Інший поріг з конфігу: медіана 400 000 не менша за 400 000.
    assert _eval(node, _config(dust_amount_lamports=400_000, dust_min_fanout=3)) == ()


def test_shipped_config_dust_thresholds_drive_the_rule():
    """Golden-перевірка проти зафіксованого `config/hubs.yaml` v2: межі — значення з файла (принцип III)."""
    config = load_hub_config(ROOT / "config/hubs.yaml", ROOT / "config/hub_addresses.yaml")
    t = config.thresholds
    assert (t.version, t.dust_amount_lamports, t.dust_min_fanout) == (2, 1_000_000, 5)
    below = _eval(_node(fanout=t.dust_min_fanout, median=t.dust_amount_lamports - 1), config)
    at = _eval(_node(fanout=t.dust_min_fanout, median=t.dust_amount_lamports), config)
    few = _eval(_node(fanout=t.dust_min_fanout - 1, median=1), config)
    assert [(h.criterion, h.measured, h.threshold) for h in below] == [(DUST, 999_999, 1_000_000)]
    assert DUST not in _criteria(at)
    assert DUST not in _criteria(few)


# --- інваріант типу CriterionHit: напрямок за критерієм ----------------------------


@pytest.mark.parametrize(
    "measured, threshold",
    [(1_000_000, 1_000_000), (1_000_001, 1_000_000), (5, 1), (1, 1), (2, 2), (10**18, 1_000_000)],
)
def test_criterion_hit_rejects_dust_measured_not_below_threshold_and_degree_not_above(measured, threshold):
    """Напрямок у типі: `dust_fanout ⇒ measured < threshold`; рівно поріг і вище — `ValueError`."""
    with pytest.raises(ValueError):
        _dust(measured, threshold)
    # Дзеркально: значення, допустимі для dust, для degree (напрямок «більше») відхиляються.
    with pytest.raises(ValueError):
        CriterionHit(DEGREE, measured=threshold - 1 if threshold > 1 else 0, threshold=threshold,
                     detail="measured", lists_version=None)


def test_criterion_hit_accepts_dust_measured_below_threshold():
    assert _dust(999_999, 1_000_000).measured == 999_999
    assert _dust(1, 2).threshold == 2
    assert CriterionHit("dust_fanout", 500_000, 1_000_000, "measured", None).criterion is DUST  # рядок -> перелічення
    # Для degree ті самі числа — «нижче порогу» — не хіт: напрямок залежить від критерію.
    with pytest.raises(ValueError):
        CriterionHit(DEGREE, 999_999, 1_000_000, "measured", None)


def test_criterion_hit_rejects_dust_median_below_one():
    """`measured` — медіана сум ребер у лампортах, ≥ 1 за інваріантом `NodeMeasures`; 0 і менше — не хіт."""
    for measured in (0, -1):
        with pytest.raises(ValueError):
            _dust(measured, 1_000_000)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(measured=500_000.0, threshold=1_000_000),  # float — не int
        dict(measured=500_000, threshold=1_000_000.0),
        dict(measured=True, threshold=2),  # bool — не int
        dict(measured=0, threshold=True),
        dict(measured=None, threshold=1_000_000),
        dict(measured=500_000, threshold=None),
        dict(measured="500000", threshold=1_000_000),
    ],
)
def test_criterion_hit_rejects_dust_wrong_types(kwargs):
    with pytest.raises(TypeError):
        CriterionHit(DUST, detail="measured", lists_version=None, **kwargs)


@pytest.mark.parametrize(
    "detail, lists_version",
    [("list:exchanges", None), ("unexpanded:high_degree", None), ("address_type:off_curve", None),
     ("measured", 1), ("", None)],
)
def test_criterion_hit_rejects_dust_inconsistent_detail_and_lists_version(detail, lists_version):
    with pytest.raises(ValueError):
        CriterionHit(DUST, measured=500_000, threshold=1_000_000, detail=detail, lists_version=lists_version)


# --- незалежність від інших критеріїв і порядок ------------------------------------


def test_dust_independent_of_degree_and_one_off_and_ordered_by_criterion_string():
    config = _config()
    # Усі три критерії за вимірами одночасно: ступінь 150 > 100, частка 39/40 > 0.8 при 40 ≥ 10, пил при fan-out 110.
    both = _eval(_node(fanout=110, median=500_000, unique=40, one_off=39), config)
    assert _criteria(both) == [DEGREE, DUST, ONE_OFF]
    assert [h.criterion.value for h in both] == sorted(h.criterion.value for h in both)
    assert both[1] == _dust(500_000, 1_000_000)
    # Кожен окремо — незалежно від решти.
    assert _criteria(_eval(_node(fanout=110, median=2_000_000, unique=40, one_off=39), config)) == [DEGREE, ONE_OFF]
    assert _criteria(_eval(_node(fanout=110, median=500_000, unique=40, one_off=1), config)) == [DEGREE, DUST]
    assert _criteria(_eval(_node(fanout=50, median=500_000, unique=40, one_off=39), config)) == [DUST, ONE_OFF]
    assert _criteria(_eval(_node(fanout=50, median=500_000, unique=0), config)) == [DUST]


def test_buyer_gets_dust_hit_too():
    """`evaluate` не знає про захист покупців: покупець, що пилить інших покупців, отримує хіт (рішення — у prune)."""
    node = _node(fanout=6, median=500_000, roles=(NodeRole.BUYER, NodeRole.FUNDER))
    assert _eval(node, _config()) == (_dust(500_000, 1_000_000),)


def test_dust_hit_independent_of_lists_and_switches():
    """`lists=None`, `prune_off_curve=false`, `prune_ingest_high_degree=false` чи `true` → той самий хіт."""
    mark = UnexpandedMark("high_degree", counterparties_seen=201, signatures_seen=10, signatures_truncated=False)
    node = _node(fanout=19, median=500_000, address_type=AddressType.WALLET, unexpanded=mark)
    expected = (_dust(500_000, 1_000_000),)
    assert _eval(node, _config(prune_off_curve=False, prune_ingest_high_degree=False)) == expected
    lists_config = load_hub_config(ROOT / "config/hubs.yaml", ROOT / "config/hub_addresses.yaml")
    dust_hits = [h for h in _eval(node, lists_config) if h.criterion is DUST]
    assert tuple(dust_hits) == expected


# --- сценарії графа: еталони незалежного оракула ------------------------------------


def test_g_dust_D_fires_dust_fanout_alone_and_E_with_fanout_4_does_not():
    expected, hits = _scenario_hits("g_dust")
    w = expected["wallets"]
    assert hits[w["D"]] == (_dust(500_000, 1_000_000),)
    assert _as_tuples(hits[w["D"]]) == _oracle_hits(expected)[w["D"]]
    # E: ті самі пилові суми, fan-out 4 = поріг − 1 — не застосовний. Ступінь E теж 4, тож мутацію «degree замість
    # buyer_fanout у передумові» ловить не цей сценарій, а `test_precondition_uses_buyer_fanout_not_degree`.
    e_node = next(n for n in expected["graph"]["nodes"] if n["address"] == w["E"])
    assert (e_node["measures"]["buyer_fanout"], e_node["measures"]["median_to_buyers"]) == (4, 500_000)
    assert hits[w["E"]] == ()
    assert hits[w["F"]] == ()
    assert {a for a, h in hits.items() if h} == {w["D"]}


def test_g_financier_R_has_no_hits():
    """SC-009: справжній фінансист (fan-out 30, медіана 0,7 SOL, один пиловий серед справжніх) — не хаб."""
    expected, hits = _scenario_hits("g_financier")
    r = expected["wallets"]["R"]
    r_node = next(n for n in expected["graph"]["nodes"] if n["address"] == r)
    assert (r_node["measures"]["buyer_fanout"], r_node["measures"]["median_to_buyers"]) == (30, 700_000_000)
    assert hits[r] == ()
    assert all(h == () for h in hits.values())
    assert _oracle_hits(expected) == {}


def test_g_dust_mixed_X_and_W_fire_Y_Z_V_U_do_not():
    expected, hits = _scenario_hits("g_dust_mixed")
    w = expected["wallets"]
    oracle = _oracle_hits(expected)
    assert hits[w["X"]] == (_dust(500_000, 1_000_000),)  # 4 пилових + 3 справжніх: верхня медіана — пил
    assert hits[w["W"]] == (_dust(999_999, 1_000_000),)  # поріг − 1
    for name in ("X", "W"):
        assert _as_tuples(hits[w[name]]) == oracle[w[name]]
    for name in ("Y", "Z", "V", "U"):  # 3+3; рівно поріг; поріг + 1; fan-out 0 (лише SPL)
        assert hits[w[name]] == (), name
    assert {a for a, h in hits.items() if h} == {w["X"], w["W"]}


def test_g_buyer_hub_X_gets_five_hits_in_criterion_order():
    """Покупець X задовольняє всі п'ять критеріїв (еталон), `dust_fanout` — на другому місці за рядком критерію.

    `known_list`/`ingest_high_degree` у `evaluate` додає T-034 (його тест
    `test_node_matching_all_five_gets_five_hits_in_criterion_order` звіряє всі п'ять); тут — еталон із п'ятьма
    хітами у порядку критерію і точний збіг хітів за вимірами, серед яких `dust_fanout`.
    """
    expected, hits = _scenario_hits("g_buyer_hub")
    x = expected["wallets"]["X"]
    oracle = _oracle_hits(expected)[x]
    assert [c[0] for c in oracle] == ["degree", "dust_fanout", "ingest_high_degree", "known_list", "one_off_senders"]
    assert _as_tuples(hits[x]) == [c for c in oracle if c[0] in MEASURED_CRITERIA]
    assert _criteria(hits[x]) == [DEGREE, DUST, ONE_OFF]
    assert hits[x][1] == _dust(500_000, 1_000_000)
    # Позначка, не видалення: X — покупець (рішення «лишити з позначкою» — у prune, FR-002-10).
    assert x in {f["address"] for f in expected["prune"]["buyer_flags"]}
    assert x not in {r["address"] for r in expected["prune"]["records"]}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_measured_hits_match_oracle_on_every_scenario(scenario):
    """На всіх сценаріях графа хіти за вимірами (degree, dust_fanout, one_off_senders) збігаються з оракулом точно."""
    expected, hits = _scenario_hits(scenario)
    oracle = _oracle_hits(expected)
    for address, got in hits.items():
        want = [c for c in oracle.get(address, []) if c[0] in MEASURED_CRITERIA]
        assert _as_tuples(got) == want, address
