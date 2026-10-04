# verifies: FR-002-17, FR-002-19, FR-001-09
"""Підключення правила делегованої купівлі до збору 001 (T-046; research R-3, R-4).

Що доводять тести:
- на `scenarios/swapsend` `collect` дає рівно задекларовані `delegated.links`/`unpaired` (еталон
  складено генератором T-043 з декларації ролей, не цим кодом), `complete=true`;
- FR-002-17: склад, порядок і `rank` покупців — точно `expected.json`; отримувач B серед покупців
  відсутній (і в `purchases_by_wallet` теж); вимкнення правила не змінює результату без `delegated`;
- SC-006 / R-4 (в): для basic, трьох hub-конфігів, corrupt і swapsend `to_dict` без ключа `delegated`,
  `rpc_calls`, `transactions_scanned` і журнал викликів джерела побайтово дорівнюють знімку, знятому
  ДО змін T-046 (HEAD 991309b, `src/unmask/ingest` без змін), — повний збір через `collector.collect`;
- FR-002-19: повнота `delegated` дзеркалить `buyers` на пошкоджених даних, збої джерела й обрізаному
  бюджеті; суперечлива відповідь RPC (укорочений `meta.preBalances`/`postBalances`, `accountIndex`
  поза ключами) — `CorruptRecord("malformed")` у `parse`, тобто `corrupt_data` для покупців і
  delegated, а не виняток і не «complete без зв'язків»;
- resume після збою == свіжому прогону, включно з `delegated`; поле стану перевиводиться щоразу;
- `cache._FIELD_COPY` покриває `delegated_by_signature`, кеш ізолює контейнер, значення незмінні;
- ні колектор, ні сервіс не емітують `not_analyzed`; `service._unverified` виводить delegated з тієї ж
  причини збою, що й покупців.

Мережі немає: лише `FixtureRpcSource` (записані фікстури, мутовані копії — у `tmp_path`) і `FakeClock`.
"""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

import unmask.ingest.buyers as buyers_module
from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import _FIELD_COPY, ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    BuyersCompleteness,
    CompletenessStatus,
    DelegatedAnalysis,
    DelegatedLink,
    IngestResult,
    MissingReason,
    UnpairedCandidate,
)
from unmask.ingest.parse import CorruptRecord, ParsedTx, parse_transaction
from unmask.ingest.purchases import detect_purchases
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable
from unmask.ingest.serialize import to_dict
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SWAPSEND = SCENARIOS / "swapsend"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((SWAPSEND / "expected.json").read_text(encoding="utf-8"))
RPC = json.loads((SWAPSEND / "rpc.json").read_text(encoding="utf-8"))
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
DECLARED = EXPECTED["delegated"]
(LINK,) = DECLARED["links"]
LINK_SIG = LINK["signature"]
UNPAIRED_SIGS = sorted({c["signature"] for c in DECLARED["unpaired"]})
P1_SIG = EXPECTED["buyers"][0]["first_buy_signature"]

BASIC_EXPECTED = json.loads((SCENARIOS / "basic" / "expected.json").read_text(encoding="utf-8"))
HUB_EXPECTED = json.loads((SCENARIOS / "hub" / "expected.json").read_text(encoding="utf-8"))
CORRUPT_CAST = json.loads((SCENARIOS / "corrupt" / "rpc.json").read_text(encoding="utf-8"))["_meta"]["cast"]

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
RPC_ERRORS = [RpcUnavailable("node down"), RpcRateLimited(retry_after=1.0), RpcTimeout("slow")]


# --- знімки ДО змін T-046 ------------------------------------------------------------------------
# Зняті на HEAD 991309b (робоче дерево `src/unmask/ingest` чисте), `collector.collect` з `FakeClock()` і
# `FixtureRpcSource(dir, clock=clock)`, конфіг = поставлений `config/ingest.yaml` зі значеннями з
# `CASES`. Поля: sha256 канонічного JSON (`sort_keys`, `ensure_ascii=False`, `separators=(",", ":")`)
# від `to_dict(result)` без ключа `delegated`; `metadata.rpc_calls`; `metadata.transactions_scanned`;
# sha256 канонічного JSON журналу `source.calls` (список `[method, params]`).

PRE_CHANGE = {
    "basic": ("86366e753871621a78350033c135c4861877aab1c524906155d0d6e8881205bc", 50, 7,
              "25bbbfb03c2518c9d85388d71e428a8a547f1d62670f665bf3251411a03f3e83"),
    "hub_high_degree": ("bdb4b70defe1c57a377b5e0e6b2d4f8c00d5e6769e4a5e3ec38c77b57128ad9e", 69, 3,
                        "463f090d2d02822b512fde67dbe17059b51b969c8ed634f04bbdc83ca067cfd9"),
    "hub_signature_cap": ("abdbb38edc748982abd1a3769834e6193f00f587168183767bd066b80b715091", 51, 3,
                          "cfe00b9613bd83f7356770c3490c06af0e22457f0bb6541e822dc54d7e171ab8"),
    "hub_control": ("a60296cd61232543a1d5446e54792ee228c70e9f859a6c55db267fabf079849a", 87, 3,
                    "21aca53c3e8adf06b0ddad20b60c3b24162e3e1f8abde4da0802da4b39695f9d"),
    "corrupt": ("460aeb1731b91ba3e686afdc01e312dfc77b844319afecb2eecf344bd6606b07", 21, 4,
                "c035ab4c9409c24c5969624692094f4870d53a5f2c922c9abc0f8c91cd04a337"),
    "swapsend": ("7ded3d10ac15c4f433ab570af32cc6d229165385da955b76e9a1f538625be8dd", 15, 8,
                 "5405758cd301623a5f7a1cbd52c1abf2d273063a8bf3f1408e53e673fbf6461a"),
}


def _cases() -> dict[str, tuple[str, str, dict]]:
    out = {"basic": ("basic", BASIC_EXPECTED["mint"], BASIC_EXPECTED["config"])}
    for case in HUB_EXPECTED["cases"]:
        out[case["name"]] = ("hub", HUB_EXPECTED["mint"], {**BASIC_EXPECTED["config"], **case["config"]})
    out["corrupt"] = ("corrupt", CORRUPT_CAST["M"],
                      {**BASIC_EXPECTED["config"], "first_buyers_n": 3, "funding_depth": 1})
    out["swapsend"] = ("swapsend", M, EXPECTED["config"])
    return out


CASES = _cases()


# --- помічники ------------------------------------------------------------------------------------


def _cfg(values: dict | None = None, **overrides):
    cfg = dataclasses.replace(load_config(SHIPPED), **dict(values if values is not None else EXPECTED["config"]))
    return dataclasses.replace(cfg, **overrides)


def _canonical_sha256(doc) -> str:
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _collect(directory: Path, mint: str = M, cfg=None, *, failures=None, clock=None):
    cfg = cfg or _cfg()
    clock = clock or FakeClock()
    source = FixtureRpcSource(directory, failures=failures, clock=clock)
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, source, collect(state, source, cfg, clock)


def _variant(tmp_path: Path, mutate, name: str = "swapsend") -> Path:
    data = copy.deepcopy(RPC)
    mutate(data)
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / "rpc.json").write_text(json.dumps(data), encoding="utf-8")
    return directory


def _stable(result: IngestResult) -> dict:
    doc = to_dict(result)
    for key in VOLATILE:
        doc["metadata"].pop(key)
    return doc


def _assert_mirrors(result: IngestResult) -> None:
    b, d = result.completeness.buyers, result.delegated
    assert d.analyzed, d
    assert (d.complete, d.reason, d.detail) == (b.complete, b.reason, b.detail)


def _links(result: IngestResult) -> list[dict]:
    return to_dict(result)["delegated"]["links"]


def _unpaired(result: IngestResult) -> list[dict]:
    return to_dict(result)["delegated"]["unpaired"]


# --- swapsend: зв'язки й кандидати ----------------------------------------------------------------


def test_swapsend_collect_yields_exactly_one_link_and_declared_unpaired_with_signature_and_slot():
    state, _source, result = _collect(SWAPSEND)
    delegated = to_dict(result)["delegated"]
    assert delegated["links"] == DECLARED["links"]
    assert delegated["unpaired"] == DECLARED["unpaired"]
    assert (delegated["complete"], delegated["reason"], delegated["detail"]) == (True, None, "")
    assert result.completeness.status is CompletenessStatus.COMPLETE
    (link,) = result.delegated.links
    assert (link.signature, link.slot, link.payer, link.receiver) == (LINK_SIG, LINK["slot"], W["A"], W["B"])
    # стан: по запису на кожну транзакцію з результатом правила; значення — кортежі frozen-об'єктів
    assert set(state.delegated_by_signature) == {LINK_SIG, *UNPAIRED_SIGS}
    assert state.delegated_by_signature[LINK_SIG] == (link,)
    for sig, items in state.delegated_by_signature.items():
        assert isinstance(items, tuple) and items
        assert all(isinstance(i, (DelegatedLink, UnpairedCandidate)) and i.signature == sig for i in items)


def test_buyers_composition_order_and_ranks_equal_expected_and_exclude_receiver(monkeypatch):
    state, _source, result = _collect(SWAPSEND)
    assert to_dict(result)["buyers"] == EXPECTED["buyers"]
    assert [(b.wallet, b.rank) for b in result.buyers] == [(b["wallet"], b["rank"]) for b in EXPECTED["buyers"]]
    receivers = {W["B"]} | {c["wallet"] for c in DECLARED["unpaired"] if c["side"] == "receiver"}
    payers = {W["A"]} | {c["wallet"] for c in DECLARED["unpaired"] if c["side"] == "payer"}
    assert not (receivers | payers) & {b.wallet for b in result.buyers}
    assert not (receivers | payers) & set(state.purchases_by_wallet)  # отримувач не займає місця в N
    assert set(state.purchases_by_wallet) == {b["wallet"] for b in EXPECTED["buyers"]}

    # Правило не впливає на покупців: прогін із вимкненим правилом дає той самий результат без delegated
    # (і для ширшого N, де вікно сягає всієї історії mint).
    for cfg in (_cfg(), _cfg(first_buyers_n=50)):
        _s, _src, with_rule = _collect(SWAPSEND, cfg=cfg)
        with monkeypatch.context() as patch:
            patch.setattr(buyers_module, "detect_delegated", lambda parsed, mint: [])
            _s2, _src2, without = _collect(SWAPSEND, cfg=cfg)
        a, b = to_dict(with_rule), to_dict(without)
        assert a.pop("delegated")["links"] and not b.pop("delegated")["links"]
        assert a == b


@pytest.mark.parametrize("name", sorted(PRE_CHANGE))
def test_basic_hub_corrupt_results_identical_to_pre_change_except_delegated_key(name):
    directory, mint, values = CASES[name]
    _state, source, result = _collect(SCENARIOS / directory, mint, _cfg(values))
    doc = to_dict(result)
    assert "delegated" in doc
    doc.pop("delegated")
    got = (_canonical_sha256(doc), result.metadata.rpc_calls, result.metadata.transactions_scanned,
           _canonical_sha256([list(call) for call in source.calls]))
    assert got == PRE_CHANGE[name]
    _assert_mirrors(result)


# --- FR-002-19: повнота delegated == повнота покупців ----------------------------------------------


def test_delegated_complete_mirrors_buyers_complete_on_corrupt_and_budget_cut(tmp_path):
    # corrupt: пошкодження лише в історіях гаманців (missing), перелічення покупців повне → delegated
    # повний: його повнота — від buyers, а не від загального статусу (R-3 п. 5)
    directory, mint, values = CASES["corrupt"]
    _s, _src, corrupt = _collect(SCENARIOS / directory, mint, _cfg(values))
    assert corrupt.completeness.status is CompletenessStatus.INCOMPLETE and corrupt.completeness.missing
    assert corrupt.completeness.buyers.complete is True
    _assert_mirrors(corrupt)
    # пошкоджена транзакція mint (`meta` відсутня) → обидва неповні з тією самою причиною й detail
    def no_meta(data):
        data["getTransaction"][UNPAIRED_SIGS[0]]["meta"] = None
    data = copy.deepcopy(RPC)
    no_meta(data)
    directory = tmp_path / "swapsend"
    directory.mkdir()
    (directory / "rpc.json").write_text(json.dumps(data), encoding="utf-8")
    _s, _src, broken = _collect(directory)
    assert broken.completeness.buyers.reason is MissingReason.CORRUPT_DATA
    assert UNPAIRED_SIGS[0] in broken.delegated.detail
    _assert_mirrors(broken)
    assert UNPAIRED_SIGS[0] not in {c.signature for c in broken.delegated.unpaired}

    # swapsend: обрізаний бюджет і збої джерела на кожному кроці перелічення
    seen_incomplete = set()
    for budget in (0.5, 1.5, 2.5, 3.5, 4.5, 30.0):
        cfg = _cfg(time_budget_seconds=budget)
        _s, _src, result = _collect(SWAPSEND, cfg=cfg, clock=FakeClock(advance_per_call=1.0))
        _assert_mirrors(result)
        if not result.completeness.buyers.complete:
            seen_incomplete.add(result.completeness.buyers.reason)
            assert result.delegated.complete is False and result.delegated.reason != "not_analyzed"
    assert MissingReason.BUDGET_EXHAUSTED in seen_incomplete
    for k in range(1, 8):
        for exc in RPC_ERRORS:
            _s, _src, result = _collect(SWAPSEND, failures=[FailAfter(k, exc)])
            _assert_mirrors(result)
            if result.completeness.buyers.complete is False:
                assert result.delegated.complete is False


@pytest.mark.parametrize("defect", ["pre_balances_short", "post_balances_short", "pre_token_index_999",
                                    "post_token_index_999", "pre_token_index_n", "post_token_index_n",
                                    "post_token_index_negative"])
def test_inconsistent_link_transaction_is_corrupt_data_for_buyers_and_delegated_not_exception(tmp_path, defect):
    """ВИМОГА ревʼю T-045: суперечлива відповідь RPC не валить collect і не дає «complete без зв'язків»."""
    def mutate(data):
        meta = data["getTransaction"][LINK_SIG]["meta"]
        if defect == "pre_balances_short":
            meta["preBalances"] = meta["preBalances"][:-1]
        elif defect == "post_balances_short":
            meta["postBalances"] = meta["postBalances"][:-1]
        elif defect == "pre_token_index_999":
            meta["preTokenBalances"][0]["accountIndex"] = 999
        elif defect == "post_token_index_999":
            meta["postTokenBalances"][0]["accountIndex"] = 999
        elif defect in ("pre_token_index_n", "post_token_index_n"):
            # межа: індекс == len(accountKeys) — на один за останнім ключем (у jsonParsed max = n-1)
            n = len(data["getTransaction"][LINK_SIG]["transaction"]["message"]["accountKeys"])
            meta["preTokenBalances" if defect.startswith("pre") else "postTokenBalances"][0]["accountIndex"] = n
        else:
            meta["postTokenBalances"][0]["accountIndex"] = -1

    directory = _variant(tmp_path, mutate)
    raw = json.loads((directory / "rpc.json").read_text())["getTransaction"][LINK_SIG]
    if defect.startswith("pre_token"):
        assert raw["meta"]["preTokenBalances"], "link tx must have pre token balances for this defect"
    parsed = parse_transaction(raw)
    assert isinstance(parsed, CorruptRecord) and parsed.reason == "malformed" and parsed.partial is None
    assert parsed.signature == LINK_SIG

    state, _source, result = _collect(directory)  # не виняток
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, MissingReason.CORRUPT_DATA)
    assert LINK_SIG in b.detail and "malformed" in b.detail
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    _assert_mirrors(result)
    assert result.delegated.complete is False and result.delegated.reason is MissingReason.CORRUPT_DATA
    assert _links(result) == []  # зв'язок не вигадано з обрізаних масивів
    assert _unpaired(result) == DECLARED["unpaired"]  # решта транзакцій розібрана
    assert LINK_SIG not in state.tx_cache and LINK_SIG not in state.delegated_by_signature
    assert to_dict(result)["buyers"] == EXPECTED["buyers"]

    # сервіс: те саме, без винятку; неповний результат не кешується як остаточний
    clock = FakeClock()
    service = IngestService(_cfg(), FixtureRpcSource(directory, clock=clock), clock=clock)
    outcome = service.collect(M)
    assert isinstance(outcome, IngestResult)
    assert outcome.completeness.status is CompletenessStatus.INCOMPLETE
    _assert_mirrors(outcome)


def _move_key_to_end(tx: dict, index: int, *, front: bool = False) -> None:
    """Семантично та сама транзакція з ключем `index` на останній позиції: переставлено `accountKeys`,
    `pre/postBalances` і перенумеровано `accountIndex` усіх токен-балансів (інструкції посилаються на адреси).
    `front=True` — на позицію 0 (тоді змінюється fee payer: лише для перевірки діапазону індексу)."""
    keys = tx["transaction"]["message"]["accountKeys"]
    rest = [i for i in range(len(keys)) if i != index]
    order = [index] + rest if front else rest + [index]
    new_pos = {old: new for new, old in enumerate(order)}
    tx["transaction"]["message"]["accountKeys"] = [keys[i] for i in order]
    meta = tx["meta"]
    for name in ("preBalances", "postBalances"):
        meta[name] = [meta[name][i] for i in order]
    for name in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(name) or ():
            b["accountIndex"] = new_pos[b["accountIndex"]]


@pytest.mark.parametrize("side", ["preTokenBalances", "postTokenBalances"])
def test_token_balance_at_last_key_index_is_valid_and_result_unchanged(tmp_path, side):
    """Позитивний контроль меж: `accountIndex == n-1` і `== 0` — легітимні, жодного хибного `malformed`."""
    tx = RPC["getTransaction"][LINK_SIG]
    assert tx["meta"][side], side
    index = tx["meta"][side][0]["accountIndex"]
    assert index != 0  # fee payer лишається на місці

    directory = _variant(tmp_path, lambda data: _move_key_to_end(data["getTransaction"][LINK_SIG], index))
    raw = json.loads((directory / "rpc.json").read_text())["getTransaction"][LINK_SIG]
    n = len(raw["transaction"]["message"]["accountKeys"])
    assert raw["meta"][side][0]["accountIndex"] == n - 1
    parsed = parse_transaction(raw)
    assert isinstance(parsed, ParsedTx), parsed
    original = parse_transaction(tx)
    assert parsed.programs == original.programs and parsed.transfers == original.transfers
    assert {k: parsed.sol_delta(k) for k in parsed.account_keys} == {k: original.sol_delta(k) for k in original.account_keys}

    # нижня межа: accountIndex == 0 теж у діапазоні (не malformed); fee payer тут інший, тож лише розбір
    front = copy.deepcopy(tx)
    _move_key_to_end(front, index, front=True)
    assert front["meta"][side][0]["accountIndex"] == 0
    parsed_front = parse_transaction(front)
    assert isinstance(parsed_front, ParsedTx), parsed_front
    assert parsed_front.transfers == original.transfers

    _state, _source, result = _collect(directory)
    _s, _src, fresh = _collect(SWAPSEND)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(result) == _stable(fresh)
    assert _links(result) == DECLARED["links"]


@pytest.mark.parametrize(("defect", "reason", "field"), [
    ("fee_float_and_pre_short", "non_integer", "fee"),
    ("pre_float_and_pre_short", "non_integer", "preBalances"),
    ("post_float_and_post_short", "non_integer", "postBalances"),
])
def test_corrupt_reason_precedence_with_several_defects_matches_head(defect, reason, field):
    """Старшинство причин як на HEAD до T-046: неціле поле (fee, баланси) перемагає розбіжність довжин."""
    raw = copy.deepcopy(RPC["getTransaction"][LINK_SIG])
    meta = raw["meta"]
    if defect == "fee_float_and_pre_short":
        meta["fee"] = 5000.0
        meta["preBalances"] = meta["preBalances"][:-1]
    elif defect == "pre_float_and_pre_short":
        meta["preBalances"] = meta["preBalances"][:-1]
        meta["preBalances"][0] = 1.5
    else:
        meta["postBalances"] = meta["postBalances"][:-1]
        meta["postBalances"][0] = 1.5
    parsed = parse_transaction(raw)
    assert isinstance(parsed, CorruptRecord)
    assert (parsed.reason, parsed.detail.split(":")[0]) == (reason, field)


def test_partial_link_transaction_keeps_link_but_marks_both_incomplete(tmp_path):
    # CorruptRecord.partial (нерозпізнаний переказ при цілих балансах): правило працює на балансах, як і
    # правило купівлі (R-2) — зв'язок не губиться, але неповнота позначена (corrupt_data) для обох
    def mutate(data):
        message = data["getTransaction"][LINK_SIG]["transaction"]["message"]
        message["instructions"].append({
            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "parsed": {"type": "transferCheckedWithFee", "info": {"source": W["POOL_ATA"]}},
        })

    directory = _variant(tmp_path, mutate)
    raw = json.loads((directory / "rpc.json").read_text())["getTransaction"][LINK_SIG]
    parsed = parse_transaction(raw)
    assert isinstance(parsed, CorruptRecord) and parsed.partial is not None
    state, _source, result = _collect(directory)
    assert (result.completeness.buyers.complete, result.completeness.buyers.reason) == (
        False, MissingReason.CORRUPT_DATA)
    _assert_mirrors(result)
    assert _links(result) == DECLARED["links"] and _unpaired(result) == DECLARED["unpaired"]
    assert to_dict(result)["buyers"] == EXPECTED["buyers"]
    assert LINK_SIG not in state.tx_cache  # partial не кешується: resume перерахує його заново


def test_inconsistent_purchase_transaction_is_corrupt_not_truncated_purchase(tmp_path):
    # той самий захист для правила купівлі: zip більше не обрізає масиви мовчки
    def mutate(data):
        meta = data["getTransaction"][P1_SIG]["meta"]
        meta["preBalances"] = meta["preBalances"][:-1]

    directory = _variant(tmp_path, mutate)
    _state, _source, result = _collect(directory)
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, MissingReason.CORRUPT_DATA) and P1_SIG in b.detail
    assert W["P1"] not in {buyer.wallet for buyer in result.buyers}
    _assert_mirrors(result)
    assert _links(result) == DECLARED["links"]


def test_parse_consistent_transactions_unchanged_and_purchases_see_full_arrays():
    # на кожній транзакції фікстури масиви узгоджені, тож parse дає ParsedTx, як і до змін
    for sig, raw in RPC["getTransaction"].items():
        if raw is None:
            continue
        parsed = parse_transaction(raw)
        assert isinstance(parsed, ParsedTx), (sig, parsed)
        assert len(parsed.pre_balances) == len(parsed.post_balances) == len(parsed.account_keys)
        n = len(parsed.account_keys)
        assert all(0 <= tb.account_index < n for tb in parsed.pre_token_balances + parsed.post_token_balances)
    assert [p.wallet for p in detect_purchases(parse_transaction(RPC["getTransaction"][P1_SIG]), M)] == [W["P1"]]


# --- R-4: resume == fresh, поле перевиводиться ---------------------------------------------------


INTERRUPTIONS = [
    pytest.param({"failures": [FailAfter(k, exc)]}, id=f"after-{k}-{type(exc).__name__}")
    for k in (1, 2, 3, 5, 8) for exc in RPC_ERRORS
] + [
    pytest.param({"failures": [FailFor(sig, RpcUnavailable("down"), times=1)]}, id=f"tx-{sig[:6]}")
    for sig in [LINK_SIG, *UNPAIRED_SIGS]
] + [
    pytest.param({"budget": b}, id=f"budget-{b}") for b in (1.5, 2.5, 3.5, 5.5)
]


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_resume_after_failure_equals_fresh_including_delegated(interruption):
    cfg = _cfg()
    failures = interruption.get("failures")
    budget = interruption.get("budget")
    clock = FakeClock(advance_per_call=1.0 if budget is not None else 0.0)
    first_cfg = dataclasses.replace(cfg, time_budget_seconds=budget) if budget is not None else cfg
    state, _src, first = _collect(SWAPSEND, cfg=first_cfg, failures=failures, clock=clock)
    assert first.completeness.status is CompletenessStatus.INCOMPLETE
    _assert_mirrors(first)

    # через кеш, як у сервісі: структурна копія стану
    cache = ResultCache()
    cache.put_partial(state)
    partial = cache.get_partial(M)
    assert partial.delegated_by_signature == state.delegated_by_signature
    again = resume(partial, FixtureRpcSource(SWAPSEND), cfg, FakeClock())
    fresh_state, _fsrc, fresh = _collect(SWAPSEND, cfg=cfg)
    assert again.completeness.status is CompletenessStatus.COMPLETE
    assert again.metadata.resumed is True
    assert _stable(again) == _stable(fresh)
    assert again.delegated == fresh.delegated
    assert to_dict(again)["delegated"]["links"] == DECLARED["links"]
    assert to_dict(again)["delegated"]["unpaired"] == DECLARED["unpaired"]
    assert again.metadata.transactions_scanned == fresh.metadata.transactions_scanned
    assert partial.delegated_by_signature == fresh_state.delegated_by_signature


def test_delegated_state_is_rederived_on_every_enumeration():
    cfg = _cfg()
    state, _src, fresh = _collect(SWAPSEND, cfg=cfg)
    ghost = DelegatedLink("Ghost" + "1" * 83, 1, None, W["P2"], W["P3"])
    state.delegated_by_signature[ghost.signature] = (ghost,)
    del state.delegated_by_signature[LINK_SIG]
    source = FixtureRpcSource(SWAPSEND)
    again = collect(state, source, cfg, FakeClock())
    assert source.calls == []  # повний стан: перевивід лише з кешу транзакцій
    assert again.delegated == fresh.delegated
    assert ghost.signature not in state.delegated_by_signature and LINK_SIG in state.delegated_by_signature


# --- кеш ---------------------------------------------------------------------------------------


def test_field_copy_table_covers_delegated_and_cache_isolates_it():
    assert _FIELD_COPY["delegated_by_signature"] is dict
    assert set(_FIELD_COPY) == {f.name for f in dataclasses.fields(CollectionState)}
    assert CollectionState(mint=M, config_version=1).delegated_by_signature == {}

    state, _src, _r = _collect(SWAPSEND)
    assert state.delegated_by_signature
    for sig, items in state.delegated_by_signature.items():  # інваріант структурної копії
        assert isinstance(sig, str) and isinstance(items, tuple)
        for item in items:
            assert type(item).__dataclass_params__.frozen
            hash(item)
    snapshot = copy.deepcopy(state)
    cache = ResultCache()
    cache.put_partial(state)
    assert cache.get_partial(M).delegated_by_signature is not state.delegated_by_signature
    state.delegated_by_signature.clear()
    assert cache.get_partial(M) == snapshot
    got = cache.get_partial(M)
    got.delegated_by_signature.clear()
    assert cache.get_partial(M) == snapshot


# --- not_analyzed і сервіс ----------------------------------------------------------------------


def _service(directory: Path, mint: str, cfg, **source_kwargs) -> IngestResult:
    clock = source_kwargs.pop("clock", None) or FakeClock()
    outcome = IngestService(cfg, FixtureRpcSource(directory, clock=clock, **source_kwargs), clock=clock).collect(mint)
    assert isinstance(outcome, IngestResult), outcome
    return outcome


def test_collector_and_service_never_emit_not_analyzed():
    results = []
    for name, (directory, mint, values) in CASES.items():
        results.append(_collect(SCENARIOS / directory, mint, _cfg(values))[2])
        results.append(_service(SCENARIOS / directory, mint, _cfg(values)))
    for exc in RPC_ERRORS:
        for k in (1, 2, 4):
            results.append(_collect(SWAPSEND, failures=[FailAfter(k, exc)])[2])
            results.append(_service(SWAPSEND, M, _cfg(), failures=[FailAfter(k, exc)]))
    results.append(_service(SWAPSEND, M, _cfg(time_budget_seconds=0.5), clock=FakeClock(advance_per_call=1.0)))
    for result in results:
        assert result.delegated.analyzed and result.delegated.reason != "not_analyzed"
        assert to_dict(result)["delegated"]["reason"] != "not_analyzed"
        _assert_mirrors(result)


@pytest.mark.parametrize("failure", [*RPC_ERRORS, "budget"], ids=lambda f: f if isinstance(f, str) else type(f).__name__)
def test_unverified_result_derives_delegated_from_same_failure(failure):
    if failure == "budget":  # запит з'їв увесь бюджет, адаптер обірвав його таймаутом → budget_exhausted
        result = _service(SWAPSEND, M, _cfg(time_budget_seconds=5.0), clock=FakeClock(advance_per_call=5.0),
                          failures=[FailFor(M, RpcTimeout("budget"), times=1)])
        assert result.completeness.buyers.reason is MissingReason.BUDGET_EXHAUSTED
    else:
        result = _service(SWAPSEND, M, _cfg(), failures=[FailFor(M, failure, times=1)])
    assert result.metadata.transactions_scanned == 0 and result.buyers == ()
    b = result.completeness.buyers
    assert b.complete is False
    assert result.delegated == DelegatedAnalysis.derive((), (), b)
    assert (result.delegated.links, result.delegated.unpaired) == ((), ())
    _assert_mirrors(result)
    assert isinstance(b, BuyersCompleteness)
