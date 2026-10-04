# verifies: FR-002-19, FR-002-21
"""Розширення результату 001 для swap-and-send (T-044): модель, схема 1.1, серіалізація.

Що доводять тести:
- `DelegatedAnalysis.derive(links, unpaired, buyers)` — повнота аналізу ПОХІДНА від повноти
  перелічення покупців (`complete == buyers.complete`, `reason`/`detail` копіюються), списки
  впорядковані за ключами контракту, дублі відхиляються (FR-002-19);
- пряма побудова з `complete`, що суперечить `reason`, — `ValueError`; узгоджена пряма побудова —
  `TypeError` (лише `derive` або `NOT_ANALYZED`);
- `IngestResult.delegated` за умовчанням — `NOT_ANALYZED` (`complete=False`, `reason="not_analyzed"`):
  результат без аналізу каже це, а не «зв'язків немає» (принцип V);
- схема 1.1 приймає документи без `delegated` і з ним, відхиляє розбіжність `delegated.complete`
  з `completeness.buyers.complete` (FR-002-21, зворотна сумісність);
- SC-006: `to_dict` без ключа `delegated` побайтово (sha256 канонічного JSON) дорівнює знімку,
  знятому ДО внесення змін (basic N=5,d=3; hub — три конфіги; corrupt N=3,d=1); файли
  `expected.json` наявних сценаріїв — без змін;
- `from_dict` читає `delegated` (відсутній → `NOT_ANALYZED`) і обертається без втрат.

Мережі немає: усе з записаних фікстур або в пам'яті.
"""

import copy
import dataclasses
import hashlib
import json
import random
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import unmask.ingest.model as model
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
    RunMetadata,
    Spend,
    Transfer,
)
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.serialize import from_dict, to_dict, to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SCHEMA_PATH = ROOT / "specs/001-onchain-data-ingest/contracts/ingest-result.schema.json"
SHIPPED = ROOT / "config" / "ingest.yaml"
BASIC_EXPECTED = json.loads((SCENARIOS / "basic" / "expected.json").read_text())
HUB_EXPECTED = json.loads((SCENARIOS / "hub" / "expected.json").read_text())
CORRUPT_CAST = json.loads((SCENARIOS / "corrupt" / "rpc.json").read_text())["_meta"]["cast"]
W = BASIC_EXPECTED["wallets"]
SIGS = [b["first_buy_signature"] for b in BASIC_EXPECTED["buyers"]]
NOT_ANALYZED = "not_analyzed"


# --- SC-006: знімки, зняті ДО змін коду T-044 (HEAD e11a06e, робоче дерево ingest чисте) -----------
# Команда зняття: канонічний JSON (`sort_keys`, `ensure_ascii=False`, `separators=(",", ":")`)
# від `to_dict(collect(...))` з видаленим ключем `delegated` (до змін його ще не було), sha256.

PRE_CHANGE_TO_DICT_SHA256 = {
    "basic": "5a84e77da7c83eaa3a77661f8bd8cd974331f612c0c81c7668ca3333ef7c3163",
    "hub_high_degree": "976e65dc87f5948877c4afca9ac999da075bd2a93e5f713e318a1dbf8c55fd34",
    "hub_signature_cap": "17ea0ffb7b23639353748410d58c9899f200a7eb08b5e82871f8137b3d63c328",
    "hub_control": "9ab2196793aafefd61775c123143522a49b69d053d1554ee50be409fb09687c2",
    "corrupt": "6a8be689c8efe72c06ce9555afad5c0e29509ebacb0c01ae789dbda93b597831",
}

PRE_CHANGE_EXPECTED_JSON_SHA256 = {
    "basic": "ac0376590b705084874d70361a0a1fe94e19d6ac58ac8f6076b74c5b3aea6ac4",
    "hub": "5b52c186d7ed11b1db9a964b655f86a402f0cac03b73b9d3c61a358f82ef8f5d",
    "swapsend": "3626639a407d9a645380d7366a04cf34b82ec9530ef57e950b3c9f1c2fa4fa4c",
}


# --- помічники ------------------------------------------------------------------------------------


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _errors(instance) -> list[str]:
    validator = Draft202012Validator(_schema())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(instance)]


def _cfg(values: dict):
    return dataclasses.replace(load_config(SHIPPED), **values)


def _collect(directory: str, mint: str, values: dict):
    clock = FakeClock()
    source = FixtureRpcSource(SCENARIOS / directory, clock=clock)
    return IngestService(_cfg(values), source, clock=clock).collect(mint)


def _sc006_outcomes() -> dict:
    out = {"basic": _collect("basic", BASIC_EXPECTED["mint"], BASIC_EXPECTED["config"])}
    for case in HUB_EXPECTED["cases"]:
        out[case["name"]] = _collect("hub", HUB_EXPECTED["mint"], {**BASIC_EXPECTED["config"], **case["config"]})
    out["corrupt"] = _collect(
        "corrupt", CORRUPT_CAST["M"], {**BASIC_EXPECTED["config"], "first_buyers_n": 3, "funding_depth": 1}
    )
    return out


def _canonical_sha256(doc: dict) -> str:
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _complete() -> BuyersCompleteness:
    return BuyersCompleteness(True, None, "")


def _incomplete(reason=MissingReason.TIMEOUT, detail="mint history cut at slot 7") -> BuyersCompleteness:
    return BuyersCompleteness(False, reason, detail)


def _link(sig=0, slot=210, payer="A", receiver="B", block_time=1_759_400_210):
    return model.DelegatedLink(SIGS[sig], slot, block_time, W[payer], W[receiver])


def _cand(sig, slot, wallet, side, detail, block_time=1_759_400_230):
    return model.UnpairedCandidate(SIGS[sig], slot, block_time, W[wallet], model.DelegatedSide(side), detail)


def _group_2x2(sig=1, slot=230):
    d = "payers=2 receivers=2"
    return [
        _cand(sig, slot, "C", "payer", d), _cand(sig, slot, "D", "payer", d),
        _cand(sig, slot, "E", "receiver", d), _cand(sig, slot, "F", "receiver", d),
    ]


def _group_1x2(sig=2, slot=240):
    d = "payers=1 receivers=2"
    return [_cand(sig, slot, "G", "payer", d), _cand(sig, slot, "K", "receiver", d),
            _cand(sig, slot, "X", "receiver", d)]


def _result(completeness_buyers: BuyersCompleteness, delegated=None, missing=()) -> IngestResult:
    buyers = (
        Buyer(W["P1"], 1, SIGS[0], 200, 1_759_400_200, 4_000, (Spend(Asset.SOL, 600),), (), AddressType.WALLET),
    )
    transfers = (Transfer(SIGS[3], 100, None, "0", W["C"], W["P1"], Asset.SOL, 5_000, None, 1),)
    meta = RunMetadata(
        mint=W["M"], analyzed_at=1_759_500_000, wallets_analyzed=1, config_version=3, first_buyers_n=5,
        funding_depth=3, counterparty_threshold=200, max_signatures_per_wallet=300, collect_spl_inbound=True,
        time_budget_seconds=40.0, elapsed_seconds=1.5, rpc_calls=7, transactions_scanned=3,
        source="fixture:basic", resumed=False, served_from_cache=False)
    completeness = Completeness.derive(missing, completeness_buyers)
    if delegated is None:
        return IngestResult(meta, completeness, buyers, transfers, ())
    return IngestResult(meta, completeness, buyers, transfers, (), delegated)


def _rich_delegated(buyers: BuyersCompleteness):
    return model.DelegatedAnalysis.derive(
        [_link(0, 210, "A", "B"), _link(3, 250, "P2", "P3", None)], _group_2x2() + _group_1x2(), buyers)


# --- модель: derive -------------------------------------------------------------------------------


def test_derive_mirrors_buyers_completeness_and_reason():
    links = [_link(0, 210, "A", "B")]
    unpaired = _group_2x2()
    full = model.DelegatedAnalysis.derive(links, unpaired, _complete())
    assert (full.complete, full.reason, full.detail) == (True, None, "")
    for reason in MissingReason:
        part = model.DelegatedAnalysis.derive(links, unpaired, _incomplete(reason, f"cut: {reason.value}"))
        assert part.complete is False
        assert part.reason == reason and part.reason != NOT_ANALYZED
        assert part.detail == f"cut: {reason.value}"
        # зв'язки, знайдені в неповному вікні, зберігаються: неповнота — окремий прапорець, не «зв'язків немає»
        assert part.links == tuple(links) and set(part.unpaired) == set(unpaired)
    # порожній аналіз над неповним переліченням — НЕ «чисто»
    empty = model.DelegatedAnalysis.derive((), (), _incomplete(MissingReason.CORRUPT_DATA, "tx X"))
    assert (empty.complete, empty.reason, empty.links, empty.unpaired) == (False, MissingReason.CORRUPT_DATA, (), ())


def test_derive_orders_links_and_unpaired_by_contract_keys():
    """Контракт: links за (slot, signature, payer, receiver), unpaired за (slot, signature, wallet, side).

    Дані підібрані так, щоб кожна компонента ключа мала вирішальну пару і жоден інший порядок не
    збігався з контрактним: менший slot має БІЛЬШИЙ підпис; в одному slot порядок підписів не збігається
    ні з порядком платників, ні отримувачів, ні гаманців, ні сторін. Порядок base58: підписи
    SIGS[4] < SIGS[1] < SIGS[3] < SIGS[0] < SIGS[2]; гаманці U < K < S < G < D < P1 < B < A < C < X < P3.
    Еталон записано явно, а не обчислено тим самим ключем.
    """
    assert sorted(range(5), key=lambda i: SIGS[i]) == [4, 1, 3, 0, 2]
    order = ["U", "K", "S", "G", "D", "P1", "B", "A", "C", "X", "P3"]
    assert sorted(order, key=lambda k: W[k]) == order

    l_slot100 = _link(2, 100, "U", "P3")  # найменший slot, найбільший підпис
    l_sig4 = _link(4, 230, "X", "K")
    l_sig1 = _link(1, 230, "K", "X")
    l_sig3 = _link(3, 230, "A", "S")
    expected_links = (l_slot100, l_sig4, l_sig1, l_sig3)
    # у slot 230: за підписом 4,1,3; за платником 3(A),1(K),4(X); за отримувачем 4(K),3(S),1(X)

    def c(sig, slot, wallet, side, detail):
        return _cand(sig, slot, wallet, side, detail)

    d22, d12 = "payers=2 receivers=2", "payers=1 receivers=2"
    expected_unpaired = (
        # slot 100, найбільший підпис; у межах підпису за гаманцем: U(payer) B C P3(payer) ≠ порядок сторін
        c(2, 100, "U", "payer", d22), c(2, 100, "B", "receiver", d22),
        c(2, 100, "C", "receiver", d22), c(2, 100, "P3", "payer", d22),
        # slot 230, підпис SIGS[4]: K D X(payer)
        c(4, 230, "K", "receiver", d12), c(4, 230, "D", "receiver", d12), c(4, 230, "X", "payer", d12),
        # slot 230, підпис SIGS[1]: S(payer) G P1 A(payer) — гаманці перемежовуються з групою SIGS[4]
        c(1, 230, "S", "payer", d22), c(1, 230, "G", "receiver", d22),
        c(1, 230, "P1", "receiver", d22), c(1, 230, "A", "payer", d22),
    )
    links, unpaired = list(expected_links), list(expected_unpaired)
    rnd = random.Random(44)
    for _ in range(30):
        rnd.shuffle(links)
        rnd.shuffle(unpaired)
        got_links = model.DelegatedAnalysis.derive(iter(links), (), _complete()).links
        got_unpaired = model.DelegatedAnalysis.derive((), iter(unpaired), _complete()).unpaired
        assert got_links == expected_links
        assert got_unpaired == expected_unpaired
        assert isinstance(got_links, tuple) and isinstance(got_unpaired, tuple)
    # ключі сортування моделі — рівно контрактні
    assert model.link_sort_key(l_sig4) == (230, SIGS[4], W["X"], W["K"])
    assert model.unpaired_sort_key(expected_unpaired[0]) == (100, SIGS[2], W["U"], "payer")
    # те саме через серіалізацію: документ несе контрактний порядок
    # (зв'язок і кандидати з тим самим підписом заборонені — тому два окремі результати)
    doc = to_dict(_result(_complete(), model.DelegatedAnalysis.derive(links, (), _complete())))
    assert [x["signature"] for x in doc["delegated"]["links"]] == [SIGS[2], SIGS[4], SIGS[1], SIGS[3]]
    doc = to_dict(_result(_complete(), model.DelegatedAnalysis.derive((), unpaired, _complete())))
    assert [x["wallet"] for x in doc["delegated"]["unpaired"]] == [W[k] for k in (
        "U", "B", "C", "P3", "K", "D", "X", "S", "G", "P1", "A")]


def test_derive_rejects_duplicates_instead_of_silently_merging():
    with pytest.raises(ValueError, match="duplicate"):
        model.DelegatedAnalysis.derive([_link(0), _link(0)], (), _complete())
    # одна транзакція — один зв'язок (правило 1:1), навіть з іншою парою
    with pytest.raises(ValueError, match="duplicate"):
        model.DelegatedAnalysis.derive([_link(0, 210, "A", "B"), _link(0, 210, "C", "D")], (), _complete())
    group = _group_2x2()
    with pytest.raises(ValueError, match="duplicate"):
        model.DelegatedAnalysis.derive((), group + [group[0]], _complete())


def test_derive_rejects_unpaired_group_inconsistent_with_its_detail():
    group = _group_2x2()
    with pytest.raises(ValueError, match="payers=2 receivers=2"):
        model.DelegatedAnalysis.derive((), group[:-1], _complete())  # заявлено 2 отримувачі, є 1
    with pytest.raises(ValueError):
        model.DelegatedAnalysis.derive((), group + [_cand(1, 230, "G", "receiver", "payers=2 receivers=2")],
                                       _complete())
    mixed = group[:3] + [_cand(1, 230, "F", "receiver", "payers=2 receivers=3")]
    with pytest.raises(ValueError):
        model.DelegatedAnalysis.derive((), mixed, _complete())
    other_slot = group[:3] + [_cand(1, 231, "F", "receiver", "payers=2 receivers=2")]
    with pytest.raises(ValueError):
        model.DelegatedAnalysis.derive((), other_slot, _complete())
    # той самий гаманець і платник, і отримувач однієї транзакції — неможливо за правилом R-2
    both_sides = group[:3] + [_cand(1, 230, "C", "receiver", "payers=2 receivers=2")]
    with pytest.raises(ValueError, match="both sides"):
        model.DelegatedAnalysis.derive((), both_sides, _complete())


def test_derive_rejects_link_and_unpaired_on_the_same_transaction():
    with pytest.raises(ValueError, match="both"):
        model.DelegatedAnalysis.derive([_link(1, 230, "A", "B")], _group_2x2(sig=1, slot=230), _complete())


def test_derive_requires_buyers_completeness_and_typed_items():
    with pytest.raises(TypeError):
        model.DelegatedAnalysis.derive((), (), True)
    with pytest.raises(TypeError):
        model.DelegatedAnalysis.derive([("sig", 1)], (), _complete())
    with pytest.raises(TypeError):
        model.DelegatedAnalysis.derive((), [_link()], _complete())
    with pytest.raises(TypeError):
        model.DelegatedAnalysis.derive("abc", (), _complete())


def test_link_and_candidate_validate_themselves():
    assert model.DelegatedSide("payer") is model.DelegatedSide.PAYER
    assert {s.value for s in model.DelegatedSide} == {"payer", "receiver"}
    with pytest.raises(ValueError):
        _link(0, 210, "A", "A")  # receiver != payer
    with pytest.raises(ValueError):
        _link(0, -1)
    with pytest.raises(TypeError):
        _link(0, True)
    with pytest.raises(ValueError):
        _link(0, 1, block_time=-5)
    with pytest.raises(ValueError):
        model.DelegatedLink("", 1, None, W["A"], W["B"])
    with pytest.raises(ValueError):
        model.UnpairedCandidate(SIGS[0], 1, None, W["A"], "middle", "payers=2 receivers=2")
    assert model.UnpairedCandidate(SIGS[0], 1, None, W["A"], "payer", "payers=2 receivers=1").side \
        is model.DelegatedSide.PAYER
    for bad in ("payers=1 receivers=1",  # 1:1 — це зв'язок, а не кандидат
                "payers=0 receivers=2", "payers=2 receivers=0",
                "payers=2  receivers=2", "payers=2 receivers=2 ", "payers=x receivers=2", "", "p=2 r=2"):
        with pytest.raises(ValueError):
            model.UnpairedCandidate(SIGS[0], 1, None, W["A"], "payer", bad)
    # кінцевий перенос рядка — не той самий шаблон (Python `$` приймає "...\n", модель — ні)
    for bad in ("payers=2 receivers=2\n", "\npayers=2 receivers=2", "payers=2 receivers=2\r\n"):
        with pytest.raises(ValueError):
            model.UnpairedCandidate(SIGS[0], 1, None, W["A"], "payer", bad)
    with pytest.raises(TypeError):
        model.UnpairedCandidate(SIGS[0], 1, None, W["A"], "payer", None)


# --- модель: пряма побудова і NOT_ANALYZED --------------------------------------------------------


def test_direct_construction_with_contradictory_complete_and_reason_rejected():
    DA = model.DelegatedAnalysis
    with pytest.raises(ValueError):
        DA((), (), True, MissingReason.TIMEOUT, "")
    with pytest.raises(ValueError):
        DA((), (), True, NOT_ANALYZED, "")
    with pytest.raises(ValueError):
        DA((), (), False, None, "")
    with pytest.raises(ValueError):
        DA((), (), False, "bogus", "")
    # узгоджена, але пряма побудова — теж ні: лише derive або NOT_ANALYZED
    with pytest.raises(TypeError, match="derive"):
        DA((), (), True, None, "")
    with pytest.raises(TypeError, match="derive"):
        DA((), (), False, MissingReason.TIMEOUT, "x")
    # обхід через dataclasses.replace: інваріанти перевіряються й тоді
    with pytest.raises(ValueError):
        dataclasses.replace(DA.NOT_ANALYZED, complete=True)
    with pytest.raises(ValueError):
        dataclasses.replace(DA.NOT_ANALYZED, links=(_link(),))  # «не аналізували», але зв'язок є
    with pytest.raises(ValueError):
        dataclasses.replace(DA.NOT_ANALYZED, unpaired=tuple(_group_2x2()))
    derived = DA.derive((), (), _complete())
    with pytest.raises(ValueError):
        dataclasses.replace(derived, reason=MissingReason.TIMEOUT)


def test_not_analyzed_default_is_incomplete_with_reason_not_analyzed():
    na = model.DelegatedAnalysis.NOT_ANALYZED
    assert (na.links, na.unpaired, na.complete, na.reason) == ((), (), False, NOT_ANALYZED)
    assert not isinstance(na.reason, MissingReason)
    result = _result(_complete())  # побудовано без аналізу
    assert result.delegated is na
    assert result.delegated.complete is False  # навіть коли покупці повні — «не аналізували» ≠ «чисто»
    doc = to_dict(result)["delegated"]
    assert doc == {"links": [], "unpaired": [], "complete": False, "reason": NOT_ANALYZED, "detail": na.detail}
    assert type(doc["reason"]) is str
    assert [f.name for f in dataclasses.fields(IngestResult)][-1] == "delegated"


def test_ingest_result_rejects_delegated_disagreeing_with_buyers_completeness():
    with pytest.raises(ValueError, match="delegated"):
        _result(_complete(), model.DelegatedAnalysis.derive((), (), _incomplete()))
    with pytest.raises(ValueError, match="delegated"):
        _result(_incomplete(), model.DelegatedAnalysis.derive((), (), _complete()))
    with pytest.raises(ValueError, match="delegated"):  # та сама повнота, інша причина
        _result(_incomplete(MissingReason.TIMEOUT), model.DelegatedAnalysis.derive(
            (), (), _incomplete(MissingReason.RATE_LIMITED)))
    with pytest.raises(TypeError):
        _result(_complete(), {"links": []})
    # NOT_ANALYZED сумісний з будь-якою повнотою покупців
    assert _result(_incomplete(), model.DelegatedAnalysis.NOT_ANALYZED).delegated.reason == NOT_ANALYZED
    ok = _result(_incomplete(), _rich_delegated(_incomplete()))
    assert ok.delegated.complete is False and len(ok.delegated.links) == 2


# --- схема 1.1 ------------------------------------------------------------------------------------


def test_schema_1_1_accepts_documents_without_delegated_and_with_delegated():
    schema = _schema()
    assert schema["$id"] == "https://unmask.local/schemas/001/ingest-result-1.1.json"
    assert schema["title"] == "unmask ingest outcome (feature 001, schema 1.1)"
    Draft202012Validator.check_schema(schema)
    result_def = schema["$defs"]["result"]
    assert "delegated" in result_def["properties"]
    assert "delegated" not in result_def["required"]
    assert result_def["additionalProperties"] is False
    assert result_def["required"] == ["metadata", "completeness", "buyers", "transfers", "unexpanded"]

    for buyers in (_complete(), _incomplete()):
        full = to_dict(_result(buyers, _rich_delegated(buyers)))
        assert full["delegated"]["links"] and full["delegated"]["unpaired"]
        # точні JSON-типи: StrEnum (reason, side) не просочується у вихід
        assert type(full["delegated"]["reason"]) in (str, type(None))
        assert all(type(c["side"]) is str for c in full["delegated"]["unpaired"])
        assert _errors(full) == []
        old = {k: v for k, v in full.items() if k != "delegated"}  # документ 1.0
        assert _errors(old) == []
        assert _errors(to_dict(_result(buyers))) == []  # NOT_ANALYZED
    for name, outcome in _sc006_outcomes().items():
        assert _errors(to_dict(outcome)) == [], name


def test_schema_1_1_rejects_malformed_delegated_parts():
    buyers = _complete()
    base = to_dict(_result(buyers, _rich_delegated(buyers)))

    def broken(mutate):
        doc = copy.deepcopy(base)
        mutate(doc["delegated"])
        return _errors(doc)

    assert broken(lambda d: d["links"][0].update(extra=1))
    assert broken(lambda d: d["links"][0].pop("receiver"))
    assert broken(lambda d: d["links"][0].update(payer="0OIl"))
    assert broken(lambda d: d["unpaired"][0].update(side="middle"))
    assert broken(lambda d: d["unpaired"][0].update(detail="payers=2,receivers=2"))
    assert broken(lambda d: d["unpaired"][0].update(block_time=-1))
    assert broken(lambda d: d.update(extra=True))
    assert broken(lambda d: d.pop("detail"))
    assert broken(lambda d: d.update(reason="bogus"))


def test_schema_rejects_delegated_complete_mismatch_with_buyers():
    def doc(buyers_complete: bool, delegated: dict) -> dict:
        b = _complete() if buyers_complete else _incomplete()
        out = to_dict(_result(b))
        out["delegated"] = {"links": [], "unpaired": [], "detail": "", **delegated}
        return out

    assert _errors(doc(True, {"complete": True, "reason": None})) == []
    assert _errors(doc(False, {"complete": False, "reason": "timeout"})) == []
    assert _errors(doc(True, {"complete": False, "reason": "timeout"}))  # аналіз неповний, перелічення повне
    assert _errors(doc(False, {"complete": True, "reason": None}))  # «чисто» над неповним переліченням
    # complete ⇔ reason is None
    assert _errors(doc(True, {"complete": True, "reason": "timeout"}))
    assert _errors(doc(False, {"complete": False, "reason": None}))
    assert _errors(doc(True, {"complete": True, "reason": NOT_ANALYZED}))
    # not_analyzed звільнений від перехресного інваріанту з будь-якою повнотою покупців
    assert _errors(doc(True, {"complete": False, "reason": NOT_ANALYZED})) == []
    assert _errors(doc(False, {"complete": False, "reason": NOT_ANALYZED})) == []


# --- SC-006: байти 001 не змінюються --------------------------------------------------------------


def test_to_dict_without_delegated_key_is_byte_identical_to_pre_change_snapshot():
    outcomes = _sc006_outcomes()
    assert set(outcomes) == set(PRE_CHANGE_TO_DICT_SHA256)
    for name, outcome in outcomes.items():
        doc = to_dict(outcome)
        assert "delegated" in doc, name  # to_dict завжди емітує ключ (research R-3)
        assert list(doc)[:5] == ["metadata", "completeness", "buyers", "transfers", "unexpanded"]
        del doc["delegated"]
        assert _canonical_sha256(doc) == PRE_CHANGE_TO_DICT_SHA256[name], name
        # те саме через канонічний текст to_json
        reparsed = json.loads(to_json(outcome))
        del reparsed["delegated"]
        assert _canonical_sha256(reparsed) == PRE_CHANGE_TO_DICT_SHA256[name], name


def test_expected_json_files_of_existing_scenarios_unchanged():
    for name, digest in PRE_CHANGE_EXPECTED_JSON_SHA256.items():
        data = (SCENARIOS / name / "expected.json").read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name


# --- from_dict ------------------------------------------------------------------------------------


def test_from_dict_round_trips_with_delegated():
    for buyers in (_complete(), _incomplete(MissingReason.RATE_LIMITED, "cut")):
        for delegated in (_rich_delegated(buyers), model.DelegatedAnalysis.derive((), (), buyers),
                          model.DelegatedAnalysis.NOT_ANALYZED, None):
            result = _result(buyers, delegated)
            doc = to_dict(result)
            back = from_dict(doc)
            assert back == result
            assert back.delegated == result.delegated
            assert to_dict(back) == doc
            assert from_dict(json.loads(to_json(result))) == result
            assert isinstance(back.delegated.links, tuple) and isinstance(back.delegated.unpaired, tuple)
            assert all(isinstance(c.side, model.DelegatedSide) for c in back.delegated.unpaired)
    for name, outcome in _sc006_outcomes().items():
        assert from_dict(to_dict(outcome)) == outcome, name


def test_from_dict_without_delegated_key_gives_not_analyzed():
    for buyers in (_complete(), _incomplete()):
        doc = to_dict(_result(buyers, _rich_delegated(buyers)))
        del doc["delegated"]
        assert from_dict(doc).delegated is model.DelegatedAnalysis.NOT_ANALYZED


def test_from_dict_rejects_delegated_that_contradicts_buyers_or_contract():
    buyers = _incomplete(MissingReason.TIMEOUT, "cut")
    base = to_dict(_result(buyers, _rich_delegated(buyers)))

    def reject(mutate, exc=ValueError, match=None):
        doc = copy.deepcopy(base)
        mutate(doc["delegated"])
        with pytest.raises(exc, match=match):
            from_dict(doc)

    reject(lambda d: d.update(complete=True, reason=None), match="delegated")
    reject(lambda d: d.update(reason="rate_limited"), match="delegated")
    reject(lambda d: d.update(detail="other"), match="delegated")
    reject(lambda d: d.update(reason="bogus"), match="delegated")
    reject(lambda d: d.update(complete=1), exc=TypeError)
    reject(lambda d: d.update(reason=5), exc=TypeError)
    reject(lambda d: d.update(links=tuple(d["links"])), exc=TypeError)
    reject(lambda d: d["links"].reverse(), match="order")  # неканонічний порядок не переписується мовчки
    reject(lambda d: d["unpaired"].reverse(), match="order")
    reject(lambda d: d["links"].append(dict(d["links"][0])), match="duplicate")
    reject(lambda d: d["links"][0].update(extra=1), match="extra")
    reject(lambda d: d["links"][0].pop("payer"), match="payer")
    reject(lambda d: d["links"][0].update(slot=True), exc=TypeError)
    reject(lambda d: d["unpaired"][0].update(side="middle"), match="side")
    reject(lambda d: d["unpaired"][0].update(detail="payers=1 receivers=1"))
    # not_analyzed — лише точна константа: без зв'язків, з її detail
    reject(lambda d: d.update(complete=False, reason=NOT_ANALYZED), match="delegated")
    na = to_dict(_result(buyers))
    na["delegated"]["detail"] = "something else"
    with pytest.raises(ValueError, match="delegated"):
        from_dict(na)

