# verifies: FR-001-09, FR-001-10
"""Збої джерела посеред збору (T-016): жоден виняток джерела не виходить із `collect`, кожен збій видно.

Контракт (FR-001-09/10, принцип V):
- `RpcRateLimited` / `RpcTimeout` / `RpcUnavailable` з БУДЬ-ЯКОГО звернення → `MissingHistory` гаманця
  (або `buyers.complete=false`) з причиною `rate_limited` / `timeout` / `unavailable` і `detail`, з якого
  видно метод, адресу чи підпис і клас винятку; `budget_exhausted` — лише коли винен дедлайн;
- збір продовжується для решти вершин; зібране до збою лишається в результаті;
- `status=complete` ніколи, якщо хоч одне звернення збоїло.

Рішення T-016 поза буквою контракту `rpc-source.md` («адаптер піднімає тільки три винятки»): базовий
`RpcError` чи його інший підклас — дефект адаптера, але з `collect` він теж не виходить і мапиться в
`unavailable` (найчесніше «не вдалось дізнатись»), а клас винятку видно в `detail`.

Матриця «метод × виняток × момент k»: для кожного сценарію збій вводиться на k-му зверненні (k = 1 …
усі звернення свіжого прогону + 1), одноразово і «назавжди» (`FailAfter`). Покриття видів звернень
(історія mint, пакети транзакцій перелічення, історія гаманця, токен-рахунки, історія токен-рахунку,
пакети транзакцій фінансування) перевіряється окремо. `get_account_info` колектор не викликає зовсім
(перевірка існування mint — сервіс, T-019), що теж закріплено тестом. Мережі немає.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.buyers import enumerate_buyers
from unmask.ingest.collector import CollectionState, collect
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult, MissingReason
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcError, RpcRateLimited, RpcTimeout, RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, CORRUPT, HUB = SCENARIOS / "basic", SCENARIOS / "corrupt", SCENARIOS / "hub"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
RPC = json.loads((BASIC / "rpc.json").read_text())
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
A, P2, P3 = W["A"], W["P2"], W["P3"]
BUYERS = [b["wallet"] for b in EXPECTED["buyers"]]
CORRUPT_MINT = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]["M"]
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
HUB_CASES = {c["name"]: c["config"] for c in HUB_EXPECTED["cases"]}

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
BUDGET = MissingReason.BUDGET_EXHAUSTED


def _sig(prefix: str) -> str:
    (sig,) = [s for s in RPC["getTransaction"] if s.startswith(prefix)]
    return sig


SIG_G_A, SIG_B_A, SIG_D_A = _sig("2avhiv"), _sig("3sWVjS"), _sig("2YTSM2")


class _VendorRpcError(RpcError):
    """Підклас `RpcError`, якого контракт не знає (дефект адаптера) — теж не має вийти з `collect`."""


# (виняток, очікувана причина)
SOURCE_ERRORS = [
    (RpcRateLimited(retry_after=1.0), MissingReason.RATE_LIMITED),
    (RpcTimeout("request timed out"), MissingReason.TIMEOUT),
    (RpcUnavailable("node down"), MissingReason.UNAVAILABLE),
]
ALL_ERRORS = SOURCE_ERRORS + [
    (RpcError("bare adapter error"), MissingReason.UNAVAILABLE),
    (_VendorRpcError("vendor quirk"), MissingReason.UNAVAILABLE),
]


def _ids(cases) -> list[str]:
    return [type(exc).__name__ for exc, _reason in cases]


def _cfg(config: dict | None = None, *, page_size: int | None = None, **overrides):
    cfg = load_config(SHIPPED)
    values = dict(EXPECTED["config"] if config is None else config)
    values.update(overrides)
    cfg = dataclasses.replace(cfg, **values)
    if page_size is not None:
        cfg = dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, page_size=page_size))
    return cfg


def _collect(source, cfg, mint: str = M, clock=None) -> tuple[CollectionState, IngestResult]:
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, collect(state, source, cfg, clock or FakeClock())


def _stable(result: IngestResult) -> tuple:
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _reasons(result: IngestResult) -> set[MissingReason]:
    c = result.completeness
    return {m.reason for m in c.missing} | ({c.buyers.reason} if not c.buyers.complete else set())


def _diagnostics(result: IngestResult) -> list[tuple[MissingReason, str]]:
    """Усі (причина, фрагмент detail): записи `missing` і перелічення покупців.

    `buyers.detail` після збою перегортання — один фрагмент із причиною `buyers.reason`; після проблем
    у пакетах транзакцій — фрагменти `"<reason>: <що>"` через `"; "`.
    """
    c = result.completeness
    out = [(m.reason, seg) for m in c.missing for seg in m.detail.split("; ")]
    if not c.buyers.complete:
        known = {r.value for r in MissingReason}
        for seg in c.buyers.detail.split("; "):
            head, sep, rest = seg.partition(": ")
            out.append((MissingReason(head), rest) if sep and head in known else (c.buyers.reason, seg))
    return out


def _call_key(method: str, params: dict) -> str:
    """Адреса чи підпис, за якими звернення має впізнаватись у `detail`."""
    if method == "getTransaction":
        return params["signatures"][0]
    if method == "getTokenAccountsByOwner":
        return params["owner"]
    return params["address"]


class _FailOnCall(FixtureRpcSource):
    """k-те звернення (рахунок з 1, усі методи разом) піднімає `exc`; `sticky` — і кожне наступне.

    Звернення, що впало, є в журналі `calls` (як у `FailAfter`).
    """

    def __init__(self, scenario_dir, k: int, exc: Exception, *, sticky: bool = False) -> None:
        super().__init__(scenario_dir)
        self._k, self._exc, self._sticky = k, exc, sticky

    def _enter(self, method, params):
        super()._enter(method, params)
        if self._call_count == self._k or (self._sticky and self._call_count > self._k):
            raise self._exc


class _FailTransactionsOnce(FixtureRpcSource):
    """Перший пакет `getTransaction`, що містить `signature`, падає з `exc` (курсори історії не зачіпає)."""

    def __init__(self, scenario_dir, signature: str, exc: Exception) -> None:
        super().__init__(scenario_dir)
        self._signature, self._exc, self._armed = signature, exc, True

    def get_transactions(self, signatures, *, deadline):
        if self._armed and self._signature in signatures:
            self._armed = False
            self.calls.append(("getTransaction", {"signatures": list(signatures)}))
            raise self._exc
        return super().get_transactions(signatures, deadline=deadline)


# --- 1. Ліміт після k звернень: причина rate_limited у кожного недорозгорнутого гаманця -------------


def test_rate_limit_after_k_calls_yields_incomplete_with_rate_limited_reason_per_wallet():
    # basic: звернення 1–3 — перелічення, 4–11 — вершина P2 (повністю), з 12-го — ліміт, що не відпускає
    cfg = _cfg()
    state, result = _collect(FixtureRpcSource(BASIC, failures=[FailAfter(12, RpcRateLimited(retry_after=2.0))]), cfg)
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.buyers.complete is True and [b.wallet for b in result.buyers] == BUYERS
    assert state.expanded == {P2}
    known_nodes = {w: level for level, nodes in state.frontier_by_depth.items() for w in nodes}
    # кожна відома, але не розгорнута вершина — у missing, з причиною саме rate_limited і своїм рівнем
    assert {m.wallet: m.depth for m in c.missing} == {w: lvl for w, lvl in known_nodes.items() if w != P2}
    assert {w for w in known_nodes if w != P2} >= set(BUYERS) - {P2} | {A, W["S"]}
    assert {m.reason for m in c.missing} == {MissingReason.RATE_LIMITED}
    for m in c.missing:
        assert m.wallet in m.detail and "RpcRateLimited" in m.detail  # перше звернення вершини — її історія
    # перекази P2 (зібрані до ліміту) — у результаті
    assert {t.receiver for t in result.transfers} == {P2}


# --- 2. Недоступність одного гаманця не зачіпає інших ----------------------------------------------


def test_unavailable_for_one_wallet_keeps_other_wallets_complete():
    cfg = _cfg()
    _fs, fresh = _collect(FixtureRpcSource(BASIC), cfg)
    source = FixtureRpcSource(BASIC, failures=[FailFor(P3, RpcUnavailable("502 bad gateway"), times=10**6)])
    state, result = _collect(source, cfg)
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE and c.buyers.complete is True
    assert [(m.wallet, m.depth, m.reason) for m in c.missing] == [(P3, 0, MissingReason.UNAVAILABLE)]
    (m,) = c.missing
    assert "getSignaturesForAddress" in m.detail and "getTokenAccountsByOwner" in m.detail and P3 in m.detail
    assert "502 bad gateway" in m.detail
    # решта вершин розгорнута, як у свіжому прогоні; перекази інших гаманців — ті самі
    assert state.expanded == {w for nodes in state.frontier_by_depth.values() for w in nodes} - {P3}
    assert result.transfers == tuple(t for t in fresh.transfers if t.receiver != P3)
    # збір не зупинився на P3: після нього були звернення для інших вершин
    p3_calls = [i for i, (_m, p) in enumerate(source.calls) if P3 in p.values()]
    assert any(P3 not in p.values() for _m, p in source.calls[p3_calls[-1] + 1:])


# --- 3. timeout ≠ budget_exhausted -----------------------------------------------------------------


def test_timeout_reason_distinct_from_budget_exhausted():
    cfg = _cfg()
    # звичайний таймаут запиту при достатньому бюджеті → timeout, збір триває
    clock = FakeClock(advance_per_call=1.0)
    source = FixtureRpcSource(BASIC, failures=[FailFor(A, RpcTimeout("request timed out"), times=10**6)], clock=clock)
    _state, plain = _collect(source, dataclasses.replace(cfg, time_budget_seconds=1000.0), clock=clock)
    assert {(m.wallet, m.reason) for m in plain.completeness.missing} == {(A, MissingReason.TIMEOUT)}
    assert BUDGET not in _reasons(plain)
    # той самий гаманець, обірваний дедлайном (A — звернення 38–41; бюджет спливає після 39-го) → budget_exhausted
    clock = FakeClock(advance_per_call=1.0)
    _state, cut = _collect(FixtureRpcSource(BASIC, clock=clock),
                           dataclasses.replace(cfg, time_budget_seconds=38.5), clock=clock)
    by_wallet = {m.wallet: m.reason for m in cut.completeness.missing}
    assert by_wallet[A] is BUDGET
    assert MissingReason.TIMEOUT not in _reasons(cut)


# --- 4. Збій під час перелічення покупців --------------------------------------------------------


@pytest.mark.parametrize(("exc", "reason"), ALL_ERRORS, ids=_ids(ALL_ERRORS))
@pytest.mark.parametrize(("k", "method"), [
    (1, "getSignaturesForAddress"), (2, "getSignaturesForAddress"), (3, "getTransaction"),
], ids=["mint_first_page", "mint_last_page", "mint_transactions"])
def test_failure_during_buyer_enumeration_marks_buyers_incomplete_with_reason(exc, reason, k, method):
    cfg = _cfg()
    source = _FailOnCall(BASIC, k, exc)
    _state, result = _collect(source, cfg)
    failed_method, params = source.calls[k - 1]
    assert failed_method == method
    b = result.completeness.buyers
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert (b.complete, b.reason) == (False, reason)
    assert method in b.detail and _call_key(method, params) in b.detail and type(exc).__name__ in b.detail
    # найстаріших записів не бачили (або жодного пакета не розібрано) — покупців не вигадано
    assert result.buyers == () and result.transfers == () and result.completeness.missing == ()


# --- 5. Часткові перекази зберігаються поряд із missing ------------------------------------------


@pytest.mark.parametrize(("exc", "reason"), SOURCE_ERRORS, ids=_ids(SOURCE_ERRORS))
def test_partial_transfers_retained_alongside_missing(exc, reason):
    # page_size=1: транзакції A запитуються по одній від найновішого (G->A, B->A, D->A, …); пакет D->A падає
    cfg = _cfg(page_size=1)
    _fs, fresh = _collect(FixtureRpcSource(BASIC), cfg)
    state, result = _collect(_FailTransactionsOnce(BASIC, SIG_D_A, exc), cfg)
    missing = {(m.wallet, m.reason): m for m in result.completeness.missing}
    assert set(missing) == {(A, reason)}
    assert missing[(A, reason)].depth == 1 and SIG_D_A in missing[(A, reason)].detail
    into_a = {t.signature for t in result.transfers if t.receiver == A}
    assert into_a == {SIG_G_A, SIG_B_A}  # зібране до збою — докази; D->A не отримано
    assert set(result.transfers) <= set(fresh.transfers)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    # частково переглянута A відправників не реєструє (хаб чи ні — невідомо): B->C не збирається
    assert A not in state.expanded and not {t for t in result.transfers if t.receiver == W["B"]}
    # повтор добирає рівно відсутнє
    resumed = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert _stable(resumed) == _stable(fresh)


# --- 6. Ніколи complete, якщо хоч одне звернення збоїло ------------------------------------------


@pytest.mark.parametrize(("exc", "reason"), SOURCE_ERRORS, ids=_ids(SOURCE_ERRORS))
def test_never_complete_when_any_missing(exc, reason):
    cfg = _cfg()
    fresh_source = FixtureRpcSource(BASIC)
    _fs, fresh = _collect(fresh_source, cfg)
    assert fresh.completeness.status is CompletenessStatus.COMPLETE
    total = len(fresh_source.calls)
    for sticky in (False, True):
        for k in range(1, total + 2):
            _state, result = _collect(_FailOnCall(BASIC, k, exc, sticky=sticky), cfg)
            c = result.completeness
            # статус виводиться з даних: complete ⇔ missing порожній і перелічення повне
            assert (c.status is CompletenessStatus.COMPLETE) == (not c.missing and c.buyers.complete), (sticky, k)
            if k <= total:
                assert c.status is CompletenessStatus.INCOMPLETE, (sticky, k)
                assert _reasons(result) == {reason}, (sticky, k)
            else:
                assert _stable(result) == _stable(fresh), (sticky, k)


# --- Матриця: будь-який RpcError з будь-якого звернення в будь-який момент -------------------------

MATRIX = [
    ("basic", BASIC, M, _cfg()),
    ("basic_ps1", BASIC, M, _cfg(page_size=1)),
    ("basic_no_spl", BASIC, M, _cfg(collect_spl_inbound=False, page_size=2)),
    ("hub_high_degree", HUB, HUB_EXPECTED["mint"], _cfg(HUB_CASES["hub_high_degree"])),
    ("corrupt", CORRUPT, CORRUPT_MINT, _cfg({"first_buyers_n": 3, "funding_depth": 2})),
]


def _call_kinds(directory: Path, calls, mint: str, enumeration_calls: int) -> list[str]:
    """Вид кожного звернення журналу: історія mint / гаманця / токен-рахунку, транзакції, токен-рахунки."""
    recorded = json.loads((directory / "rpc.json").read_text())["getTokenAccountsByOwner"]
    token_accounts = {acc["pubkey"] for accounts in recorded.values() for acc in accounts}
    kinds = []
    for i, (method, params) in enumerate(calls):
        if method == "getSignaturesForAddress":
            address = params["address"]
            kind = "sigs:mint" if address == mint else "sigs:token_account" if address in token_accounts else "sigs:wallet"
        elif method == "getTransaction":
            kind = "tx:buyers" if i < enumeration_calls else "tx:funding"
        else:
            kind = method
        kinds.append(kind)
    return kinds


# basic (усі шість видів звернень) — усі п'ять винятків; решта сценаріїв — три винятки контракту й базовий
# `RpcError` (невідомий підклас іде тим самим шляхом, що й базовий). hub дорогий (1,7 МБ фікстури, ~0,1 с на
# прогін), а мапінг виняток → причина від сценарію не залежить, тож там — `RpcTimeout` (взаємодія з
# `deadline_timeouts`) і `RpcError`.
_MATRIX_ERRORS = {"basic": tuple(type(exc) for exc, _r in ALL_ERRORS),
                  "hub_high_degree": (RpcTimeout, RpcError)}
_DEFAULT_MATRIX_ERRORS = (RpcRateLimited, RpcTimeout, RpcUnavailable, RpcError)
MATRIX_CASES = [
    pytest.param(name, directory, mint, cfg, exc, reason, id=f"{name}-{type(exc).__name__}")
    for name, directory, mint, cfg in MATRIX
    for exc, reason in ALL_ERRORS
    if type(exc) in _MATRIX_ERRORS.get(name, _DEFAULT_MATRIX_ERRORS)
]


@pytest.mark.parametrize(("name", "directory", "mint", "cfg", "exc", "reason"), MATRIX_CASES)
def test_any_rpc_error_from_any_call_at_any_moment_is_reported_never_raised(name, directory, mint, cfg, exc, reason):
    fresh_source = FixtureRpcSource(directory)
    _fs, fresh = _collect(fresh_source, cfg, mint)
    total = len(fresh_source.calls)
    fresh_reasons = _reasons(fresh)
    for sticky in (False, True):
        for k in range(1, total + 2):
            source = _FailOnCall(directory, k, exc, sticky=sticky)
            state, result = _collect(source, cfg, mint)  # виняток джерела тут = провал тесту
            where = (sticky, k)
            assert source.calls[:k] == fresh_source.calls[:k], where  # до збою — той самий шлях
            c = result.completeness
            if k > total:
                assert _stable(result) == _stable(fresh), where
                continue
            assert c.status is CompletenessStatus.INCOMPLETE, where
            reasons = _reasons(result)
            assert reason in reasons and BUDGET not in reasons, where
            assert reasons <= fresh_reasons | {reason}, where  # причина — саме тип винятку, без підміни
            # збій k-го звернення видно в detail: метод, адреса/підпис, клас винятку
            method, params = fresh_source.calls[k - 1]
            key = _call_key(method, params)
            assert any(r is reason and method in text and key in text and type(exc).__name__ in text
                       for r, text in _diagnostics(result)), (where, method, key, _diagnostics(result))
            if c.buyers.complete:  # перелічення вціліло — вибірка та сама, що у свіжому прогоні
                assert result.buyers == fresh.buyers, where
            # жодна відома вершина не зникла мовчки: розгорнута або в missing
            nodes = {w for level in state.frontier_by_depth.values() for w in level}
            assert nodes <= state.expanded | {m.wallet for m in c.missing}, where
            if not sticky:
                # одноразовий збій стосується рівно одного гаманця (або перелічення) — решта розгорнута…
                failed = {m.wallet for m in c.missing if m.reason is reason and type(exc).__name__ in m.detail}
                assert len(failed) == 1 or (not c.buyers.complete and reason is c.buyers.reason), where
                # …і повтор на тому самому стані дає свіжий результат
                resumed = collect(state, FixtureRpcSource(directory), cfg, FakeClock())
                assert _stable(resumed) == _stable(fresh), where


def test_failure_matrix_covers_every_source_call_kind_and_collect_never_checks_mint_account():
    every_kind = {"sigs:mint", "tx:buyers", "sigs:wallet", "getTokenAccountsByOwner", "sigs:token_account", "tx:funding"}
    for name, directory, mint, cfg in MATRIX:
        source = FixtureRpcSource(directory)
        _collect(source, cfg, mint)
        probe = FixtureRpcSource(directory)
        enumerate_buyers(probe, mint, CollectionState(mint=mint, config_version=cfg.version), cfg,
                         _NeverExpires())
        assert source.calls[:len(probe.calls)] == probe.calls
        kinds = set(_call_kinds(directory, source.calls, mint, len(probe.calls)))
        if name == "basic":  # basic проходить матрицю всіма п'ятьма винятками: метод × виняток × k — повна
            assert kinds == every_kind
        assert kinds <= every_kind
        # існування mint (`get_account_info`) перевіряє сервіс до збору (T-019), не колектор: збій на тому
        # кроці не може тут стати «токен не знайдено»
        assert all(method != "getAccountInfo" for method, _p in source.calls)


class _NeverExpires:
    def remaining(self) -> float:
        return float("inf")

    def expired(self) -> bool:
        return False

    def request_timeout(self, cap: float) -> float:
        return cap
