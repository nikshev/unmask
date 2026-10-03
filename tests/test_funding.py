# verifies: FR-001-03, FR-001-04, FR-001-05, FR-001-07
"""BFS джерел фінансування по рівнях (research R-1, R-8, R-9; T-012).

`expand_level(source, state, depth, config, deadline)` збирає перекази глибини `depth`:
розгортає вершини рівня `depth-1` (0 — покупці) від їхньої межі (перша купівля або найпізніше
ребро, що привело у вершину), строго раніше за межу; відправники стають вершинами рівня `depth`,
лише якщо `depth+1 <= funding_depth`.

Еталон — `scenarios/basic/expected.json` (незалежний генератор `build_fixtures.py`, що не
імпортує `unmask`). Межові випадки — варіанти basic у `tmp_path`; мережі немає.
"""

import copy
import dataclasses
import json
import math
from pathlib import Path

import pytest

from unmask.ingest.buyers import enumerate_buyers
from unmask.ingest.collector import CollectionState
from unmask.ingest.config import load_config
from unmask.ingest.funding import expand_level
from unmask.ingest.model import MissingReason, Transfer, transfer_sort_key
from unmask.ingest.rpc.fixture import FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
BASIC = ROOT / "tests" / "fixtures" / "scenarios" / "basic"
SHIPPED = ROOT / "config" / "ingest.yaml"
RPC = json.loads((BASIC / "rpc.json").read_text())
EXPECTED = json.loads((BASIC / "expected.json").read_text())
W = EXPECTED["wallets"]
M = EXPECTED["mint"]
A, B, C, D, G, X, S = (W[k] for k in ("A", "B", "C", "D", "G", "X", "S"))
P1, P2, P3, P4, P5 = (W[k] for k in ("P1", "P2", "P3", "P4", "P5"))
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _sig(prefix: str) -> str:
    (sig,) = [s for s in RPC["getTransaction"] if s.startswith(prefix)]
    return sig


# Ребра й пастки basic (див. expected.json: transfer_notes / excluded).
SIG_C_B = _sig("4Au5MB")      # слот 100, глибина 3
SIG_A_D = _sig("nrCxgM")      # 104, глибина 3 (цикл)
SIG_D_A = _sig("2YTSM2")      # 108, глибина 2
SIG_B_A = _sig("3sWVjS")      # 110, глибина 2
SIG_A_P1 = _sig("47HaJr")     # 120, глибина 1 (CPI 0.0)
SIG_G_A = _sig("2avhiv")      # 122, глибина 2 — між двома ребрами A
SIG_A_P2 = _sig("RafQBM")     # 125, глибина 1, два перекази
SIG_S_P2_TA = _sig("wWN58u")  # 126, SPL лише в історії токен-рахунку
SIG_S_P2 = _sig("5UkNdV")     # 127, SPL в історії і гаманця, і токен-рахунку
SIG_K_C = _sig("5N5yn9")      # 90, глибина 4
SIG_E_B = _sig("5XWYqw")      # 115, після ребра B->A
SIG_F_A = _sig("3cxg6r")      # 128, після останнього ребра A
SIG_FAILED = _sig("4PDeYe")   # 130, err
SIG_SELF = _sig("64Z8bu")     # 150, самопереказ
SIG_X_P1 = _sig("4PEu5n")     # 205, після купівлі P1
BUY_SIGS = {b["first_buy_signature"] for b in EXPECTED["buyers"]}
EXCLUDED = {e["signature"]: e["reason"] for e in EXPECTED["excluded"]}


class _StubDeadline:
    """Структурний стаб дедлайну (справжній `budget.Deadline` — T-015)."""

    def expired(self) -> bool:
        return False

    def remaining(self) -> float:
        return math.inf


DEADLINE = _StubDeadline()


def _cfg(depth: int = 3, *, spl: bool = True, page_size: int = 1000, n: int = 5):
    cfg = load_config(SHIPPED)
    return dataclasses.replace(
        cfg, first_buyers_n=n, funding_depth=depth, collect_spl_inbound=spl,
        rpc=dataclasses.replace(cfg.rpc, page_size=page_size),
    )


def _buyers(source, cfg) -> CollectionState:
    state = CollectionState(mint=M, config_version=cfg.version)
    assert enumerate_buyers(source, M, state, cfg, DEADLINE).complete
    return state


def _bfs(source, cfg, state: CollectionState | None = None) -> CollectionState:
    state = state if state is not None else _buyers(source, cfg)
    for depth in range(1, cfg.funding_depth + 1):
        expand_level(source, state, depth, cfg, DEADLINE)
    return state


def _run(depth: int = 3, **kwargs):
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(depth, **{k: kwargs[k] for k in ("spl", "page_size") if k in kwargs})
    return source, _bfs(source, cfg)


def _as_dict(t) -> dict:
    d = dataclasses.asdict(t)
    d["asset"] = str(d["asset"])
    return d


def _rows(state: CollectionState) -> list[dict]:
    return [_as_dict(t) for t in sorted(state.transfers.values(), key=transfer_sort_key)]


def _keys(state: CollectionState) -> set[tuple[str, str]]:
    return set(state.transfers)


def _sigs(state: CollectionState) -> set[str]:
    return {sig for sig, _path in state.transfers}


def _expected_row(sig: str, path: str) -> dict:
    (row,) = [r for r in EXPECTED["transfers"] if (r["signature"], r["instruction_path"]) == (sig, path)]
    return row


def _variant(tmp_path: Path, mutate, **kwargs) -> FixtureRpcSource:
    data = copy.deepcopy(RPC)
    mutate(data)
    directory = tmp_path / "basic_variant"
    directory.mkdir(exist_ok=True)
    (directory / "rpc.json").write_text(json.dumps(data))
    return FixtureRpcSource(directory, **kwargs)


def _sig_calls(source, address: str) -> list[dict]:
    return [p for m, p in source.calls if m == "getSignaturesForAddress" and p["address"] == address]


def _fetched(source) -> list[str]:
    return [s for m, p in source.calls if m == "getTransaction" for s in p["signatures"]]


# --- Golden: увесь basic проти незалежного еталона --------------------------------


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_golden_basic_transfers_match_expected_for_each_depth(depth):
    _source, state = _run(depth)
    expected = [r for r in EXPECTED["transfers"] if r["depth"] <= depth]
    assert expected, "еталон має містити перекази цієї глибини"
    assert _rows(state) == expected
    assert state.missing == {}
    # жоден виключений еталоном підпис (пост-купівля, причинне відсікання, err, self,
    # глибина 4, доставка в купівельній транзакції) не потрапив у перекази
    assert not (_sigs(state) & set(EXCLUDED)), {s: EXCLUDED[s] for s in _sigs(state) & set(EXCLUDED)}


def test_golden_depth_3_has_every_excluded_reason_covered():
    # Еталон справді перевіряє всі пастки, а не порожню множину.
    assert set(EXCLUDED.values()) == {
        "beyond_funding_depth", "causal_cutoff", "failed_transaction", "self_transfer",
        "after_first_buy", "in_first_buy_transaction",
    }


# --- Тести з tasks.md (T-012) ------------------------------------------------------


def test_depth1_and_depth2_edges_present_with_all_fields():
    _source, state = _run(2)
    assert _as_dict(state.transfers[(SIG_B_A, "0")]) == _expected_row(SIG_B_A, "0")
    assert _as_dict(state.transfers[(SIG_A_P1, "0.0")]) == _expected_row(SIG_A_P1, "0.0")
    b_a = state.transfers[(SIG_B_A, "0")]
    assert (b_a.sender, b_a.receiver, b_a.depth, b_a.asset, b_a.slot) == (B, A, 2, "sol", 110)


def test_third_hop_absent_at_depth_2():
    _source, state = _run(2)
    assert SIG_C_B not in _sigs(state)
    assert all(t.depth <= 2 for t in state.transfers.values())
    _source, state3 = _run(3)
    assert state3.transfers[(SIG_C_B, "0")].depth == 3


def test_transfer_after_first_buy_excluded():
    source, state = _run(3)
    assert (SIG_A_P1, "0.0") in state.transfers  # P1 розгорнуто
    assert SIG_X_P1 not in _sigs(state)
    assert SIG_X_P1 not in _fetched(source)  # навіть не запитується: before=купівля


def test_depth2_transfer_after_funding_edge_excluded_causal_cutoff():
    _source, state = _run(3)
    # E->B (115) після B->A (110); F->A (128) після останнього ребра A (A->P2, 125):
    # обидва раніше за будь-яку купівлю, але не могли фінансувати ребро.
    assert SIG_E_B not in _sigs(state)
    assert SIG_F_A not in _sigs(state)
    assert state.frontier_by_depth[2][B] == SIG_B_A


def test_cycle_a_d_a_terminates_without_duplicates():
    source, state = _run(3)
    assert state.transfers[(SIG_D_A, "0")].depth == 2
    assert state.transfers[(SIG_A_D, "0")].depth == 3
    rows = _rows(state)
    assert len(rows) == len({(r["signature"], r["instruction_path"]) for r in rows})
    # A розгорнуто один раз (рівень 1), повторно як відправник A->D не стає вершиною
    assert [p["before"] for p in _sig_calls(source, A)][0] == SIG_A_P2
    assert sum(1 for m, p in source.calls if m == "getTokenAccountsByOwner" and p["owner"] == A) == 1
    assert A not in state.frontier_by_depth.get(2, {}) and A not in state.frontier_by_depth.get(3, {})


def test_self_transfer_ignored():
    _source, state = _run(3)
    assert P3 in state.expanded
    assert SIG_SELF not in _sigs(state)
    assert all(t.sender != t.receiver for t in state.transfers.values())


def test_shared_funder_expanded_once_transfers_not_duplicated():
    source, state = _run(3)
    # A фінансує і P1, і P2: одна вершина рівня 1, розгорнута рівно раз
    assert state.frontier_by_depth[1] == {A: SIG_A_P2, S: SIG_S_P2}
    starts = [p for p in _sig_calls(source, A) if p["before"] == SIG_A_P2]
    assert len(starts) == 1
    assert sum(1 for m, p in source.calls if m == "getTokenAccountsByOwner" and p["owner"] == A) == 1
    assert {k for k in _keys(state) if k[0] in (SIG_A_P1, SIG_A_P2)} == {
        (SIG_A_P1, "0.0"), (SIG_A_P2, "0"), (SIG_A_P2, "1"),
    }
    assert A in state.expanded


def test_spl_inbound_to_existing_token_account_collected():
    _source, state = _run(1)
    t = state.transfers[(SIG_S_P2_TA, "0")]  # видно лише з історії токен-рахунку P2
    assert (t.sender, t.receiver, t.asset, t.amount, t.decimals, t.depth) == (
        S, P2, f"spl:{W['USDC']}", 25_000_000, 6, 1,
    )


def test_collect_spl_inbound_false_skips_token_account_calls():
    source, state = _run(3, spl=False)
    assert not [c for c in source.calls if c[0] == "getTokenAccountsByOwner"]
    token_accounts = {ta["pubkey"] for lst in RPC["getTokenAccountsByOwner"].values() for ta in lst}
    assert not [p for m, p in source.calls if m == "getSignaturesForAddress" and p["address"] in token_accounts]
    # еталон: «при false усі перекази з asset 'spl:*' відсутні»
    expected = [r for r in EXPECTED["transfers"] if not r["asset"].startswith("spl:")]
    assert _rows(state) == expected


def test_buyer_without_history_has_empty_funding_and_no_error():
    _source, state = _run(3)
    assert not [t for t in state.transfers.values() if t.receiver == P4]
    assert not [m for m in state.missing.values() if m.wallet == P4]
    assert P4 in state.expanded


# --- Додаткові межові випадки (critical) --------------------------------------------


def test_latest_edge_is_cutoff_when_several_edges_reach_node():
    _source, state = _run(2)
    # Межа A — найпізніше з ребер A->P1 (120) і A->P2 (125); G->A (122) лежить між ними:
    # з межею «найраніше ребро» він би зник.
    assert state.frontier_by_depth[1][A] == SIG_A_P2
    assert state.transfers[(SIG_G_A, "0")].depth == 2
    assert SIG_F_A not in _sigs(state)


def test_depth_is_minimal_over_multiple_paths():
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(3)
    state = _buyers(source, cfg)
    # B->A вже зустрічався довшим шляхом (глибина 3) — після BFS лишається мінімальна 2;
    # A->P1, записаний із глибиною 1, не «поглиблюється» повторною зустріччю.
    state.transfers[(SIG_B_A, "0")] = Transfer(**(_expected_row(SIG_B_A, "0") | {"depth": 3}))
    _bfs(source, cfg, state)
    assert state.transfers[(SIG_B_A, "0")].depth == 2
    # вершина з кількома шляхами (A: через P1, P2 і як відправник у D на рівні 3) — лише рівень 1
    levels = [d for d, nodes in state.frontier_by_depth.items() if A in nodes]
    assert levels == [1]
    assert all(t.depth == 2 for t in state.transfers.values() if t.receiver == A)


@pytest.mark.parametrize("depth, frontier_levels", [(1, {0}), (2, {0, 1}), (3, {0, 1, 2})])
def test_sender_becomes_node_only_if_next_level_within_funding_depth(depth, frontier_levels):
    _source, state = _run(depth)
    assert {d for d, nodes in state.frontier_by_depth.items() if nodes} == frontier_levels
    if depth == 3:
        assert state.frontier_by_depth[2] == {B: SIG_B_A, D: SIG_D_A, G: SIG_G_A}
        assert C not in state.expanded  # C->B на глибині 3: C — рівень 3, не розгортається


def test_expand_level_beyond_funding_depth_rejected():
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(2)
    state = _bfs(source, cfg)
    with pytest.raises(ValueError):
        expand_level(source, state, 3, cfg, DEADLINE)
    with pytest.raises(ValueError):
        expand_level(source, state, 0, cfg, DEADLINE)


def test_level_requires_previous_level():
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(3)
    state = _buyers(source, cfg)
    with pytest.raises(ValueError):
        expand_level(source, state, 2, cfg, DEADLINE)


def test_spl_dedup_between_wallet_and_token_account_history():
    source, state = _run(3)
    # 127 є і в історії P2, і в історії його USDC-рахунку: один запит, один запис
    assert _fetched(source).count(SIG_S_P2) == 1
    assert [k for k in _keys(state) if k[0] == SIG_S_P2] == [(SIG_S_P2, "1")]
    # загальний інваріант: кожен підпис запитано не більше одного разу за прогін (tx_cache)
    fetched = _fetched(source)
    assert len(fetched) == len(set(fetched))


def test_transfer_in_purchase_transaction_not_included_strict_cutoff():
    _source, state = _run(3)
    # Доставка mint у купівельній транзакції (і продаж U->P5 у «купівлі» пулу) видна в
    # історії токен-рахунку покупця зі слотом == слот межі — межа строга на рівні транзакції.
    assert (SIG_S_P2_TA, "0") in state.transfers  # історії токен-рахунків переглянуто
    assert not (_sigs(state) & BUY_SIGS)


def test_failed_transaction_ignored_and_not_fetched():
    source, state = _run(3)
    assert P1 in state.expanded and (SIG_A_P1, "0.0") in state.transfers
    assert SIG_FAILED not in _sigs(state)
    assert SIG_FAILED not in _fetched(source)  # err у списку підписів — не запитується


def test_failed_transaction_ignored_even_if_signature_list_has_no_err(tmp_path):
    def mutate(data):
        for entry in data["getSignaturesForAddress"][P1]:
            if entry["signature"] == SIG_FAILED:
                entry["err"] = None

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(3))
    assert SIG_FAILED in _fetched(source)
    assert SIG_FAILED not in _sigs(state)
    assert state.missing == {}


# --- Збої посеред рівня: не мовчки, решта триває, зібране зберігається ---------------


def test_null_transaction_recorded_as_unavailable_and_rest_kept(tmp_path):
    source = _variant(tmp_path, lambda d: d["getTransaction"].__setitem__(SIG_G_A, None))
    state = _bfs(source, _cfg(3))
    m = state.missing[(A, MissingReason.UNAVAILABLE)]
    assert (m.wallet, m.depth) == (A, 1) and SIG_G_A in m.detail
    assert {(SIG_B_A, "0"), (SIG_D_A, "0")} <= _keys(state)
    assert SIG_G_A not in _sigs(state)
    assert G not in state.frontier_by_depth[2]
    assert A not in state.expanded  # повтор спробує знову
    assert SIG_G_A not in state.tx_cache


def test_corrupt_record_recorded_as_corrupt_data_and_rest_kept(tmp_path):
    source = _variant(tmp_path, lambda d: d["getTransaction"][SIG_G_A].__setitem__("meta", None))
    state = _bfs(source, _cfg(3))
    m = state.missing[(A, MissingReason.CORRUPT_DATA)]
    assert (m.wallet, m.depth) == (A, 1) and SIG_G_A in m.detail
    assert {(SIG_B_A, "0"), (SIG_D_A, "0")} <= _keys(state)
    # решта рівня 3 розгорнута (B, D), лише G недосяжний
    assert (SIG_C_B, "0") in state.transfers and (SIG_A_D, "0") in state.transfers


def test_partially_corrupt_transaction_keeps_resolved_transfers_and_marks_corrupt(tmp_path):
    def mutate(data):
        tx = data["getTransaction"][SIG_A_P2]
        tx["transaction"]["message"]["instructions"].append({"programId": TOKEN, "accounts": [A, P2], "data": ""})

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(2))
    assert (SIG_A_P2, "0") in state.transfers and (SIG_A_P2, "1") in state.transfers
    m = state.missing[(P2, MissingReason.CORRUPT_DATA)]
    assert m.depth == 0 and SIG_A_P2 in m.detail
    assert state.frontier_by_depth[1][A] == SIG_A_P2  # межа з частково розібраного ребра


def test_rpc_failure_for_one_wallet_mid_level_recorded_and_level_continues():
    source = FixtureRpcSource(BASIC, failures=[FailFor(P2, RpcUnavailable("boom"), times=1)])
    cfg = _cfg(1)
    state = _bfs(source, cfg)
    m = state.missing[(P2, MissingReason.UNAVAILABLE)]
    assert m.depth == 0 and "boom" in m.detail
    assert P2 not in state.expanded
    assert (SIG_A_P1, "0.0") in state.transfers  # P1 до збою
    assert {P3, P4, P5} <= state.expanded        # після збою — продовжено
    # історія гаманця P2 не отримана — її перекази відсутні; токен-рахунки P2 отримано (FailFor
    # відпустив після першого збою) — SPL-перекази з них збережено, а не викинуто разом зі збоєм
    assert SIG_A_P2 not in _sigs(state)
    assert {(SIG_S_P2_TA, "0"), (SIG_S_P2, "1")} <= _keys(state)


class _FailOnNthPage(FixtureRpcSource):
    """Відмова на `n`-й сторінці історії конкретної адреси (посеред перегортання)."""

    def __init__(self, *args, address: str, n: int, exc: Exception, **kwargs):
        super().__init__(*args, **kwargs)
        self._address, self._n, self._exc, self._seen = address, n, exc, 0

    def get_signatures_for_address(self, address, **kwargs):
        if address == self._address:
            self._seen += 1
            if self._seen == self._n:
                self.calls.append(("getSignaturesForAddress", {"address": address, **kwargs, "deadline": None}))
                raise self._exc
        return super().get_signatures_for_address(address, **kwargs)


def test_rpc_failure_mid_history_keeps_records_collected_before_failure():
    # page_size=1: історія A до межі = 122(G->A), 120, 110(B->A), 108, 104; збій на 3-й сторінці
    source = _FailOnNthPage(BASIC, address=A, n=3, exc=RpcRateLimited(1.0))
    state = _bfs(source, _cfg(2, page_size=1))
    m = state.missing[(A, MissingReason.RATE_LIMITED)]
    assert m.depth == 1 and "getSignaturesForAddress" in m.detail
    assert (SIG_G_A, "0") in state.transfers           # зібрано до збою
    assert SIG_B_A not in _sigs(state)                  # після збою — не бачили
    assert A not in state.expanded


def test_rerun_with_same_state_is_idempotent():
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(3)
    state = _bfs(source, cfg)
    rows, n_calls = _rows(state), len(source.calls)
    assert len(rows) == len(EXPECTED["transfers"])
    _bfs(source, cfg, state)
    assert _rows(state) == rows
    assert len(source.calls) == n_calls  # розгорнуті вершини не запитуються повторно


def test_resume_after_failure_retries_only_failed_wallet_without_duplicates():
    source = FixtureRpcSource(BASIC, failures=[FailFor(A, RpcUnavailable("down"), times=1)])
    cfg = _cfg(2)
    state = _bfs(source, cfg)
    assert (A, MissingReason.UNAVAILABLE) in state.missing
    before_calls = len(source.calls)
    expand_level(source, state, 2, cfg, DEADLINE)
    new_calls = source.calls[before_calls:]
    assert {p.get("address", p.get("owner")) for m, p in new_calls if m != "getTransaction"} <= {A} | {
        ta["pubkey"] for ta in RPC["getTokenAccountsByOwner"].get(A, [])
    }
    refetched = [s for m, p in new_calls if m == "getTransaction" for s in p["signatures"]]
    assert SIG_A_P1 not in refetched and SIG_A_P2 not in refetched  # уже в tx_cache
    assert state.missing == {}
    assert A in state.expanded
    assert _rows(state) == [r for r in EXPECTED["transfers"] if r["depth"] <= 2]


# --- Межа на історії токен-рахунку (research R-8, рішення за ревʼю T-012) -------------

USDC = W["USDC"]
P2_USDC_TA = next(ta["pubkey"] for ta in RPC["getTokenAccountsByOwner"][P2] if ta["account"]["data"]["parsed"]["info"]["mint"] == USDC)
SPL_TEMPLATE = SIG_S_P2_TA  # S -> P2 (USDC-рахунок), лише в історії токен-рахунку
SOL_TEMPLATE = SIG_G_A      # G -> A, простий системний переказ


def _clone(data: dict, template: str, new_sig: str, slot: int, replace: dict[str, str] | None = None) -> None:
    """Копія транзакції-шаблону з новим підписом і слотом; `replace` — заміна адрес у ній."""
    text = json.dumps(data["getTransaction"][template]).replace(template, new_sig)
    for old, new in (replace or {}).items():
        text = text.replace(old, new)
    tx = json.loads(text)
    tx["slot"], tx["blockTime"] = slot, 1759400000 + slot
    data["getTransaction"][new_sig] = tx


def _entry(sig: str, slot: int) -> dict:
    return {"signature": sig, "slot": slot, "err": None, "memo": None,
            "blockTime": 1759400000 + slot, "confirmationStatus": "finalized"}


def _insert(data: dict, address: str, sig: str, slot: int, *, at: int | None = None) -> None:
    """Вставити запис в історію адреси (від найновішого): за слотом або на позицію `at`."""
    history = data["getSignaturesForAddress"].setdefault(address, [])
    if at is None:
        at = next((i for i, e in enumerate(history) if e["slot"] < slot), len(history))
    history.insert(at, _entry(sig, slot))


def _fake_sig(label: str) -> str:
    return (label + "1" * 88)[:88]


def test_spl_to_existing_usdc_account_at_or_after_buy_slot_excluded(tmp_path):
    # (a) P2 купує на слоті 210; USDC-рахунок у купівлі не бере участі (межі в його історії немає).
    late, later = _fake_sig("SPLlateA"), _fake_sig("SPLlateB")

    def mutate(data):
        for sig, slot in ((late, 211), (later, 230)):
            _clone(data, SPL_TEMPLATE, sig, slot)
            _insert(data, P2_USDC_TA, sig, slot)

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(1))
    assert late not in _sigs(state) and later not in _sigs(state)
    assert (SIG_S_P2_TA, "0") in state.transfers  # раніший SPL-переказ із тієї ж історії — на місці


def test_spl_into_token_account_of_depth2_node_after_its_edge_excluded(tmp_path):
    # (b) A (рівень 1, межа — ребро A->P2 на слоті 125) отримує USDC на власний токен-рахунок.
    ata_a = _fake_sig("ATAofA")[:44]
    before_edge, after_edge = _fake_sig("SPLtoAearly"), _fake_sig("SPLtoAlate")

    def mutate(data):
        data["getTokenAccountsByOwner"][A] = [{"pubkey": ata_a, "account": {"data": {"parsed": {"info": {
            "mint": USDC, "owner": A, "tokenAmount": {"amount": "0", "decimals": 6}}}}}}]
        for sig, slot in ((after_edge, 126), (before_edge, 121)):
            _clone(data, SPL_TEMPLATE, sig, slot, {P2_USDC_TA: ata_a, P2: A})
            _insert(data, ata_a, sig, slot)

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(2))
    assert after_edge not in _sigs(state)
    t = state.transfers[(before_edge, "0")]
    assert (t.sender, t.receiver, t.depth, t.asset) == (S, A, 2, f"spl:{USDC}")


def test_cutoff_present_in_token_account_history_cuts_by_position(tmp_path):
    # (c) межа P2 (купівля, слот 210) є в історії USDC-рахунку: новіший запис того самого слота
    # відкидається, старіший запис того самого слота (за позицією після межі) — береться.
    buy_p2 = next(b["first_buy_signature"] for b in EXPECTED["buyers"] if b["wallet"] == P2)
    newer, older = _fake_sig("SPLsameNewer"), _fake_sig("SPLsameOlder")

    def mutate(data):
        _clone(data, SPL_TEMPLATE, newer, 210)
        _clone(data, SPL_TEMPLATE, older, 210)
        history = data["getSignaturesForAddress"][P2_USDC_TA]
        history[0:0] = [_entry(newer, 210), _entry(buy_p2, 210), _entry(older, 210)]

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(1))
    assert newer not in _sigs(state)
    assert buy_p2 not in _sigs(state)
    assert state.transfers[(older, "0")].depth == 1


def test_cutoff_absent_from_token_account_history_same_slot_excluded_earlier_slot_included(tmp_path):
    # (d) межі в історії USDC-рахунку немає: той самий слот (210) — виключено (строго, R-8),
    # слот строго раніше (209) — включено.
    same, earlier = _fake_sig("SPLslot210"), _fake_sig("SPLslot209")

    def mutate(data):
        for sig, slot in ((same, 210), (earlier, 209)):
            _clone(data, SPL_TEMPLATE, sig, slot)
            _insert(data, P2_USDC_TA, sig, slot)

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(1))
    assert same not in _sigs(state)
    assert state.transfers[(earlier, "0")].depth == 1


def test_short_cycle_buyer_funder_buyer_terminates_without_duplicates(tmp_path):
    # P1 -> A (слот 115) і A -> P1 (120): цикл замикається на глибині 2 при funding_depth=3.
    back = _fake_sig("SOLp1toA")

    def mutate(data):
        _clone(data, SOL_TEMPLATE, back, 115, {G: P1})
        _insert(data, A, back, 115)
        _insert(data, P1, back, 115)

    source = _variant(tmp_path, mutate)
    state = _bfs(source, _cfg(3))
    t = state.transfers[(back, "0")]
    assert (t.sender, t.receiver, t.depth) == (P1, A, 2)
    assert [r for r in _rows(state) if r["signature"] != back] == EXPECTED["transfers"]
    assert all(P1 not in nodes for d, nodes in state.frontier_by_depth.items() if d > 0)
    p1_starts = [p for p in _sig_calls(source, P1) if p["before"] == BUY_SIGS_BY_WALLET[P1]]
    assert len(p1_starts) == 1  # P1 розгорнуто рівно раз
    fetched = _fetched(source)
    assert len(fetched) == len(set(fetched))


BUY_SIGS_BY_WALLET = {b["wallet"]: b["first_buy_signature"] for b in EXPECTED["buyers"]}
