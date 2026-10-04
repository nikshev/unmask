# verifies: FR-001-06, FR-001-14
"""Серіалізація за контрактом (T-020): `serialize.to_dict` / `to_json`.

Еталон форми — `contracts/ingest-result.schema.json` (`Draft202012Validator`, схема не послаблюється).
`to_dict` має бути валідним проти неї для будь-якого `IngestOutcome`; вихід — лише JSON-типи
(точні `dict/list/str/int/float/bool/None`: жодних `set`, `tuple`, `StrEnum`, `Asset`), службове поле
`Completeness._token` не потрапляє у вихід, `status` виведено явно.

Рішення власника процесу щодо `expected.json`: ці файли НЕ мають форми схеми (немає `metadata`,
`buyers_complete` замість об'єкта, службові ключі), тому вони не валідуються проти схеми буквально.
Натомість `to_dict` результату збору на еталонному сценарії валідується проти схеми, а переказі/
покупці/нерозгорнуті/повнота звіряються з `expected.json` — незалежним еталоном (генератор
`build_fixtures.py`), а не з іншою ділянкою `serialize`. Мережі немає.
"""

import copy
import dataclasses
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

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
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcUnavailable
from unmask.ingest.serialize import to_dict, to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB, CORRUPT, NOTFOUND = (SCENARIOS / n for n in ("basic", "hub", "corrupt", "notfound"))
SHIPPED = ROOT / "config" / "ingest.yaml"
SCHEMA = json.loads((ROOT / "specs/001-onchain-data-ingest/contracts/ingest-result.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

EXPECTED = json.loads((BASIC / "expected.json").read_text())
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
CORRUPT_CAST = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]
NOTFOUND_CAST = json.loads((NOTFOUND / "rpc.json").read_text())["_meta"]["cast"]
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
SIG = EXPECTED["buyers"][0]["first_buy_signature"]
SIG2 = EXPECTED["buyers"][1]["first_buy_signature"]

JSON_TYPES = (dict, list, str, int, float, bool, type(None))


# --- помічники ------------------------------------------------------------------------


def _errors(instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(instance)]


def _assert_valid(instance) -> None:
    assert _errors(instance) == []


def _assert_invalid(instance) -> None:
    assert _errors(instance) != []


def _cfg(expected_config: dict | None = None, **overrides):
    cfg = load_config(SHIPPED)
    values = dict(expected_config or EXPECTED["config"])
    values.update(overrides)
    return dataclasses.replace(cfg, **values)


def _service(directory: Path, cfg=None, *, failures=None) -> IngestService:
    clock = FakeClock()
    source = FixtureRpcSource(directory, failures=failures, clock=clock)
    return IngestService(cfg or _cfg(), source, clock=clock)


def _collect(directory: Path, mint: str, cfg=None, *, failures=None):
    return _service(directory, cfg, failures=failures).collect(mint)


def _assert_json_types_only(value, path="$") -> None:
    """Точні JSON-типи (не підкласи): StrEnum/Asset/tuple/set не просочуються."""
    assert type(value) in JSON_TYPES, f"{path}: {type(value)!r}"
    if isinstance(value, dict):
        for k, v in value.items():
            assert type(k) is str, f"{path}: key {k!r} is {type(k)!r}"
            _assert_json_types_only(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_json_types_only(v, f"{path}[{i}]")


def _keys(value) -> set[str]:
    """Усі імена ключів у вкладеній структурі."""
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            out |= _keys(v)
    elif isinstance(value, list):
        for v in value:
            out |= _keys(v)
    return out


def _meta(**overrides) -> RunMetadata:
    values = dict(
        mint=M, analyzed_at=1_759_500_000, wallets_analyzed=1, config_version=1, first_buyers_n=5,
        funding_depth=3, counterparty_threshold=200, max_signatures_per_wallet=300,
        collect_spl_inbound=True, time_budget_seconds=40.0, elapsed_seconds=1.5, rpc_calls=7,
        transactions_scanned=3, source="fixture:basic", resumed=False, served_from_cache=False,
    )
    values.update(overrides)
    return RunMetadata(**values)


def _buyer(wallet=W["A"], rank=1, **overrides) -> Buyer:
    values = dict(
        wallet=wallet, rank=rank, first_buy_signature=SIG, first_buy_slot=200, first_buy_time=1_759_400_200,
        received_amount=4_000_000_000, spent=(Spend(Asset.SOL, 600_000_000),),
        programs=("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "11111111111111111111111111111111"),
        address_type=AddressType.WALLET,
    )
    values.update(overrides)
    return Buyer(**values)


def _transfer(**overrides) -> Transfer:
    values = dict(
        signature=SIG, slot=100, block_time=1_759_400_100, instruction_path="0", sender=W["B"],
        receiver=W["A"], asset=Asset.SOL, amount=5_000_000_000, decimals=None, depth=1,
    )
    values.update(overrides)
    return Transfer(**values)


def _result(*, buyers=None, transfers=(), unexpanded=(), missing=(), buyers_completeness=None, **meta) -> IngestResult:
    buyers = (_buyer(),) if buyers is None else tuple(buyers)
    completeness = Completeness.derive(
        missing, buyers_completeness or BuyersCompleteness(True, None, "")
    )
    return IngestResult(
        metadata=_meta(wallets_analyzed=len(buyers), **meta),
        completeness=completeness, buyers=buyers, transfers=tuple(transfers), unexpanded=tuple(unexpanded),
    )


# --- T-020: схема ----------------------------------------------------------------------


def test_result_dict_validates_against_schema():
    result = _collect(BASIC, M)
    assert isinstance(result, IngestResult) and result.buyers and result.transfers
    data = to_dict(result)
    _assert_valid(data)
    # схема 1.1 (фіча 002, T-044): to_dict завжди додає ключ delegated; решта ключів незмінна
    assert set(data) == {"metadata", "completeness", "buyers", "transfers", "unexpanded", "delegated"}
    assert data["completeness"]["status"] == "complete"
    assert len(data["buyers"]) == len(result.buyers) == 5
    assert len(data["transfers"]) == len(result.transfers) > 0


@pytest.mark.parametrize(
    ("directory", "mint", "kind", "detail"),
    [
        pytest.param(BASIC, "not-a-valid-address", "invalid_address", None, id="invalid_address"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["MISSING_MINT"], "token_not_found", "account_missing", id="account_missing"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["SYSTEM_OWNED"], "token_not_found", "not_a_mint", id="not_a_mint-system"),
        pytest.param(NOTFOUND, NOTFOUND_CAST["TOKEN_ACCOUNT"], "token_not_found", "not_a_mint", id="not_a_mint-token-account"),
    ],
)
def test_rejection_dict_validates_against_schema(directory, mint, kind, detail):
    outcome = _collect(directory, mint)
    assert isinstance(outcome, Rejection)
    data = to_dict(outcome)
    _assert_valid(data)
    assert set(data) == {"kind", "mint", "detail"}
    assert (data["kind"], data["mint"]) == (kind, mint)
    assert data["detail"] == (detail if detail is not None else outcome.detail)
    assert data["detail"]
    _assert_json_types_only(data)


def test_incomplete_result_validates_and_complete_with_missing_is_rejected_by_schema():
    result = _collect(BASIC, M, failures=[FailAfter(6, RpcRateLimited("429"))])
    assert result.completeness.status.value == "incomplete"
    data = to_dict(result)
    _assert_valid(data)
    assert data["completeness"]["status"] == "incomplete"
    assert data["completeness"]["missing"] or data["completeness"]["buyers"]["complete"] is False

    # Модель не дасть `complete` з непорожнім missing — будуємо dict руками й перевіряємо схему.
    with_missing = _result(missing=(MissingHistory(W["A"], 1, MissingReason.TIMEOUT, SIG),))
    good = to_dict(with_missing)
    _assert_valid(good)
    assert good["completeness"]["status"] == "incomplete" and len(good["completeness"]["missing"]) == 1

    liar = copy.deepcopy(good)
    liar["completeness"]["status"] = "complete"
    _assert_invalid(liar)

    liar = copy.deepcopy(to_dict(_result(
        buyers_completeness=BuyersCompleteness(False, MissingReason.RATE_LIMITED, "cursor")
    )))
    assert liar["completeness"]["status"] == "incomplete"
    liar["completeness"]["status"] = "complete"
    _assert_invalid(liar)  # buyers.complete=false при complete

    liar = copy.deepcopy(to_dict(_result()))
    _assert_valid(liar)
    liar["completeness"]["missing"] = [
        {"wallet": W["A"], "depth": 1, "reason": "timeout", "detail": "x"}
    ]
    _assert_invalid(liar)  # complete з непорожнім missing


# --- T-020: expected.json (рішення власника процесу) ------------------------------------------

_UNASSERTED = "not_asserted"


def _assert_matches_expected(data: dict, expected: dict, *, buyers=None) -> None:
    """Частини `to_dict`, що мають аналог в `expected.json`, збігаються з еталоном."""
    assert data["completeness"]["status"] == expected["completeness"]["status"]
    assert data["completeness"]["missing"] == expected["completeness"]["missing"]
    assert data["completeness"]["buyers"]["complete"] == expected["completeness"]["buyers_complete"]
    assert data["buyers"] == (expected["buyers"] if buyers is None else buyers)
    assert data["transfers"] == expected["transfers"]
    assert len(data["unexpanded"]) == len(expected["unexpanded"])
    for got, want in zip(data["unexpanded"], expected["unexpanded"]):
        skipped = set(want.get(_UNASSERTED, ()))
        assert {k: v for k, v in got.items() if k not in skipped} == {
            k: v for k, v in want.items() if k != _UNASSERTED
        }
    for key, value in expected["config"].items():
        assert data["metadata"][key] == value


def test_expected_fixtures_validate_against_schema():
    # basic
    data = to_dict(_collect(BASIC, M))
    _assert_valid(data)
    assert data["metadata"]["mint"] == EXPECTED["mint"]
    _assert_matches_expected(data, EXPECTED)

    # hub: три випадки конфігурації
    assert {c["name"] for c in HUB_EXPECTED["cases"]} == {"hub_high_degree", "hub_signature_cap", "hub_control"}
    for case in HUB_EXPECTED["cases"]:
        data = to_dict(_collect(HUB, HUB_EXPECTED["mint"], _cfg(case["config"])))
        _assert_valid(data)
        assert data["metadata"]["mint"] == HUB_EXPECTED["mint"], case["name"]
        _assert_matches_expected(data, case, buyers=HUB_EXPECTED["buyers"])

    # corrupt: валідний неповний результат (K2 unavailable, K3 corrupt_data)
    cfg = _cfg(first_buyers_n=3, funding_depth=1)
    data = to_dict(_collect(CORRUPT, CORRUPT_CAST["M"], cfg))
    _assert_valid(data)
    assert data["completeness"]["status"] == "incomplete"
    assert {(m["wallet"], m["reason"]) for m in data["completeness"]["missing"]} == {
        (CORRUPT_CAST["K2"], "unavailable"), (CORRUPT_CAST["K3"], "corrupt_data"),
    }

    # notfound: відмови
    for key, detail in (("MISSING_MINT", "account_missing"), ("SYSTEM_OWNED", "not_a_mint"),
                        ("TOKEN_ACCOUNT", "not_a_mint")):
        data = to_dict(_collect(NOTFOUND, NOTFOUND_CAST[key]))
        _assert_valid(data)
        assert data == {"kind": "token_not_found", "mint": NOTFOUND_CAST[key], "detail": detail}


def test_expected_basic_transfers_carry_all_seven_evidence_fields():
    data = to_dict(_collect(BASIC, M))
    for transfer in data["transfers"]:
        # FR-001-06: відправник, отримувач, актив, сума, час, слот, підпис
        for field in ("sender", "receiver", "asset", "amount", "block_time", "slot", "signature"):
            assert field in transfer
        assert set(transfer) == {"signature", "slot", "block_time", "instruction_path", "sender",
                                 "receiver", "asset", "amount", "decimals", "depth"}


# --- T-020: JSON, детермінізм, round-trip -------------------------------------------------------


def test_to_json_is_deterministic_and_round_trips():
    result = _collect(BASIC, M)
    text = to_json(result)
    assert to_json(result) == text
    assert to_json(_collect(BASIC, M)) == text  # інший прогін з тими ж вхідними — той самий текст
    assert json.loads(text) == to_dict(result)
    _assert_valid(json.loads(text))

    # sort_keys, без зайвих пробілів, ensure_ascii=False
    assert text == json.dumps(to_dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert "\n" not in text and not text.startswith(" ")

    rejection = _collect(NOTFOUND, NOTFOUND_CAST["MISSING_MINT"])
    rtext = to_json(rejection)
    assert json.loads(rtext) == to_dict(rejection)
    assert rtext == f'{{"detail":"account_missing","kind":"token_not_found","mint":"{NOTFOUND_CAST["MISSING_MINT"]}"}}'


def test_to_json_keys_sorted_at_every_level():
    def check(pairs):
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys)
        return dict(pairs)

    seen = []
    json.loads(to_json(_collect(BASIC, M)), object_pairs_hook=lambda pairs: seen.append(len(pairs)) or check(pairs))
    assert len(seen) > 20 and max(seen) >= 10  # обійдено вкладені об'єкти, не порожній вихід


def test_to_json_independent_of_insertion_order():
    missing = (
        MissingHistory(W["B"], 2, MissingReason.TIMEOUT, "b"),
        MissingHistory(W["A"], 1, MissingReason.RATE_LIMITED, "a"),
        MissingHistory(W["A"], 1, MissingReason.UNAVAILABLE, "a2"),
    )
    forward = _result(missing=missing)
    backward = _result(missing=tuple(reversed(missing)))
    assert to_json(forward) == to_json(backward)
    assert to_dict(forward) == to_dict(backward)
    assert [m["reason"] for m in to_dict(forward)["completeness"]["missing"]] == [
        "rate_limited", "unavailable", "timeout"]  # порядок моделі (depth, wallet, reason)


def test_to_json_unicode_is_not_escaped():
    result = _result(buyers_completeness=BuyersCompleteness(False, MissingReason.UNAVAILABLE, "збій: недоступно — 🙂"))
    text = to_json(result)
    assert "збій: недоступно — 🙂" in text and "\\u" not in text
    assert json.loads(text)["completeness"]["buyers"]["detail"] == "збій: недоступно — 🙂"
    _assert_valid(json.loads(text))
    rej = Rejection(RejectKind.INVALID_ADDRESS, "адреса-🙂", "не base58")
    assert json.loads(to_json(rej)) == {"kind": "invalid_address", "mint": "адреса-🙂", "detail": "не base58"}
    assert "адреса-🙂" in to_json(rej)


def test_to_json_rejects_non_finite_numbers():
    # NaN не є JSON; модель такого `elapsed_seconds` не відсікає — серіалізація не мовчить.
    result = _result(elapsed_seconds=float("nan"))
    with pytest.raises(ValueError):
        to_json(result)


# --- Типи та службові поля ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome_name", ["basic", "incomplete", "rejection", "synthetic"])
def test_output_contains_only_plain_json_types(outcome_name):
    outcome = {
        "basic": lambda: _collect(BASIC, M),
        "incomplete": lambda: _collect(BASIC, M, failures=[FailAfter(5, RpcUnavailable("down"))]),
        "rejection": lambda: _collect(BASIC, "bad"),
        "synthetic": lambda: _result(
            transfers=(_transfer(asset=Asset.spl(W["USDC"]), decimals=6),),
            unexpanded=(UnexpandedNode(W["A"], 1, UnexpandedReason.HIGH_DEGREE, 7, 12, False),),
        ),
    }[outcome_name]()
    data = to_dict(outcome)
    assert len(data) >= 3  # не порожній вихід
    _assert_json_types_only(data)
    assert json.loads(json.dumps(data)) == data  # без втрат через стандартний json
    assert json.loads(to_json(outcome)) == data


def test_output_has_no_service_fields_and_no_token():
    result = _collect(BASIC, M)
    data = to_dict(result)
    assert "_token" not in _keys(data)
    assert not any(k.startswith("_") for k in _keys(data))
    assert "_token" not in to_json(result)
    assert "object at 0x" not in to_json(result)
    assert set(data["completeness"]) == {"status", "missing", "buyers"}  # рівно за схемою


def test_status_is_derived_explicitly_in_output():
    complete = to_dict(_result())
    assert complete["completeness"]["status"] == "complete"
    incomplete = to_dict(_result(buyers_completeness=BuyersCompleteness(False, MissingReason.TIMEOUT, "t")))
    assert incomplete["completeness"]["status"] == "incomplete"
    with_missing = to_dict(_result(missing=(MissingHistory(W["A"], 0, MissingReason.CORRUPT_DATA, ""),)))
    assert with_missing["completeness"]["status"] == "incomplete"
    for data in (complete, incomplete, with_missing):
        _assert_valid(data)


def test_all_metadata_fields_present_and_equal_to_model():
    result = _collect(BASIC, M)
    data = to_dict(result)["metadata"]
    meta = dataclasses.asdict(result.metadata)
    assert set(data) == set(meta) == {f.name for f in dataclasses.fields(RunMetadata)}
    assert len(data) == 16
    assert data == meta
    assert data["source"] == "fixture:basic"


def test_to_dict_returns_fresh_independent_structure():
    result = _result(transfers=(_transfer(),))
    first = to_dict(result)
    first["buyers"].append("junk")
    first["metadata"]["mint"] = "changed"
    first["transfers"][0]["amount"] = 0
    second = to_dict(result)
    assert second == to_dict(result)
    assert second["metadata"]["mint"] == M and second["transfers"][0]["amount"] == 5_000_000_000
    assert len(second["buyers"]) == 1


def test_unknown_outcome_type_is_rejected():
    for bad in (None, {}, "x", 1, [], _meta(), _result().completeness):
        with pytest.raises(TypeError):
            to_dict(bad)
        with pytest.raises(TypeError):
            to_json(bad)


# --- Межові значення -------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(MissingReason))
def test_every_missing_reason_serializes_to_schema_value(reason):
    result = _result(
        missing=(MissingHistory(W["A"], 2, reason, "d"),),
        buyers_completeness=BuyersCompleteness(False, reason, "b"),
    )
    data = to_dict(result)
    _assert_valid(data)
    assert data["completeness"]["missing"][0]["reason"] == reason.value
    assert data["completeness"]["buyers"]["reason"] == reason.value
    assert type(data["completeness"]["missing"][0]["reason"]) is str


@pytest.mark.parametrize("reason", list(UnexpandedReason))
@pytest.mark.parametrize("truncated", [True, False])
def test_every_unexpanded_reason_serializes_to_schema_value(reason, truncated):
    node = UnexpandedNode(W["A"], 3, reason, 201, 300, truncated)
    data = to_dict(_result(unexpanded=(node,)))
    _assert_valid(data)
    assert data["unexpanded"] == [{
        "wallet": W["A"], "depth": 3, "reason": reason.value, "counterparties_seen": 201,
        "signatures_seen": 300, "signatures_truncated": truncated,
    }]


@pytest.mark.parametrize("address_type", list(AddressType))
def test_every_address_type_serializes_to_schema_value(address_type):
    data = to_dict(_result(buyers=(_buyer(address_type=address_type),)))
    _assert_valid(data)
    assert data["buyers"][0]["address_type"] == address_type.value
    assert type(data["buyers"][0]["address_type"]) is str


@pytest.mark.parametrize("kind", list(RejectKind))
def test_every_reject_kind_serializes_to_schema_value(kind):
    data = to_dict(Rejection(kind, M, "detail"))
    _assert_valid(data)
    assert data["kind"] == kind.value and type(data["kind"]) is str


def test_asset_formats_sol_and_spl():
    usdc = W["USDC"]
    sol = _transfer(signature=SIG, asset=Asset.SOL, decimals=None)
    spl = _transfer(signature=SIG2, asset=Asset.spl(usdc), decimals=6)
    buyer = _buyer(spent=(Spend(Asset.SOL, 5), Spend(Asset.spl(usdc), 7)))
    data = to_dict(_result(buyers=(buyer,), transfers=(sol, spl)))
    _assert_valid(data)
    assert [t["asset"] for t in data["transfers"]] == ["sol", f"spl:{usdc}"]
    assert [s["asset"] for s in data["buyers"][0]["spent"]] == ["sol", f"spl:{usdc}"]
    _assert_json_types_only(data)


def test_block_time_none_is_null_and_valid():
    data = to_dict(_result(buyers=(_buyer(first_buy_time=None),), transfers=(_transfer(block_time=None),)))
    _assert_valid(data)
    assert data["buyers"][0]["first_buy_time"] is None
    assert data["transfers"][0]["block_time"] is None
    assert "null" in to_json(_result(buyers=(_buyer(first_buy_time=None),)))


def test_decimals_none_for_sol_and_integer_for_spl():
    sol = to_dict(_result(transfers=(_transfer(decimals=None),)))["transfers"][0]
    assert sol["decimals"] is None and "decimals" in sol
    spl = to_dict(_result(transfers=(_transfer(asset=Asset.spl(W["USDC"]), decimals=0),)))["transfers"][0]
    assert spl["decimals"] == 0 and type(spl["decimals"]) is int


def test_empty_lists_are_lists_not_null():
    data = to_dict(_result(buyers=(), transfers=(), unexpanded=()))
    _assert_valid(data)
    assert data["buyers"] == data["transfers"] == data["unexpanded"] == []
    assert data["completeness"]["missing"] == []
    assert data["metadata"]["wallets_analyzed"] == 0
    assert _buyer().programs and to_dict(_result(buyers=(_buyer(programs=()),)))["buyers"][0]["programs"] == []


def test_tuples_become_lists():
    data = to_dict(_result(transfers=(_transfer(),)))
    assert type(data["buyers"]) is list and type(data["buyers"][0]["spent"]) is list
    assert type(data["buyers"][0]["programs"]) is list and type(data["transfers"]) is list


@pytest.mark.parametrize("flags", [(True, False), (False, True), (True, True)], ids=["resumed", "cache", "both"])
def test_resumed_and_served_from_cache_true(flags):
    resumed, cached = flags
    data = to_dict(_result(resumed=resumed, served_from_cache=cached))
    _assert_valid(data)
    assert (data["metadata"]["resumed"], data["metadata"]["served_from_cache"]) == (resumed, cached)


def test_cache_copy_and_resume_serialize_with_flags():
    service = _service(BASIC)
    first = service.collect(M)
    second = service.collect(M)
    assert (to_dict(first)["metadata"]["served_from_cache"], to_dict(second)["metadata"]["served_from_cache"]) == (False, True)
    a, b = to_dict(first), to_dict(second)
    a["metadata"]["served_from_cache"] = b["metadata"]["served_from_cache"] = None
    assert a == b
    _assert_valid(to_dict(second))


def test_step4_failure_result_is_valid_incomplete():
    # збій getAccountInfo (крок 4): `_unverified` — покупців немає, incomplete
    result = _collect(BASIC, M, failures=[FailAfter(1, RpcUnavailable("boom"))])
    assert isinstance(result, IngestResult) and result.buyers == ()
    data = to_dict(result)
    _assert_valid(data)
    assert data["buyers"] == [] and data["transfers"] == [] and data["unexpanded"] == []
    assert data["completeness"]["status"] == "incomplete"
    assert data["completeness"]["buyers"]["complete"] is False
    assert data["completeness"]["buyers"]["reason"] == "unavailable"
    assert M in data["completeness"]["buyers"]["detail"]
    assert data["metadata"]["wallets_analyzed"] == 0
    _assert_json_types_only(data)
    assert json.loads(to_json(result)) == data


def test_buyers_completeness_reason_null_when_complete():
    data = to_dict(_result())
    assert data["completeness"]["buyers"] == {"complete": True, "reason": None, "detail": ""}
    _assert_valid(data)


def test_non_default_metadata_values_round_trip():
    data = to_dict(_result(
        first_buyers_n=500, funding_depth=1, config_version=12, counterparty_threshold=1,
        max_signatures_per_wallet=1, collect_spl_inbound=False, time_budget_seconds=0.5,
        elapsed_seconds=0, rpc_calls=0, transactions_scanned=0, source="http", analyzed_at=0,
    ))
    _assert_valid(data)
    meta = data["metadata"]
    assert (meta["first_buyers_n"], meta["funding_depth"], meta["config_version"], meta["source"]) == (500, 1, 12, "http")
    assert meta["collect_spl_inbound"] is False and meta["time_budget_seconds"] == 0.5


def test_depth_zero_in_missing_and_unexpanded_is_valid():
    data = to_dict(_result(
        missing=(MissingHistory(W["A"], 0, MissingReason.BUDGET_EXHAUSTED, "x"),),
        unexpanded=(UnexpandedNode(W["A"], 0, UnexpandedReason.SIGNATURE_CAP, 1, 300, True),),
    ))
    _assert_valid(data)


def test_to_dict_does_not_modify_the_outcome():
    result = _collect(BASIC, M)
    before = copy.deepcopy(result)
    assert to_dict(result) and to_json(result)
    assert result == before


# --- decimals: u8 (0..255), модель і схема збігаються (рев'ю T-020, блокер 1) -------------------------


def _basic_with_decimals(tmp_path: Path, decimals: int) -> Path:
    """Копія basic/rpc.json, де всі `decimals: 6` замінено на `decimals` (токен-баланси й інструкції узгоджені)."""
    def walk(x):
        if isinstance(x, dict):
            return {k: (decimals if k == "decimals" and v == 6 else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    data = walk(json.loads((BASIC / "rpc.json").read_text()))
    directory = tmp_path / f"basic{decimals}"
    directory.mkdir()
    (directory / "rpc.json").write_text(json.dumps(data))
    return directory


@pytest.mark.parametrize("decimals", [19, 255])
def test_large_spl_decimals_pass_model_and_schema_via_full_path(tmp_path, decimals):
    result = _collect(_basic_with_decimals(tmp_path, decimals), M)
    assert isinstance(result, IngestResult)
    spl = [t for t in result.transfers if t.asset != Asset.SOL]
    assert spl and {t.decimals for t in spl} == {decimals}
    data = to_dict(result)
    _assert_valid(data)
    assert {t["decimals"] for t in data["transfers"] if t["asset"] != "sol"} == {decimals}
    assert json.loads(to_json(result)) == data
    assert data["completeness"]["status"] == "complete"


def test_decimals_256_is_corrupt_data_not_exception_and_never_in_output(tmp_path):
    result = _collect(_basic_with_decimals(tmp_path, 256), M)  # не піднімає виняток
    assert isinstance(result, IngestResult)
    data = to_dict(result)
    _assert_valid(data)
    assert data["completeness"]["status"] == "incomplete"
    # decimals=256 стоїть у транзакціях mint → перелічення покупців чесно неповне (corrupt_data/non_integer)
    buyers = data["completeness"]["buyers"]
    assert buyers["complete"] is False and buyers["reason"] == "corrupt_data"
    assert "non_integer" in buyers["detail"]
    assert {m["reason"] for m in data["completeness"]["missing"]} <= {"corrupt_data"}
    assert all(t["decimals"] is None or t["decimals"] <= 255 for t in data["transfers"])


@pytest.mark.parametrize("decimals", [0, 18, 19, 255])
def test_model_and_schema_accept_decimals_up_to_255(decimals):
    t = _transfer(asset=Asset.spl(W["USDC"]), decimals=decimals)
    _assert_valid(to_dict(_result(transfers=(t,))))


def test_model_and_schema_reject_decimals_256():
    with pytest.raises(ValueError):
        _transfer(asset=Asset.spl(W["USDC"]), decimals=256)
    with pytest.raises(ValueError):
        _transfer(asset=Asset.spl(W["USDC"]), decimals=-1)
    data = to_dict(_result(transfers=(_transfer(asset=Asset.spl(W["USDC"]), decimals=255),)))
    data["transfers"][0]["decimals"] = 256
    _assert_invalid(data)  # схема теж відхиляє


# --- missing[].detail — посилання на первинний доказ (рев'ю T-020, блокер 2) -------------------------


def test_corrupt_scenario_missing_detail_matches_independent_defect_signatures():
    defects = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["defects"]
    (null_sig,) = defects["null_transaction"]
    (no_meta_sig,) = defects["missing_meta"]
    data = to_dict(_collect(CORRUPT, CORRUPT_CAST["M"], _cfg(first_buyers_n=3, funding_depth=1)))
    got = {(m["wallet"], m["reason"]): m["detail"] for m in data["completeness"]["missing"]}
    assert set(got) == {(CORRUPT_CAST["K2"], "unavailable"), (CORRUPT_CAST["K3"], "corrupt_data")}
    assert got[(CORRUPT_CAST["K2"], "unavailable")] == null_sig  # detail — підпис транзакції
    assert no_meta_sig in got[(CORRUPT_CAST["K3"], "corrupt_data")]
    assert all(m["detail"] != m["wallet"] and m["detail"] for m in data["completeness"]["missing"])


def test_missing_detail_is_preserved_verbatim_in_distinct_order():
    # detail підібрано так, що порядок за detail (або за wallet) дає іншу послідовність, ніж (depth, wallet, reason)
    missing = (
        MissingHistory(W["B"], 2, MissingReason.TIMEOUT, "aaa-first-by-detail"),
        MissingHistory(W["A"], 1, MissingReason.UNAVAILABLE, "zzz-last-by-detail"),
        MissingHistory(W["A"], 1, MissingReason.RATE_LIMITED, "mmm-middle \u00e9 збій"),
        MissingHistory(W["C"], 0, MissingReason.CORRUPT_DATA, SIG),
    )
    data = to_dict(_result(missing=missing))
    got = [(m["depth"], m["wallet"], m["reason"], m["detail"]) for m in data["completeness"]["missing"]]
    assert got == [(m.depth, m.wallet, m.reason.value, m.detail) for m in _result(missing=missing).completeness.missing]
    assert [m["detail"] for m in data["completeness"]["missing"]] == [
        SIG, "mmm-middle \u00e9 збій", "zzz-last-by-detail", "aaa-first-by-detail"]
    _assert_valid(data)


def test_buyers_completeness_detail_is_preserved():
    data = to_dict(_result(buyers_completeness=BuyersCompleteness(False, MissingReason.TIMEOUT, "cursor:abc")))
    assert data["completeness"]["buyers"]["detail"] == "cursor:abc"


# --- дробові секунди не обрізаються (рев'ю T-020, нотатка) --------------------------------------------


def test_fractional_elapsed_seconds_are_exact_in_dict_and_json():
    clock = FakeClock(advance_per_call=0.25)
    source = FixtureRpcSource(BASIC, clock=clock)
    result = IngestService(_cfg(time_budget_seconds=33.5), source, clock=clock).collect(M)
    elapsed = result.metadata.elapsed_seconds
    assert elapsed != int(elapsed) and elapsed == 0.25 * result.metadata.rpc_calls
    data = to_dict(result)
    assert data["metadata"]["elapsed_seconds"] == elapsed
    assert data["metadata"]["time_budget_seconds"] == 33.5
    assert json.loads(to_json(result))["metadata"]["elapsed_seconds"] == elapsed
    assert to_dict(_result(elapsed_seconds=1.5, time_budget_seconds=0.125))["metadata"]["elapsed_seconds"] == 1.5
