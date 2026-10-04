# verifies: FR-001-01, FR-001-02
"""Перші N покупців із курсором (research R-6, R-7; T-011).

`enumerate_buyers(source, mint, state, config, deadline) -> BuyersCompleteness`:
перегортання історії mint від найновішого через `before`, далі транзакції від найстаріших,
правило купівлі R-2, перше входження гаманця, добивання слота N-го покупця, ключ
`(first_buy_slot, first_buy_signature, wallet)`, рівно N з рангами.

Еталон — `scenarios/basic` (незалежний генератор `build_fixtures.py`). Межові випадки
(повторна купівля, err-транзакції, пошкоджені й відсутні транзакції, переставлені відповіді)
— варіанти basic, похідні детерміновано тут же в `tmp_path`; мережі немає.
"""

import copy
import dataclasses
import json
import math
from pathlib import Path

import pytest
import yaml

from unmask.ingest.buyers import enumerate_buyers, select_first_n
from unmask.ingest.collector import CollectionState
from unmask.ingest.config import ConfigError, load_config
from unmask.ingest.model import AddressType, Asset, BuyersCompleteness, MissingReason, Spend
from unmask.ingest.purchases import Purchase
from unmask.ingest.rpc.fixture import FailAfter, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
BASIC = ROOT / "tests" / "fixtures" / "scenarios" / "basic"
SHIPPED = ROOT / "config" / "ingest.yaml"
RPC = json.loads((BASIC / "rpc.json").read_text())
EXPECTED = json.loads((BASIC / "expected.json").read_text())
W = EXPECTED["wallets"]
M = EXPECTED["mint"]
HISTORY = RPC["getSignaturesForAddress"][M]           # від найновішого до найстарішого
BUY_SIG = {b["wallet"]: b["first_buy_signature"] for b in EXPECTED["buyers"]}
P1, P2, P3, P4, P5 = (W[k] for k in ("P1", "P2", "P3", "P4", "P5"))


class _StubDeadline:
    """Структурний стаб дедлайну (справжній `budget.Deadline` — T-015)."""

    def expired(self) -> bool:
        return False

    def remaining(self) -> float:
        return math.inf


DEADLINE = _StubDeadline()


def _cfg(n: int, page_size: int = 1000, tx_batch_size: int = 1000):
    # T-052: сторінка підписів (page_size) і пачка транзакцій (tx_batch_size) — окремі параметри; умовчання
    # 1000/1000 відтворює поведінку до T-052 (тоді page_size задавав обидва)
    cfg = load_config(SHIPPED)
    return dataclasses.replace(cfg, first_buyers_n=n, rpc=dataclasses.replace(
        cfg.rpc, page_size=page_size, tx_batch_size=tx_batch_size))


def _run(source, n: int, page_size: int = 1000, tx_batch_size: int = 1000):
    cfg = _cfg(n, page_size, tx_batch_size)
    state = CollectionState(mint=M, config_version=cfg.version)
    completeness = enumerate_buyers(source, M, state, cfg, DEADLINE)
    return state, completeness


def _basic(**kwargs) -> FixtureRpcSource:
    return FixtureRpcSource(BASIC, **kwargs)


def _variant(tmp_path: Path, mutate, **kwargs) -> FixtureRpcSource:
    """Варіант basic: копія rpc.json, змінена `mutate(data)`, у `tmp_path/basic_variant`."""
    data = copy.deepcopy(RPC)
    mutate(data)
    directory = tmp_path / "basic_variant"
    directory.mkdir(exist_ok=True)
    (directory / "rpc.json").write_text(json.dumps(data))
    return FixtureRpcSource(directory, **kwargs)


def _clone_buy(data, *, of: str, sig: str, slot: int, sig_err=None, meta_err=None, newest_in_slot=False) -> None:
    """Додати в історію mint копію купівельної транзакції `of` з новим підписом і слотом.

    Запис історії стає найстарішим у своєму слоті (або найновішим, якщо `newest_in_slot`).
    """
    raw = copy.deepcopy(data["getTransaction"][BUY_SIG[of]])
    raw["transaction"]["signatures"][0] = sig
    raw["slot"] = slot
    raw["blockTime"] = 1759400000 + slot
    if meta_err is not None:
        raw["meta"]["err"] = meta_err
    data["getTransaction"][sig] = raw
    history = data["getSignaturesForAddress"][M]
    entry = {"signature": sig, "slot": slot, "err": sig_err, "memo": None,
             "blockTime": 1759400000 + slot, "confirmationStatus": "finalized"}
    if newest_in_slot:
        history.insert(next(k for k, e in enumerate(history) if e["slot"] <= slot), entry)
    else:
        history.append(entry)
        history.sort(key=lambda e: -e["slot"])


def _as_dict(buyer) -> dict:
    d = dataclasses.asdict(buyer)
    d["spent"] = [{"asset": str(s["asset"]), "amount": s["amount"]} for s in d["spent"]]
    d["programs"] = list(d["programs"])
    d["address_type"] = buyer.address_type.value
    return d


def _wallets(state) -> list[str]:
    return [b.wallet for b in state.buyers]


def _fetched(source) -> list[str]:
    return [sig for method, params in source.calls if method == "getTransaction" for sig in params["signatures"]]


COMPLETE = BuyersCompleteness(complete=True, reason=None, detail="")


# --- Щасливий шлях і межі N ----------------------------------------------------------


def test_exactly_n_buyers_in_first_buy_order_no_later_buyer_included():
    source = _basic()
    state, completeness = _run(source, n=3)

    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"][:3]
    assert _wallets(state) == [P1, P3, P2]
    assert [b.rank for b in state.buyers] == [1, 2, 3]
    assert P4 not in _wallets(state) and P5 not in _wallets(state)
    assert completeness == COMPLETE
    # пізніших покупців правилом не розбирали: слот 210 добитий (10, 12, 200, 210, 210), 220 і 230 — ні
    assert state.transactions_scanned == 5
    assert BUY_SIG[P4] not in state.tx_cache and BUY_SIG[P5] not in state.tx_cache
    assert state.rpc_calls == len(source.calls)


def test_n_equal_to_buyer_count_matches_golden_and_keeps_off_curve_pool_in_n():
    """N = кількість покупців: усі 5 збігаються з еталоном поле в поле.

    P5 — off-curve (пул/PDA) — рахується в N нарівні з іншими: відсікання хабів — окремий
    модуль (принцип VI), тут вершина лише позначена `address_type=off_curve`.
    """
    state, completeness = _run(_basic(), n=5)

    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"]
    assert state.buyers[-1].wallet == P5
    assert state.buyers[-1].rank == 5
    assert state.buyers[-1].address_type is AddressType.OFF_CURVE
    assert completeness == COMPLETE


def test_fewer_buyers_than_n_returns_all_and_marks_complete():
    source = _basic()
    state, completeness = _run(source, n=10)

    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"]
    assert completeness == COMPLETE
    assert state.mint_history_exhausted is True
    # усі 7 транзакцій історії mint розібрано правилом купівлі, кожну один раз
    assert state.transactions_scanned == len(HISTORY) == 7
    assert sorted(_fetched(source)) == sorted(e["signature"] for e in HISTORY)


def test_n_zero_is_rejected_by_config_and_by_enumerate(tmp_path):
    data = yaml.safe_load(SHIPPED.read_text())
    data["first_buyers_n"] = 0
    path = tmp_path / "ingest.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="first_buyers_n"):
        load_config(path)

    # IngestConfig можна зібрати й в обхід YAML — ядро не мовчить і нічого не запитує
    source = _basic()
    with pytest.raises(ValueError, match="first_buyers_n"):
        _run(source, n=0)
    assert source.calls == []


# --- Порядок і детермінізм -----------------------------------------------------------


def test_same_slot_tie_broken_by_signature_then_wallet_and_stable_across_runs(tmp_path):
    # basic: P2 і P3 в одному слоті 210; у історії P2 (vryd…) старіший, але підпис P3 (3dML…) менший
    assert BUY_SIG[P3] < BUY_SIG[P2]
    state, _ = _run(_basic(), n=5)
    assert _wallets(state)[1:3] == [P3, P2]

    # Інший порядок записів в одному слоті у відповіді джерела та інші розміри сторінок —
    # той самий результат (ключ складено лише з даних транзакцій).
    def swap_slot_210(data):
        history = data["getSignaturesForAddress"][M]
        i = next(k for k, e in enumerate(history) if e["slot"] == 210)
        history[i], history[i + 1] = history[i + 1], history[i]

    reference = [_as_dict(b) for b in state.buyers]
    for page_size in (1, 2, 3, 1000):
        for tx_batch_size in (1, 2, 3, 1000):
            for source in (_basic(), _variant(tmp_path, swap_slot_210)):
                again, completeness = _run(source, n=5, page_size=page_size, tx_batch_size=tx_batch_size)
                assert [_as_dict(b) for b in again.buyers] == reference
                assert completeness == COMPLETE

    # Однаковий слот і підпис (кілька покупців в одній транзакції) — далі за wallet,
    # незалежно від порядку входу.
    def purchase(wallet, slot, sig):
        return Purchase(wallet=wallet, signature=sig, slot=slot, block_time=None, received_amount=1,
                        spent=(Spend(Asset.SOL, 1),), programs=(), address_type=AddressType.WALLET)

    items = [purchase(P4, 7, "sigB"), purchase(P2, 7, "sigA"), purchase(P1, 7, "sigB"), purchase(P3, 6, "sigZ")]
    expected = [P3, P2] + sorted([P1, P4])           # sigB: далі за рядком wallet
    for ordering in (items, list(reversed(items)), items[2:] + items[:2]):
        selected = select_first_n(ordering, 4)
        assert [b.wallet for b in selected] == expected
        assert [b.rank for b in selected] == [1, 2, 3, 4]
    assert [b.wallet for b in select_first_n(items, 2)] == [P3, P2]


def test_nth_buyer_boundary_inside_slot_processes_whole_slot_before_cut():
    # N=2: другий покупець (P3) знайдено в слоті 210, де є ще купівля P2 — слот добивається
    # до кінця, наступний слот (220) уже не розбирається.
    source = _basic()
    state, completeness = _run(source, n=2, page_size=1, tx_batch_size=1)

    assert _wallets(state) == [P1, P3]
    assert completeness == COMPLETE
    fetched = _fetched(source)
    assert BUY_SIG[P2] in fetched                       # решта слота 210 розібрана
    assert BUY_SIG[P4] not in fetched and BUY_SIG[P5] not in fetched
    assert state.transactions_scanned == 5              # слоти 10, 12, 200, 210, 210

    # N досягнуто на єдиній купівлі слота: наступний слот не чіпається взагалі
    source = _basic()
    state, _ = _run(source, n=1, page_size=1, tx_batch_size=1)
    assert _wallets(state) == [P1]
    assert state.transactions_scanned == 3
    assert not {BUY_SIG[P2], BUY_SIG[P3]} & set(_fetched(source))


def test_repeat_purchase_by_same_wallet_counts_once_at_first(tmp_path):
    # P1 купує вдруге в слоті 215 (між P2/P3 і P4): повтор не займає місця в N
    source = _variant(tmp_path, lambda d: _clone_buy(d, of=P1, sig="RepeatBuyP1slot215", slot=215))
    state, completeness = _run(source, n=4)

    assert _wallets(state) == [P1, P3, P2, P4]
    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"][:4]   # перша купівля P1, не повтор
    assert completeness == COMPLETE


def test_repeat_purchase_in_same_slot_resolved_by_order_key_not_response_order(tmp_path):
    # Друга купівля P1 у тому ж слоті 200 з меншим підписом, але новіша в історії джерела:
    # «перша» — за ключем R-6 (slot, signature), а не за позицією у відповіді джерела.
    sig = "1111RepeatBuyP1slot200"
    assert sig < BUY_SIG[P1]
    source = _variant(tmp_path, lambda d: _clone_buy(d, of=P1, sig=sig, slot=200, newest_in_slot=True))
    history = json.loads((tmp_path / "basic_variant" / "rpc.json").read_text())["getSignaturesForAddress"][M]
    assert [e["signature"] for e in history].index(sig) < [e["signature"] for e in history].index(BUY_SIG[P1])
    for page_size in (1, 1000):
        state, _ = _run(source, n=5, page_size=page_size, tx_batch_size=page_size)
        assert _wallets(state) == [P1, P3, P2, P4, P5]
        assert state.buyers[0].first_buy_signature == sig


def test_err_transactions_skipped_and_never_counted_as_purchase(tmp_path):
    def mutate(data):
        # err у списку підписів: транзакцію навіть не запитуємо
        _clone_buy(data, of=P4, sig="FailedBuyP4slot150", slot=150, sig_err={"InstructionError": [2, "Custom"]})
        # err лише в meta транзакції: розібрана, але купівлею не є
        _clone_buy(data, of=P5, sig="FailedBuyP5slot160", slot=160, meta_err={"InstructionError": [0, "Custom"]})

    source = _variant(tmp_path, mutate)
    state, completeness = _run(source, n=5)

    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"]   # P4 і P5 — на своїх справжніх купівлях
    assert completeness == COMPLETE
    assert "FailedBuyP4slot150" not in _fetched(source)
    assert state.transactions_scanned == 8                              # 7 basic + meta-err, без sig-err


def test_pagination_cursor_advances_through_multiple_pages():
    source = _basic()
    state, completeness = _run(source, n=5, page_size=2)

    sigs = [e["signature"] for e in HISTORY]
    pages = [params for method, params in source.calls if method == "getSignaturesForAddress"]
    assert [p["before"] for p in pages] == [None, sigs[1], sigs[3], sigs[5], sigs[6]]
    assert all(p["address"] == M and p["limit"] == 2 and p["until"] is None for p in pages)
    assert state.signature_cursor == sigs[-1]
    assert state.mint_history_exhausted is True
    assert state.mint_signatures == [(e["signature"], e["slot"], e["blockTime"], e["err"]) for e in HISTORY]
    assert [_as_dict(b) for b in state.buyers] == EXPECTED["buyers"]
    assert completeness == COMPLETE
    assert state.rpc_calls == len(source.calls)


# --- Неповнота не мовчки -------------------------------------------------------------


def test_corrupt_transaction_marks_buyers_incomplete_and_keeps_other_buyers(tmp_path):
    def mutate(data):
        data["getTransaction"][BUY_SIG[P4]]["meta"] = None

    state, completeness = _run(_variant(tmp_path, mutate), n=5)

    assert completeness.complete is False
    assert completeness.reason is MissingReason.CORRUPT_DATA
    assert BUY_SIG[P4] in completeness.detail
    assert _wallets(state) == [P1, P3, P2, P5]
    assert BUY_SIG[P4] not in state.tx_cache        # не кешується: повтор спробує знову


def test_null_transaction_marks_buyers_incomplete_as_unavailable(tmp_path):
    def mutate(data):
        data["getTransaction"][BUY_SIG[P1]] = None

    state, completeness = _run(_variant(tmp_path, mutate), n=3)

    assert completeness.complete is False
    assert completeness.reason is MissingReason.UNAVAILABLE
    assert BUY_SIG[P1] in completeness.detail
    assert _wallets(state) == [P3, P2, P4]


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (RpcRateLimited(retry_after=1.0), MissingReason.RATE_LIMITED),
        (RpcTimeout("slow"), MissingReason.TIMEOUT),
        (RpcUnavailable("503"), MissingReason.UNAVAILABLE),
    ],
)
def test_source_failure_mid_pagination_marks_buyers_incomplete_with_reason(exc, reason):
    source = _basic(failures=[FailAfter(2, exc)])     # друга сторінка падає
    state, completeness = _run(source, n=5, page_size=2)

    assert completeness.complete is False
    assert completeness.reason is reason
    sigs = [e["signature"] for e in HISTORY]
    assert sigs[1] in completeness.detail             # курсор, з якого продовжувати
    assert state.signature_cursor == sigs[1]
    assert state.mint_history_exhausted is False
    assert state.buyers == ()                         # найстаріших записів не бачили — перших N не вигадуємо
    assert not _fetched(source)


def test_source_failure_while_fetching_transactions_marks_buyers_incomplete():
    # page_size=1: 8 викликів перегортання (7 сторінок + порожня); tx_batch_size=1 (T-052; до нього — той
    # самий page_size): далі по одній транзакції від найстаріших: 10, 12, 200, 210, 210, 220 — шоста
    # (купівля P4) = 14-й виклик падає.
    source = _basic(failures=[FailAfter(14, RpcUnavailable("boom"))])
    state, completeness = _run(source, n=5, page_size=1, tx_batch_size=1)

    assert completeness.complete is False
    assert completeness.reason is MissingReason.UNAVAILABLE
    assert state.mint_history_exhausted is True
    assert _wallets(state) == [P1, P3, P2]           # лише розібране до збою, без вигаданих
    assert source.calls[-1] == ("getTransaction", {"signatures": [BUY_SIG[P4]]})
    assert state.rpc_calls == len(source.calls)
