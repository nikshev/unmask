# verifies: FR-002-20
"""`ingest.serialize.from_dict` (T-026): читання контракту 001 назад у типи, обернене до `to_dict`.

Що доводять тести:
- round-trip `from_dict(to_dict(x)) == x` на справжніх результатах збору (basic, hub у трьох конфігураціях,
  corrupt, збій кроку 4) і на всіх видах `Rejection`; також через канонічний JSON-текст;
- `to_dict(from_dict(d)) == d` для документа з усіма непорожніми списками;
- сувора форма: кожен об'єкт має РІВНО ключі схеми (зайвий чи відсутній ключ — `ValueError`), кожне
  значення — свого JSON-типу (чужий тип — виняток, а не мовчазне прийняття); перелічення поза
  значеннями — `ValueError`; інваріанти моделі 001 (порожній `spent`, дубль покупця, `amount < 1`,
  `wallets_analyzed != len(buyers)`, розбіжний `status`) спрацьовують;
- `delegated` (схема 1.1, T-044) необов'язковий; суперечність із повнотою покупців — `ValueError`;
- вхід не змінюється, результат не ділить із ним змінних структур.

Перевірки типів/ключів породжуються автоматично з форми базового документа (кожен ключ кожного
об'єкта), тож нове поле контракту без обробки в `from_dict` не лишиться непоміченим.
Мережі немає.
"""

import copy
import dataclasses
import json
import math
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    AddressType,
    Asset,
    Buyer,
    BuyersCompleteness,
    Completeness,
    IngestResult,
    MissingHistory,
    MissingReason,
    RejectKind,
    Rejection,
    RunMetadata,
    Spend,
    Transfer,
    UnexpandedNode,
    UnexpandedReason,
)
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.serialize import from_dict, to_dict, to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB, CORRUPT, NOTFOUND = (SCENARIOS / n for n in ("basic", "hub", "corrupt", "notfound"))
SHIPPED = ROOT / "config" / "ingest.yaml"
EXPECTED = json.loads((BASIC / "expected.json").read_text())
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
CORRUPT_CAST = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]
NOTFOUND_CAST = json.loads((NOTFOUND / "rpc.json").read_text())["_meta"]["cast"]
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
SIG = EXPECTED["buyers"][0]["first_buy_signature"]
SIG2 = EXPECTED["buyers"][1]["first_buy_signature"]
USDC = W["USDC"]


# --- помічники ------------------------------------------------------------------------


def _cfg(expected_config: dict | None = None, **overrides):
    cfg = load_config(SHIPPED)
    values = dict(expected_config or EXPECTED["config"])
    values.update(overrides)
    return dataclasses.replace(cfg, **values)


def _collect(directory: Path, mint: str, cfg=None):
    clock = FakeClock()
    return IngestService(cfg or _cfg(), FixtureRpcSource(directory, clock=clock), clock=clock).collect(mint)


def _rich_result() -> IngestResult:
    """Результат, де кожен список непорожній, а необов'язкові поля представлені обома значеннями."""
    buyers = (
        Buyer(W["A"], 1, SIG, 200, 1_759_400_200, 4_000_000_000,
              (Spend(Asset.SOL, 600_000_000), Spend(Asset.spl(USDC), 7)),
              ("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "11111111111111111111111111111111"),
              AddressType.WALLET),
        Buyer(W["B"], 2, SIG2, 210, None, 1_000, (Spend(Asset.SOL, 5),), (), AddressType.OFF_CURVE),
    )
    transfers = (
        Transfer(SIG, 100, 1_759_400_100, "0", W["C"], W["A"], Asset.SOL, 5_000_000_000, None, 1),
        Transfer(SIG2, 101, None, "1.2", W["D"], W["B"], Asset.spl(USDC), 25_000_000, 6, 2),
    )
    unexpanded = (
        UnexpandedNode(W["D"], 2, UnexpandedReason.HIGH_DEGREE, 201, 12, False),
        UnexpandedNode(W["C"], 1, UnexpandedReason.SIGNATURE_CAP, 3, 300, True),
    )
    missing = (MissingHistory(W["B"], 1, MissingReason.TIMEOUT, "getSignaturesForAddress: timeout"),)
    completeness = Completeness.derive(
        missing, BuyersCompleteness(False, MissingReason.RATE_LIMITED, "mint history cut at slot 7"))
    meta = RunMetadata(
        mint=M, analyzed_at=1_759_500_000, wallets_analyzed=2, config_version=3, first_buyers_n=5,
        funding_depth=3, counterparty_threshold=200, max_signatures_per_wallet=300,
        collect_spl_inbound=True, time_budget_seconds=40.0, elapsed_seconds=1.5, rpc_calls=7,
        transactions_scanned=3, source="fixture:basic", resumed=True, served_from_cache=False)
    return IngestResult(meta, completeness, buyers, transfers, unexpanded)


RICH = _rich_result()
RICH_DOC = to_dict(RICH)


def _doc() -> dict:
    return copy.deepcopy(RICH_DOC)


def _get(doc, path):
    for step in path:
        doc = doc[step]
    return doc


def _set(doc, path, value):
    _get(doc, path[:-1])[path[-1]] = value
    return doc


def _walk(value, path=()):
    """Усі (шлях, значення) документа, включно з контейнерами."""
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, path + (i,))


def _id(path) -> str:
    return ".".join(str(p) for p in path) or "<root>"


OBJECT_PATHS = [p for p, v in _walk(RICH_DOC) if isinstance(v, dict)]
LEAF_PATHS = [p for p, v in _walk(RICH_DOC) if not isinstance(v, (dict, list))]
LIST_PATHS = [p for p, v in _walk(RICH_DOC) if isinstance(v, list)]
def _nullable(path) -> bool:
    """Поля, де `null` — законне значення за схемою 001."""
    return (
        (path[0] == "buyers" and path[-1] == "first_buy_time")
        or (path[0] == "transfers" and path[-1] in {"block_time", "decimals"})
        or path == ("completeness", "buyers", "reason")
    )


# --- round-trip на справжніх результатах збору ---------------------------------------------


def _real_outcomes():
    out = {"basic": _collect(BASIC, M)}
    for case in HUB_EXPECTED["cases"]:
        out[case["name"]] = _collect(HUB, HUB_EXPECTED["mint"], _cfg(case["config"]))
    out["corrupt"] = _collect(CORRUPT, CORRUPT_CAST["M"], _cfg(first_buyers_n=3, funding_depth=1))
    out["rich"] = RICH
    return out


REAL = _real_outcomes()


@pytest.mark.parametrize("name", sorted(REAL))
def test_from_dict_of_to_dict_equals_original_result(name):
    original = REAL[name]
    assert isinstance(original, IngestResult)
    restored = from_dict(to_dict(original))
    assert type(restored) is IngestResult
    assert restored == original


@pytest.mark.parametrize("name", sorted(REAL))
def test_from_dict_through_json_text_equals_original_result(name):
    original = REAL[name]
    assert from_dict(json.loads(to_json(original))) == original


def test_real_scenarios_are_not_trivial():
    """Round-trip осмислений лише на непорожніх результатах: є покупці, перекази, нерозгорнуті й неповнота."""
    assert len(REAL["basic"].buyers) == 5 and REAL["basic"].transfers
    assert any(r.unexpanded for r in REAL.values() if isinstance(r, IngestResult))
    assert REAL["corrupt"].completeness.missing
    assert {n for n in REAL} >= {"basic", "hub_high_degree", "hub_signature_cap", "hub_control", "corrupt", "rich"}


def test_step4_failure_result_round_trips():
    from unmask.ingest.rpc.fixture import FailAfter
    from unmask.ingest.rpc.protocol import RpcUnavailable

    clock = FakeClock()
    source = FixtureRpcSource(BASIC, failures=[FailAfter(1, RpcUnavailable("boom"))], clock=clock)
    result = IngestService(_cfg(), source, clock=clock).collect(M)
    assert isinstance(result, IngestResult) and result.buyers == ()
    assert from_dict(to_dict(result)) == result


@pytest.mark.parametrize(
    ("directory", "mint"),
    [
        pytest.param(BASIC, "not-a-valid-address", id="invalid_address"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["MISSING_MINT"], id="account_missing"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["SYSTEM_OWNED"], id="not_a_mint-system"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["TOKEN_ACCOUNT"], id="not_a_mint-token-account"),
    ],
)
def test_real_rejections_round_trip(directory, mint):
    original = _collect(directory, mint)
    assert isinstance(original, Rejection)
    restored = from_dict(to_dict(original))
    assert type(restored) is Rejection
    assert restored == original
    assert from_dict(json.loads(to_json(original))) == original


@pytest.mark.parametrize("kind", list(RejectKind))
@pytest.mark.parametrize(("mint", "detail"), [(M, "why"), ("", ""), ("адреса-🙂", "не base58")])
def test_every_reject_kind_with_edge_strings_round_trips(kind, mint, detail):
    original = Rejection(kind, mint, detail)
    restored = from_dict(to_dict(original))
    assert restored == original and restored.kind is kind


def test_to_dict_of_from_dict_reproduces_document_exactly():
    doc = _doc()
    assert to_dict(from_dict(doc)) == doc == RICH_DOC
    assert json.dumps(to_dict(from_dict(doc)), sort_keys=True) == json.dumps(RICH_DOC, sort_keys=True)


@pytest.mark.parametrize("name", sorted(REAL))
def test_to_dict_of_from_dict_reproduces_collected_documents(name):
    doc = to_dict(REAL[name])
    assert to_dict(from_dict(doc)) == doc


# --- форма результату: типи моделі, а не словники --------------------------------------------


def test_restored_result_uses_model_types_and_tuples():
    r = from_dict(_doc())
    assert isinstance(r.metadata, RunMetadata) and isinstance(r.completeness, Completeness)
    assert type(r.buyers) is tuple and all(type(b) is Buyer for b in r.buyers)
    assert type(r.transfers) is tuple and all(type(t) is Transfer for t in r.transfers)
    assert type(r.unexpanded) is tuple and all(type(u) is UnexpandedNode for u in r.unexpanded)
    assert type(r.completeness.missing) is tuple and type(r.completeness.missing[0]) is MissingHistory
    assert type(r.buyers[0].spent) is tuple and type(r.buyers[0].spent[0]) is Spend
    assert type(r.buyers[0].programs) is tuple
    assert r.buyers[0].address_type is AddressType.WALLET and r.buyers[1].address_type is AddressType.OFF_CURVE
    assert type(r.transfers[1].asset) is Asset and r.transfers[1].asset.mint == USDC
    assert r.unexpanded[0].reason is UnexpandedReason.HIGH_DEGREE
    assert r.completeness.missing[0].reason is MissingReason.TIMEOUT
    assert r.completeness.buyers.reason is MissingReason.RATE_LIMITED


def test_status_is_derived_not_stored_and_matches_document():
    r = from_dict(_doc())
    assert r.completeness.status.value == RICH_DOC["completeness"]["status"] == "incomplete"
    complete = _doc()
    complete["completeness"] = {"status": "complete", "missing": [], "buyers": {"complete": True, "reason": None, "detail": ""}}
    assert from_dict(complete).completeness.status.value == "complete"


def test_order_of_lists_is_kept_as_in_document():
    doc = _doc()
    doc["transfers"].reverse()
    doc["unexpanded"].reverse()
    r = from_dict(doc)
    assert [t.signature for t in r.transfers] == [t["signature"] for t in doc["transfers"]]
    assert [u.wallet for u in r.unexpanded] == [u["wallet"] for u in doc["unexpanded"]]


def test_input_is_not_modified_and_result_shares_nothing_with_it():
    doc = _doc()
    snapshot = copy.deepcopy(doc)
    result = from_dict(doc)
    assert doc == snapshot
    doc["buyers"][0]["spent"].append({"asset": "sol", "amount": 1})
    doc["buyers"][0]["programs"].append("x")
    doc["transfers"].clear()
    assert result == from_dict(copy.deepcopy(snapshot))


def test_metadata_numbers_are_kept_exactly():
    doc = _doc()
    doc["metadata"].update(time_budget_seconds=33.5, elapsed_seconds=0, analyzed_at=0, rpc_calls=0)
    r = from_dict(doc)
    assert r.metadata.time_budget_seconds == 33.5 and r.metadata.elapsed_seconds == 0
    assert (r.metadata.analyzed_at, r.metadata.rpc_calls) == (0, 0)
    assert to_dict(r)["metadata"]["time_budget_seconds"] == 33.5


# --- сувора форма: ключі -----------------------------------------------------------------------


@pytest.mark.parametrize("path", OBJECT_PATHS, ids=_id)
def test_unknown_key_in_any_object_is_rejected(path):
    doc = _doc()
    _get(doc, path)["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        from_dict(doc)


# `delegated` — єдиний необов'язковий ключ схеми 1.1: його відсутність дає NOT_ANALYZED (T-044,
# tests/test_ingest_delegated_model.py::test_from_dict_without_delegated_key_gives_not_analyzed).
MISSING_KEY_CASES = [(p, k) for p in OBJECT_PATHS for k in _get(RICH_DOC, p) if (p, k) != ((), "delegated")]


@pytest.mark.parametrize(("path", "key"), MISSING_KEY_CASES, ids=[f"{_id(p)}:{k}" for p, k in MISSING_KEY_CASES])
def test_missing_key_in_any_object_is_rejected(path, key):
    doc = _doc()
    del _get(doc, path)[key]
    with pytest.raises(ValueError, match=key):
        from_dict(doc)


def test_delegated_that_contradicts_buyers_completeness_is_rejected():
    """Схема 1.1 (T-044): `delegated.complete` не береться на віру — RICH має неповних покупців."""
    doc = _doc()
    doc["delegated"] = {"links": [], "unpaired": [], "complete": True, "reason": None, "detail": ""}
    with pytest.raises(ValueError, match="delegated"):
        from_dict(doc)


def test_rejection_with_extra_or_missing_key_is_rejected():
    ok = {"kind": "invalid_address", "mint": "x", "detail": "d"}
    assert from_dict(ok) == Rejection(RejectKind.INVALID_ADDRESS, "x", "d")
    with pytest.raises(ValueError, match="extra"):
        from_dict({**ok, "extra": 1})
    for key in ok:
        broken = {k: v for k, v in ok.items() if k != key}
        with pytest.raises(ValueError):
            from_dict(broken)


def test_document_that_mixes_result_and_rejection_keys_is_rejected():
    with pytest.raises(ValueError):
        from_dict({**_doc(), "kind": "invalid_address"})
    with pytest.raises(ValueError):
        from_dict({"kind": "invalid_address", "mint": "x", "detail": "d", "metadata": {}})


def test_empty_document_is_rejected():
    with pytest.raises(ValueError):
        from_dict({})


@pytest.mark.parametrize("bad", [[], None, "{}", 5, (1,), RICH, [("kind", "x")]], ids=repr)
def test_non_dict_input_is_type_error(bad):
    with pytest.raises(TypeError):
        from_dict(bad)


# --- сувора форма: типи значень -----------------------------------------------------------------


def _wrong_values(path, value):
    """Значення іншого JSON-типу, ніж законне для цього поля."""
    if isinstance(value, bool):
        return ["true", 1, 0, [], {}] + ([] if _nullable(path) else [None])
    if isinstance(value, int):
        return ["1", 1.5, True, [], {}] + ([] if _nullable(path) else [None])
    if isinstance(value, float):
        return ["1.0", True, [], {}, None]
    if isinstance(value, str):
        return [1, 1.5, True, [], {}] + ([] if _nullable(path) else [None])
    return ["x", 1.5, True, [], {}]  # null-поле: будь-який не-null непридатний тип/значення


WRONG_VALUE_CASES = [(p, i, bad) for p in LEAF_PATHS for i, bad in enumerate(_wrong_values(p, _get(RICH_DOC, p)))]


@pytest.mark.parametrize(
    ("path", "idx", "bad"), WRONG_VALUE_CASES, ids=[f"{_id(p)}#{i}" for p, i, _ in WRONG_VALUE_CASES]
)
def test_wrong_json_type_of_any_leaf_is_rejected(path, idx, bad):
    doc = _set(_doc(), path, bad)
    with pytest.raises((TypeError, ValueError)):
        from_dict(doc)


@pytest.mark.parametrize("path", LIST_PATHS, ids=_id)
@pytest.mark.parametrize("bad", ["abc", {}, None, 1, {"a": 1}], ids=repr)
def test_non_list_in_place_of_list_is_rejected(path, bad):
    doc = _set(_doc(), path, bad)
    with pytest.raises((TypeError, ValueError)):
        from_dict(doc)


@pytest.mark.parametrize("path", [p for p in OBJECT_PATHS if p], ids=_id)
@pytest.mark.parametrize("bad", [[], "x", None, 1], ids=repr)
def test_non_object_in_place_of_object_is_rejected(path, bad):
    doc = _set(_doc(), path, bad)
    with pytest.raises((TypeError, ValueError)):
        from_dict(doc)


def test_list_items_of_wrong_type_are_rejected():
    for path in (("buyers", 0, "programs"), ("buyers", 0, "spent"), ("buyers",), ("transfers",),
                 ("unexpanded",), ("completeness", "missing")):
        for bad in (1, "x", None, []):
            if bad == "x" and path[-1] == "programs":
                continue  # рядок — законний елемент programs
            doc = _doc()
            _get(doc, path)[0] = bad
            with pytest.raises((TypeError, ValueError)):
                from_dict(doc)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field", ["time_budget_seconds", "elapsed_seconds"])
def test_non_finite_floats_are_rejected(field, value):
    doc = _set(_doc(), ("metadata", field), value)
    with pytest.raises((TypeError, ValueError)):
        from_dict(doc)


# --- перелічення -------------------------------------------------------------------------------


ENUM_PATHS = [
    (("buyers", 0, "address_type"), AddressType),
    (("completeness", "status"), None),
    (("completeness", "missing", 0, "reason"), MissingReason),
    (("completeness", "buyers", "reason"), MissingReason),
    (("unexpanded", 0, "reason"), UnexpandedReason),
]


@pytest.mark.parametrize(("path", "enum"), ENUM_PATHS, ids=[_id(p) for p, _ in ENUM_PATHS])
@pytest.mark.parametrize("bad", ["bogus", "", "WALLET", "Complete", " wallet"])
def test_unknown_enum_value_is_value_error(path, enum, bad):
    doc = _set(_doc(), path, bad)
    with pytest.raises(ValueError):
        from_dict(doc)


def test_every_enum_value_is_accepted_where_it_is_legal():
    for member in AddressType:
        doc = _set(_doc(), ("buyers", 0, "address_type"), member.value)
        assert from_dict(doc).buyers[0].address_type is member
    for member in UnexpandedReason:
        doc = _set(_doc(), ("unexpanded", 0, "reason"), member.value)
        assert from_dict(doc).unexpanded[0].reason is member
    for member in MissingReason:
        doc = _set(_doc(), ("completeness", "missing", 0, "reason"), member.value)
        assert from_dict(doc).completeness.missing[0].reason is member
    for member in RejectKind:
        assert from_dict({"kind": member.value, "mint": "m", "detail": "d"}).kind is member


@pytest.mark.parametrize("bad", ["SOL", "spl:", "usdc", "spl", "", "native"])
@pytest.mark.parametrize("path", [("transfers", 0, "asset"), ("buyers", 0, "spent", 0, "asset")], ids=_id)
def test_invalid_asset_is_value_error(path, bad):
    with pytest.raises(ValueError):
        from_dict(_set(_doc(), path, bad))


def test_rejection_with_unknown_kind_is_value_error():
    with pytest.raises(ValueError):
        from_dict({"kind": "teapot", "mint": "m", "detail": "d"})


# --- інваріанти моделі 001 доходять до читача -----------------------------------------------------


def _violations():
    def with_(path, value):
        return lambda d: _set(d, path, value)

    def dup_buyer(d):
        d["buyers"][1]["wallet"] = d["buyers"][0]["wallet"]

    def dup_transfer(d):
        d["transfers"][1]["signature"] = d["transfers"][0]["signature"]
        d["transfers"][1]["instruction_path"] = d["transfers"][0]["instruction_path"]

    def complete_with_missing(d):
        d["completeness"]["status"] = "complete"

    def incomplete_without_reason(d):
        d["completeness"]["buyers"] = {"complete": False, "reason": None, "detail": ""}

    def complete_with_reason(d):
        d["completeness"]["buyers"] = {"complete": True, "reason": "timeout", "detail": ""}

    def dup_missing(d):
        d["completeness"]["missing"].append(copy.deepcopy(d["completeness"]["missing"][0]))

    def status_incomplete_over_complete(d):
        d["completeness"] = {"status": "incomplete", "missing": [],
                             "buyers": {"complete": True, "reason": None, "detail": ""}}

    return {
        "wallets_analyzed_mismatch": with_(("metadata", "wallets_analyzed"), 3),
        "rank_zero": with_(("buyers", 0, "rank"), 0),
        "empty_spent": with_(("buyers", 0, "spent"), []),
        "spend_amount_zero": with_(("buyers", 0, "spent", 0, "amount"), 0),
        "received_amount_zero": with_(("buyers", 0, "received_amount"), 0),
        "negative_slot": with_(("buyers", 0, "first_buy_slot"), -1),
        "empty_wallet": with_(("buyers", 0, "wallet"), ""),
        "empty_signature": with_(("buyers", 0, "first_buy_signature"), ""),
        "transfer_amount_zero": with_(("transfers", 0, "amount"), 0),
        "transfer_depth_zero": with_(("transfers", 0, "depth"), 0),
        "sol_with_decimals": with_(("transfers", 0, "decimals"), 6),
        "decimals_256": with_(("transfers", 1, "decimals"), 256),
        "path_not_numeric": with_(("transfers", 0, "instruction_path"), "a.b"),
        "path_three_levels": with_(("transfers", 0, "instruction_path"), "1.2.3"),
        "path_empty": with_(("transfers", 0, "instruction_path"), ""),
        "negative_block_time": with_(("transfers", 0, "block_time"), -5),
        "time_budget_zero": with_(("metadata", "time_budget_seconds"), 0),
        "negative_elapsed": with_(("metadata", "elapsed_seconds"), -0.5),
        "config_version_zero": with_(("metadata", "config_version"), 0),
        "empty_source": with_(("metadata", "source"), ""),
        "empty_mint": with_(("metadata", "mint"), ""),
        "unexpanded_negative_depth": with_(("unexpanded", 0, "depth"), -1),
        "missing_negative_depth": with_(("completeness", "missing", 0, "depth"), -1),
        "duplicate_buyer_wallet": dup_buyer,
        "duplicate_transfer_key": dup_transfer,
        "status_complete_over_missing": complete_with_missing,
        "status_incomplete_over_complete": status_incomplete_over_complete,
        "buyers_incomplete_without_reason": incomplete_without_reason,
        "buyers_complete_with_reason": complete_with_reason,
        "duplicate_missing_entry": dup_missing,
    }


@pytest.mark.parametrize("name", sorted(_violations()))
def test_model_invariant_violation_is_value_error(name):
    doc = _doc()
    _violations()[name](doc)
    with pytest.raises((TypeError, ValueError)):
        from_dict(doc)


def test_status_disagreeing_with_derived_one_is_not_silently_rederived():
    doc = _doc()
    assert doc["completeness"]["status"] == "incomplete"
    doc["completeness"]["status"] = "complete"
    with pytest.raises(ValueError, match="status"):
        from_dict(doc)


def test_valid_boundary_values_are_accepted():
    doc = _doc()
    doc["metadata"].update(first_buyers_n=500, funding_depth=1, counterparty_threshold=1,
                           max_signatures_per_wallet=1, time_budget_seconds=0.001)
    doc["transfers"][1]["decimals"] = 255
    doc["buyers"][0]["rank"] = 1
    assert from_dict(doc).transfers[1].decimals == 255


def test_from_dict_is_deterministic_and_idempotent():
    doc = _doc()
    assert from_dict(doc) == from_dict(copy.deepcopy(doc))
    assert from_dict(to_dict(from_dict(doc))) == from_dict(doc)
