# verifies: FR-001-08
"""Межі розгортання вершини (research R-3; T-013).

`counterparty_threshold` — унікальні відправники вхідних переказів вершини лічаться інкрементально
від найновішої транзакції до межі; щойно їх стало БІЛЬШЕ порога, вершина фіксується як
`UnexpandedNode(reason=high_degree)`: зібране до порога лишається, переказ відправника-перевищувача
й усе старіше не збирається, відправники вершини не стають вершинами наступного рівня.
`max_signatures_per_wallet` — спільний ліміт підписів гаманця й його токен-рахунків: переглядаються
лише найновіші до межі, вершина — `signature_cap`, знайдені відправники розгортаються нормально.
`high_degree` має пріоритет. `unexpanded[]` не робить результат неповним.

Сценарій `hub` (`scenarios/hub/expected.json`, незалежний генератор): історія H до межі (ребро H->Q1)
— 1197 підписів, позиція j=0 — найновіший; вхідні від S1..S5 по колу на j = 17 + 61·i (i < 20),
S6 — найстаріший (j = 1196); решта — вихідні H->R*. S1 має власне джерело T1 (глибина 3).
Мережі немає.
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
from unmask.ingest.model import (
    BuyersCompleteness,
    Completeness,
    CompletenessStatus,
    MissingReason,
    UnexpandedNode,
    UnexpandedReason,
    transfer_sort_key,
)
from unmask.ingest.rpc.fixture import FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
HUB = SCENARIOS / "hub"
BASIC = SCENARIOS / "basic"
SHIPPED = ROOT / "config" / "ingest.yaml"

HUB_RPC = json.loads((HUB / "rpc.json").read_text())
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
CASES = {c["name"]: c for c in HUB_EXPECTED["cases"]}
HW = HUB_EXPECTED["wallets"]
H, Q1, Q2, T1, U1, W_ = (HW[k] for k in ("H", "Q1", "Q2", "T1", "U1", "W"))
S = [HW[f"S{k}"] for k in range(1, 7)]  # S[0] = S1 … S[5] = S6

H_HISTORY = HUB_RPC["getSignaturesForAddress"][H]
H_OLD = 1197  # підписів H строго раніше за ребро H->Q1
H_AT = [e["signature"] for e in H_HISTORY[3:]]  # H_AT[j]: позиція j від найновішого до межі
assert len(H_AT) == H_OLD and H_HISTORY[2]["slot"] == 3001  # третій запис — саме ребро H->Q1
INBOUND_J = [17 + 61 * i for i in range(20)]  # S1..S5 по колу
S6_J = H_OLD - 1

BASIC_RPC = json.loads((BASIC / "rpc.json").read_text())
BASIC_EXPECTED = json.loads((BASIC / "expected.json").read_text())
BW = BASIC_EXPECTED["wallets"]
P2, A, BS = BW["P2"], BW["A"], BW["S"]


class _StubDeadline:
    def expired(self) -> bool:
        return False

    def remaining(self) -> float:
        return math.inf


DEADLINE = _StubDeadline()


def _cfg(*, threshold: int, cap: int, depth: int = 3, n: int = 2, page_size: int = 1000, spl: bool = True):
    cfg = load_config(SHIPPED)
    return dataclasses.replace(
        cfg, first_buyers_n=n, funding_depth=depth, collect_spl_inbound=spl,
        counterparty_threshold=threshold, max_signatures_per_wallet=cap,
        rpc=dataclasses.replace(cfg.rpc, page_size=page_size),
    )


def _case_cfg(name: str, **overrides):
    c = CASES[name]["config"]
    kwargs = dict(threshold=c["counterparty_threshold"], cap=c["max_signatures_per_wallet"],
                  depth=c["funding_depth"], n=c["first_buyers_n"], spl=c["collect_spl_inbound"])
    kwargs.update(overrides)
    return _cfg(**kwargs)


def _bfs(source, cfg, state: CollectionState | None = None, mint: str = HUB_EXPECTED["mint"]) -> CollectionState:
    if state is None:
        state = CollectionState(mint=mint, config_version=cfg.version)
        assert enumerate_buyers(source, mint, state, cfg, DEADLINE).complete
    for depth in range(1, cfg.funding_depth + 1):
        expand_level(source, state, depth, cfg, DEADLINE)
    return state


def _hub(cfg, **source_kwargs):
    source = FixtureRpcSource(HUB, **source_kwargs)
    return source, _bfs(source, cfg)


def _rows(state: CollectionState) -> list[dict]:
    rows = []
    for t in sorted(state.transfers.values(), key=transfer_sort_key):
        d = dataclasses.asdict(t)
        d["asset"] = str(d["asset"])
        rows.append(d)
    return rows


def _sigs(state: CollectionState) -> set[str]:
    return {sig for sig, _path in state.transfers}


def _into(state: CollectionState, wallet: str) -> list:
    return sorted((t for t in state.transfers.values() if t.receiver == wallet), key=transfer_sort_key)


def _fetched(source) -> list[str]:
    return [s for m, p in source.calls if m == "getTransaction" for s in p["signatures"]]


def _sig_calls(source, address: str) -> list[dict]:
    return [p for m, p in source.calls if m == "getSignaturesForAddress" and p["address"] == address]


def _unexpanded_dict(u: UnexpandedNode) -> dict:
    d = dataclasses.asdict(u)
    d["reason"] = str(u.reason)
    return d


def _complete(state: CollectionState) -> Completeness:
    return Completeness.derive(state.missing.values(), BuyersCompleteness(complete=True, reason=None, detail=""))


# --- Golden: три конфігурації hub проти незалежного еталона ----------------------------


@pytest.mark.parametrize("name", ["hub_high_degree", "hub_signature_cap", "hub_control"])
def test_golden_hub_cases_match_expected(name):
    case = CASES[name]
    _source, state = _hub(_case_cfg(name))
    assert _rows(state) == case["transfers"]
    assert len(state.unexpanded) == len(case["unexpanded"])
    for got, want in zip(state.unexpanded, case["unexpanded"]):
        got = _unexpanded_dict(got)
        for key in want.get("not_asserted", []):
            got.pop(key)
        assert got == {k: v for k, v in want.items() if k != "not_asserted"}
    assert state.missing == {}
    assert _complete(state).status.value == case["completeness"]["status"]
    excluded = {e["signature"] for e in case["excluded"]}
    assert not (_sigs(state) & excluded)


# --- high_degree --------------------------------------------------------------------


def test_hub_over_threshold_marked_high_degree_with_counts():
    # поріг 3: S1 (j=17), S2 (78), S3 (139) — у межах порога; S4 (j=200) — четвертий унікальний
    _source, state = _hub(_cfg(threshold=3, cap=2000))
    assert state.unexpanded == [UnexpandedNode(
        wallet=H, depth=1, reason=UnexpandedReason.HIGH_DEGREE,
        counterparties_seen=4, signatures_seen=201, signatures_truncated=False,
    )]


def test_hub_collected_transfers_kept_and_senders_not_expanded():
    source, state = _hub(_cfg(threshold=3, cap=2000))
    collected = [t.signature for t in _into(state, H)]
    # зібрані до порога перекази лишаються (від найстарішого до найновішого: S3, S2, S1)
    assert collected == [H_AT[139], H_AT[78], H_AT[17]]
    assert all(t.depth == 2 for t in _into(state, H))
    # переказ перевищувача (S4) і все старіше — не зібрано
    assert H_AT[200] not in _sigs(state)
    assert not ({H_AT[j] for j in INBOUND_J[3:] + [S6_J]} & _sigs(state))
    # відправники хаба не стали вершинами рівня 2 і не запитувались
    assert not (set(S) & set(state.frontier_by_depth.get(2, {})))
    for sender in S:
        assert _sig_calls(source, sender) == []
    assert not [t for t in state.transfers.values() if t.depth == 3]
    # хаб розгорнуто (рішення конфігурації, не збій) — повторно не запитується
    assert H in state.expanded
    # решта графа розгортається нормально: W -> Q2
    assert [t.sender for t in _into(state, Q2)] == [W_]


@pytest.mark.parametrize("threshold, marked", [(6, False), (5, True)])
def test_threshold_boundary_exactly_threshold_not_exceeded(threshold, marked):
    # у H рівно 6 унікальних відправників: поріг 6 — не перевищено, поріг 5 — перевищено
    _source, state = _hub(_cfg(threshold=threshold, cap=2000))
    if marked:
        assert [(u.reason, u.counterparties_seen) for u in state.unexpanded] == [(UnexpandedReason.HIGH_DEGREE, 6)]
        assert H_AT[S6_J] not in _sigs(state)
    else:
        assert state.unexpanded == []
        assert _rows(state) == CASES["hub_control"]["transfers"]


def test_below_threshold_node_not_marked():
    _source, state = _hub(_cfg(threshold=3, cap=2000))
    # Q1, Q2 (по 1 відправнику) і W (0) — нижче порога, не позначені
    assert [u.wallet for u in state.unexpanded] == [H]
    _source, state = _hub(_case_cfg("hub_control"))
    assert state.unexpanded == []


@pytest.mark.parametrize("page_size", [1000, 50, 7])
def test_unique_senders_counted_incrementally_newest_first(page_size):
    # поріг 2: S1 повторюється пізніше, але рахується один раз; зупинка — на S3 (j=139)
    _source, state = _hub(_cfg(threshold=2, cap=2000, page_size=page_size))
    assert [t.signature for t in _into(state, H)] == [H_AT[78], H_AT[17]]
    (u,) = state.unexpanded
    assert (u.reason, u.counterparties_seen, u.signatures_seen) == (UnexpandedReason.HIGH_DEGREE, 3, 140)
    # той самий відправник повторно не збільшує лічильник: поріг 5 — S1..S5 по 4 рази, усі 20 зібрано
    _source, state = _hub(_cfg(threshold=5, cap=2000, page_size=page_size))
    assert sorted(t.signature for t in _into(state, H)) == sorted(H_AT[j] for j in INBOUND_J)


def test_high_degree_stops_fetching_transactions():
    # пакети по 50: зупинка на j=139 (третій пакет, j 100..149) — старіші транзакції H не запитуються
    source, _state = _hub(_cfg(threshold=2, cap=2000, page_size=50))
    fetched = set(_fetched(source))
    assert {H_AT[j] for j in range(150)} <= fetched
    assert not ({H_AT[j] for j in range(150, H_OLD)} & fetched)


# --- signature_cap --------------------------------------------------------------------


def test_long_history_marked_signature_cap_and_scans_only_cap_newest():
    # ліміт 50: у вікні j < 50 лише S1 (j=17); поріг 3 не досягнуто
    source, state = _hub(_cfg(threshold=3, cap=50))
    assert state.unexpanded == [UnexpandedNode(
        wallet=H, depth=1, reason=UnexpandedReason.SIGNATURE_CAP,
        counterparties_seen=1, signatures_seen=50, signatures_truncated=True,
    )]
    fetched = set(_fetched(source))
    assert {H_AT[j] for j in range(50)} <= fetched
    # старіші транзакції H не запитувались при розгортанні H (вихідні S1->H — з історії S1, вона рівня 2)
    s1_history = {e["signature"] for e in HUB_RPC["getSignaturesForAddress"][S[0]]}
    assert not ({H_AT[j] for j in range(50, H_OLD)} - s1_history) & fetched
    assert [t.signature for t in _into(state, H)] == [H_AT[17]]
    # знайдений відправник S1 розгортається нормально: T1 -> S1 на глибині 3
    assert S[0] in state.frontier_by_depth[2]
    assert [(t.sender, t.depth) for t in _into(state, S[0])] == [(T1, 3)]


def test_signature_cap_stops_paging_history():
    # сторінки по 100, ліміт 300: досить 4 сторінок (301 запис) — не 12
    source, state = _hub(_cfg(threshold=10, cap=300, page_size=100))
    assert len(_sig_calls(source, H)) == 4
    assert _rows(state) == CASES["hub_signature_cap"]["transfers"]


@pytest.mark.parametrize("cap, marked", [(H_OLD, False), (H_OLD - 1, True)])
def test_signature_cap_boundary_exactly_cap_not_truncated(cap, marked):
    _source, state = _hub(_cfg(threshold=10, cap=cap))
    if marked:
        assert state.unexpanded == [UnexpandedNode(
            wallet=H, depth=1, reason=UnexpandedReason.SIGNATURE_CAP,
            counterparties_seen=5, signatures_seen=H_OLD - 1, signatures_truncated=True,
        )]
        assert H_AT[S6_J] not in _sigs(state)
    else:
        assert state.unexpanded == []
        assert _rows(state) == CASES["hub_control"]["transfers"]


def _basic_p2(cap: int):
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(threshold=200, cap=cap, depth=1, n=5)
    return source, _bfs(source, cfg, mint=BASIC_EXPECTED["mint"])


def test_signature_cap_counts_wallet_and_token_accounts_together():
    # P2 до купівлі: гаманець {5UkNdV@127, RafQBM@125}, токен-рахунок {5UkNdV@127, wWN58u@126}:
    # окремо по 2, разом (спільний підпис — раз) 3
    sigs = BASIC_RPC["getSignaturesForAddress"]
    sig = {s[:6]: s for s in BASIC_RPC["getTransaction"]}
    assert [e["signature"] for e in sigs[P2]][1:] == [sig["5UkNdV"], sig["RafQBM"]]
    _source, state = _basic_p2(cap=3)
    assert [u.wallet for u in state.unexpanded if u.wallet == P2] == []

    _source, state = _basic_p2(cap=2)
    (u,) = [u for u in state.unexpanded if u.wallet == P2]
    assert u == UnexpandedNode(wallet=P2, depth=0, reason=UnexpandedReason.SIGNATURE_CAP,
                               counterparties_seen=1, signatures_seen=2, signatures_truncated=True)
    # переглянуто два найновіших (обидва SPL від S), найстаріший (A -> P2) — за межею
    assert sorted(t.signature for t in _into(state, P2)) == sorted([sig["5UkNdV"], sig["wWN58u"]])
    assert {t.sender for t in _into(state, P2)} == {BS}


# --- пріоритет і повнота ----------------------------------------------------------------


def test_high_degree_wins_over_signature_cap():
    # ліміт 300 обрізає історію (1197), а поріг 3 перевищено в межах вікна (S4, j=200)
    _source, state = _hub(_cfg(threshold=3, cap=300))
    assert state.unexpanded == [UnexpandedNode(
        wallet=H, depth=1, reason=UnexpandedReason.HIGH_DEGREE,
        counterparties_seen=4, signatures_seen=201, signatures_truncated=True,
    )]
    assert not (set(S) & set(state.frontier_by_depth.get(2, {})))


def test_threshold_exceeded_only_beyond_window_is_signature_cap():
    # за межею вікна відправників не видно — лише signature_cap, відправники розгортаються
    _source, state = _hub(_cfg(threshold=1, cap=50))
    (u,) = [u for u in state.unexpanded if u.wallet == H]
    assert (u.reason, u.counterparties_seen) == (UnexpandedReason.SIGNATURE_CAP, 1)
    assert S[0] in state.frontier_by_depth[2]


@pytest.mark.parametrize("name", ["hub_high_degree", "hub_signature_cap"])
def test_unexpanded_does_not_make_result_incomplete(name):
    _source, state = _hub(_case_cfg(name))
    assert state.unexpanded
    assert state.missing == {}
    assert _complete(state).status == CompletenessStatus.COMPLETE


# --- покупець, повторний виклик -----------------------------------------------------------


def test_buyer_can_be_unexpanded_high_degree():
    # поріг 1: у P2 від найновішого S (SPL@127, @126), далі A (@125) — другий унікальний
    source = FixtureRpcSource(BASIC)
    state = _bfs(source, _cfg(threshold=1, cap=300, depth=2, n=5), mint=BASIC_EXPECTED["mint"])
    (u,) = [u for u in state.unexpanded if u.wallet == P2]
    assert (u.depth, u.reason, u.counterparties_seen, u.signatures_seen) == (0, UnexpandedReason.HIGH_DEGREE, 2, 3)
    assert {t.sender for t in _into(state, P2)} == {BS}
    assert len(_into(state, P2)) == 2
    assert BS not in state.frontier_by_depth[1]
    assert A in state.frontier_by_depth[1]  # A лишається вершиною через P1


def test_rerun_with_same_state_does_not_duplicate_unexpanded():
    cfg = _case_cfg("hub_high_degree")
    source, state = _hub(cfg)
    before = (list(state.unexpanded), dict(state.transfers), len(source.calls))
    _bfs(source, cfg, state)
    assert (list(state.unexpanded), dict(state.transfers), len(source.calls)) == before


def test_resume_after_failure_keeps_single_unexpanded_entry():
    # сторінки по 100; друга сторінка історії H (before = j99) один раз падає: перша спроба бачить j < 100,
    # поріг 1 перевищено на S2 (j=78) — вершина і в missing, і в unexpanded, але не в expanded
    cfg = _cfg(threshold=1, cap=2000, page_size=100)
    source = FixtureRpcSource(HUB, failures=[FailFor(H_AT[99], RpcUnavailable("down"), times=1)])
    state = _bfs(source, cfg)
    assert (H, MissingReason.UNAVAILABLE) in state.missing
    assert H not in state.expanded
    expected = [UnexpandedNode(
        wallet=H, depth=1, reason=UnexpandedReason.HIGH_DEGREE,
        counterparties_seen=2, signatures_seen=79, signatures_truncated=False,
    )]
    assert state.unexpanded == expected

    _bfs(source, cfg, state)  # resume: H розгортається знову, тепер без збою
    assert state.missing == {}
    assert H in state.expanded
    assert state.unexpanded == expected  # замінено, не продубльовано
    assert [t.signature for t in _into(state, H)] == [H_AT[17]]
    assert not (set(S) & set(state.frontier_by_depth.get(2, {})))


# --- вершина зі збоєм не реєструє відправників (рішення власника процесу) ---------------------


def _hub_page2_fails():
    # сторінки по 100; друга сторінка історії H (before = j99) падає: видно лише S1 (j=17), S2 (j=78) —
    # поріг 5 не перевищено, але хаб це чи ні, невідомо
    return FixtureRpcSource(HUB, failures=[FailFor(H_AT[99], RpcUnavailable("down"), times=1)])


def test_failure_before_threshold_does_not_register_hub_senders():
    cfg = _case_cfg("hub_high_degree", page_size=100)
    source = _hub_page2_fails()
    state = _bfs(source, cfg)
    assert (H, MissingReason.UNAVAILABLE) in state.missing
    assert state.unexpanded == []
    assert not (set(S) & set(state.frontier_by_depth.get(2, {})))
    for sender in S:
        assert _sig_calls(source, sender) == []
    assert [t.signature for t in _into(state, H)] == [H_AT[78], H_AT[17]]  # зібране до збою лишається
    assert _complete(state).status == CompletenessStatus.INCOMPLETE


def test_resume_hub_becomes_high_degree_without_any_sender_subtree():
    cfg = _case_cfg("hub_high_degree", page_size=100)
    state = _bfs(_hub_page2_fails(), cfg)
    _bfs(FixtureRpcSource(HUB), cfg, state)  # resume тим самим станом, чисте джерело
    assert state.missing == {}
    assert [(u.wallet, u.reason, u.counterparties_seen) for u in state.unexpanded] == [
        (H, UnexpandedReason.HIGH_DEGREE, 6)]
    assert not [t for t in state.transfers.values() if t.receiver in S or t.depth == 3]
    assert _rows(state) == CASES["hub_high_degree"]["transfers"]


def test_signature_cap_stops_paging_token_account_history(tmp_path):
    # USDC-рахунок P2 (EtPd7G…): 127, 126 + 50 старіших фіктивних записів; ліміт 2, сторінки по 1.
    # Межі (купівля @210) в історії немає: max+1 = 3 придатних (127, 126, old00@100) і ще одна сторінка,
    # щоб побачити, що група слота 100 закінчилась (old01@99) — 4 сторінки, а не 53
    data = copy.deepcopy(BASIC_RPC)
    sigs = data["getSignaturesForAddress"]
    (ta,) = [acc["pubkey"] for acc in data["getTokenAccountsByOwner"][P2]
             if [e["slot"] for e in sigs.get(acc["pubkey"], [])] == [127, 126]]
    for i in range(50):
        sigs[ta].append({"signature": (f"old{i:02d}" + "1" * 88)[:88], "slot": 100 - i, "err": None,
                         "memo": None, "blockTime": 1759400000 + 100 - i, "confirmationStatus": "finalized"})
    (tmp_path / "rpc.json").write_text(json.dumps(data))
    source = FixtureRpcSource(tmp_path)
    cfg = _cfg(threshold=200, cap=2, depth=1, n=5, page_size=1)
    state = _bfs(source, cfg, mint=BASIC_EXPECTED["mint"])
    assert len(_sig_calls(source, ta)) == 4
    (u,) = [u for u in state.unexpanded if u.wallet == P2]
    assert (u.reason, u.signatures_seen, u.signatures_truncated) == (UnexpandedReason.SIGNATURE_CAP, 2, True)


# --- масштаб: перевиведення рівня лінійне за переказами (FR-001-16, SC-003) -------------------


class _CountingTransfers(dict):
    """`state.transfers`, що лічить перебрані елементи й падає, щойно перевищено бюджет."""

    def __init__(self, *args, budget: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.visits, self.budget = 0, budget

    def _tick(self, iterable):
        for item in iterable:
            self.visits += 1
            if self.visits > self.budget:
                raise AssertionError(f"перебрано понад {self.budget} записів переказів — не лінійно")
            yield item

    def items(self):
        return self._tick(super().items())

    def values(self):
        return self._tick(super().values())

    def keys(self):
        return self._tick(super().keys())

    def __iter__(self):
        return self._tick(super().__iter__())


def test_reconcile_level_is_linear_in_transfers_for_300_buyers_x_50_senders():
    # 300 покупців × 50 унікальних відправників = 15 000 нових вершин рівня 1 і 15 000 переказів;
    # квадратична інвалідація дала б ~2·10^8 переборів, лінійна — кілька проходів (тут ≤ 10·T)
    from unmask.ingest.model import Asset, Transfer

    n_buyers, k = 300, 50
    state = CollectionState(mint="M", config_version=1)
    frontier0: dict[str, str] = {}
    transfers = {}
    for i in range(n_buyers):
        buyer = f"buyer{i:03d}"
        frontier0[buyer] = f"buy{i:03d}"
        transfers[(f"buy{i:03d}", "0")] = Transfer(  # купівля — у transfers лише заради слота межі
            signature=f"buy{i:03d}", slot=10_000, block_time=None, instruction_path="0",
            sender=f"pool{i:03d}", receiver=f"pool{i:03d}", asset=Asset("sol"), amount=1, decimals=None, depth=1,
        )
        for j in range(k):
            sig = f"tx{i:03d}_{j:02d}"
            transfers[(sig, "0")] = Transfer(
                signature=sig, slot=1000 + j, block_time=None, instruction_path="0",
                sender=f"s{i:03d}_{j:02d}", receiver=buyer, asset=Asset("sol"), amount=1, decimals=None, depth=1,
            )
    total = len(transfers)
    # рівень 0 виводиться з state.buyers на кожному expand_level(1) (T-014) — покупці мусять бути в стані
    from unmask.ingest.model import AddressType, Buyer, Spend

    state.buyers = tuple(
        Buyer(wallet=w, rank=r, first_buy_signature=sig, first_buy_slot=10_000, first_buy_time=None,
              received_amount=1, spent=(Spend(Asset("sol"), 1),), programs=(), address_type=AddressType.WALLET)
        for r, (w, sig) in enumerate(frontier0.items(), start=1)
    )
    state.frontier_by_depth = {0: frontier0}
    state.expanded = set(frontier0)
    state.transfers = _CountingTransfers(transfers, budget=10 * total)

    cfg = _cfg(threshold=200, cap=300, depth=2, n=n_buyers)
    expand_level(None, state, 1, cfg, DEADLINE)  # усі покупці розгорнуті — лише перевиведення рівня 1

    assert len(state.frontier_by_depth[1]) == n_buyers * k
    assert state.frontier_by_depth[1]["s123_45"] == "tx123_45"
    assert state.transfers.visits <= 10 * total


# --- межа R-8 на токен-рахунку разом із зупинкою гортання ---------------------------------------


def test_token_account_cutoff_same_slot_boundary_with_paging_stop(tmp_path):
    # Ліміт 2, сторінки по 1. Купівля P2 — vryd3o@210.
    # USDC-рахунок (межі в ньому немає): новий запис y@210 — той самий слот, що й межа → відкидається.
    # Рахунок M (межа в ньому є): w@210 новіший за межу → відкидається; z@210 старший за межу за
    # позицією → береться. Обидва рахунки мають понад ліміт старших записів, тож гортання зупиняється.
    data = copy.deepcopy(BASIC_RPC)
    sigs = data["getSignaturesForAddress"]
    txs = data["getTransaction"]
    accounts = data["getTokenAccountsByOwner"][P2]
    buy_sig = next(b["first_buy_signature"] for b in BASIC_EXPECTED["buyers"] if b["wallet"] == P2)
    (usdc,) = [a["pubkey"] for a in accounts if [e["slot"] for e in sigs.get(a["pubkey"], [])] == [127, 126]]
    (mta,) = [a["pubkey"] for a in accounts if [e["signature"] for e in sigs.get(a["pubkey"], [])] == [buy_sig]]
    s_p2_ta = next(s for s in txs if s.startswith("wWN58u"))

    def entry(sig, slot):
        return {"signature": sig, "slot": slot, "err": None, "memo": None,
                "blockTime": 1759400000 + slot, "confirmationStatus": "finalized"}

    def fake(label):
        return (label + "1" * 88)[:88]

    y, w, z = fake("ySameSlot"), fake("wNewer"), fake("zOlder")
    tx = json.loads(json.dumps(txs[s_p2_ta]).replace(s_p2_ta, z))  # S -> P2 (SPL), слот 210
    tx["slot"], tx["blockTime"] = 210, 1759400210
    txs[z] = tx
    sigs[usdc].insert(0, entry(y, 210))
    sigs[mta] = [entry(w, 210), entry(buy_sig, 210), entry(z, 210)] + [
        entry(fake(f"mOld{i:02d}"), 100 - i) for i in range(10)]
    sigs[usdc] += [entry(fake(f"uOld{i:02d}"), 90 - i) for i in range(10)]
    (tmp_path / "rpc.json").write_text(json.dumps(data))

    source = FixtureRpcSource(tmp_path)
    state = _bfs(source, _cfg(threshold=200, cap=2, depth=1, n=5, page_size=1), mint=BASIC_EXPECTED["mint"])
    fetched = set(_fetched(source))
    assert y not in fetched and w not in fetched       # той самий слот без межі / новіший за межу
    assert z in _sigs(state)                           # старший за межу за позицією
    assert [k for k in state.missing if k[0] == P2] == []
    (u,) = [u for u in state.unexpanded if u.wallet == P2]
    assert (u.reason, u.signatures_seen, u.signatures_truncated) == (UnexpandedReason.SIGNATURE_CAP, 2, True)
    # гортання зупинено на max+1 = 3 придатних плюс сторінка, де слот уже менший за слот третього:
    # USDC — y (відкинуто), 127, 126, uOld00@90, uOld01@89; M — w, межа (відкинуто), z, mOld00@100,
    # mOld01@99, mOld02@98 — а не всі 13 сторінок
    assert len(_sig_calls(source, usdc)) == 5
    assert len(_sig_calls(source, mta)) == 6
