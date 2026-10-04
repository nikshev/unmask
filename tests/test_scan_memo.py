# verifies: FR-001-13
"""Мемо завершених сканувань джерел (T-022, `CollectionState.scan_memo`; known-issues §1).

Гранулярність resume до T-022 — вершина: гаманець, чиє розгортання (історія гаманця + список
токен-рахунків + історія кожного токен-рахунку + пакети транзакцій) дорожче за бюджет, повторювався
з нуля на кожному проході й не завершувався ніколи. Мемо зберігає незмінний знімок кожного ЗАВЕРШЕНОГО
сканування джерела (історія гаманця до межі, історія токен-рахунку до межі, список токен-рахунків
власника) одразу після завершення — тож кожен повтор просувається щонайменше на одне сканування.

Що перевіряється (мережі немає: `FixtureRpcSource`, `FakeClock`, кожне звернення — 1 с):
- бюджет, менший за розгортання одного гаманця, доходить до `complete` за скінченну кількість повторів,
  і результат == свіжому прогону; жодне завершене сканування не повторюється (журнал);
- ключ містить межу: пізніше ребро на resume → інший ключ → перескан; кожне поле ключа значуще;
- свіжий прогін: журнал викликів == журналу HEAD до T-022 (sha256) і == прогону без мемо;
- `resume == fresh` для обрізань бюджетом/збоєм на КОЖНОМУ k (basic/hub/corrupt і варіанти), кожен запис
  мемо == оракулу повного сканування (атомарність: неповне не кладеться, принцип V);
- stale `config_version` скидає мемо; кеш копіює мемо ізольовано (значення незмінні, діляться);
- список токен-рахунків власника запитується раз за життя партиційного стану.
"""

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from unmask.ingest import funding
from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult, MissingReason, UnexpandedReason
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcUnavailable
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB, CORRUPT = SCENARIOS / "basic", SCENARIOS / "hub", SCENARIOS / "corrupt"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
RPC = json.loads((BASIC / "rpc.json").read_text())
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
A, G, P2, P3 = W["A"], W["G"], W["P2"], W["P3"]
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
HUB_MINT = HUB_EXPECTED["mint"]
HUB_CASES = {c["name"]: c["config"] for c in HUB_EXPECTED["cases"]}
CORRUPT_MINT = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]["M"]

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
WALLET, TOKEN_ACCOUNT, LISTING = "wallet", "token_account", "token_accounts_listing"


def _sig(prefix: str) -> str:
    (sig,) = [s for s in RPC["getTransaction"] if s.startswith(prefix)]
    return sig


SIG_G_A = _sig("2avhiv")   # G -> A, слот 122
SIG_A_P2 = _sig("RafQBM")  # A -> P2, слот 125: межа A, поки A -> P3 не видно


def _fake_sig(label: str) -> str:
    return (label + "1" * 88)[:88]


SIG_A_P3 = _fake_sig("AtoP3")  # A -> P3, слот 140 (варіант): пізніше ребро → нова межа A


def _cfg(config: dict | None = None, *, page_size: int | None = None, tx_batch_size: int | None = None,
         **overrides):
    cfg = load_config(SHIPPED)
    values = dict(EXPECTED["config"] if config is None else config)
    values.update(overrides)
    cfg = dataclasses.replace(cfg, **values)
    if page_size is not None:
        # T-052: page_size — лише сторінка підписів; до T-052 він задавав і пачку транзакцій. Без явного
        # tx_batch_size пара зберігається — журнал викликів тесту той самий, що до T-052 (див. HEAD_CALL_LOG_SHA256)
        cfg = dataclasses.replace(cfg, rpc=dataclasses.replace(
            cfg.rpc, page_size=page_size, tx_batch_size=page_size if tx_batch_size is None else tx_batch_size))
    elif tx_batch_size is not None:
        cfg = dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, tx_batch_size=tx_batch_size))
    return cfg


def _stable(result: IngestResult) -> tuple:
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _fresh(directory: Path, cfg, mint: str):
    source = FixtureRpcSource(directory)
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, source, collect(state, source, cfg, FakeClock())


def _timed(state: CollectionState, directory: Path, cfg, budget: float, *, collector=collect):
    """Один прохід із бюджетом `budget` с при 1 с/звернення: рівно `floor(budget)` звернень."""
    cfg = dataclasses.replace(cfg, time_budget_seconds=budget)
    clock = FakeClock(advance_per_call=1.0)
    source = FixtureRpcSource(directory, clock=clock)
    return source, collector(state, source, cfg, clock)


class _Recorder(dict):
    """`scan_memo`, що пам'ятає кожен запис: ключ, номер звернення джерела на момент запису, знімок
    (чистка прибирає записи зі словника, але не з журналу)."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list = []
        self.records: dict = {}
        self.position = lambda: 0

    def __setitem__(self, key, value) -> None:
        self.log.append((key, self.position()))
        self.records[key] = value
        super().__setitem__(key, value)


def _start_call(key) -> tuple:
    """Перше звернення сканування за ключем (так його видно в журналі джерела)."""
    if key.kind == LISTING:
        return ("getTokenAccountsByOwner", key.address, None)
    before = key.cutoff.signature if key.kind == WALLET else None
    return ("getSignaturesForAddress", key.address, before)


def _calls(source) -> list[tuple]:
    out = []
    for method, params in source.calls:
        if method == "getSignaturesForAddress":
            out.append((method, params["address"], params["before"]))
        elif method == "getTokenAccountsByOwner":
            out.append((method, params["owner"], None))
        else:
            out.append((method, None, None))
    return out


def _oracle(data: dict, key):
    """Знімок ПОВНОГО сканування джерела з усієї записаної історії (без сторінок): R-1/R-8 → нормалізація.

    Нормалізація (page_size-незалежна): `cap+1`-й придатний запис задає поріг слота; береться все зі
    слотом не меншим (група слота на межі повністю) — більше злиттю вікна не потрібно (T-013)."""
    if key.kind == LISTING:
        accounts = data["getTokenAccountsByOwner"].get(key.address, [])
        return funding.TokenAccountsListing(accounts=tuple(sorted({a["pubkey"] for a in accounts})))
    history = data["getSignaturesForAddress"].get(key.address, [])
    order = [e["signature"] for e in history]
    cut = key.cutoff
    if key.kind == WALLET:
        tail = history[order.index(cut.signature) + 1:] if history else []
        valid, seen = [], set()
        for e in tail:
            if e.get("err") is None and e["signature"] != cut.signature and e["signature"] not in seen:
                seen.add(e["signature"])
                valid.append((e["signature"], e["slot"]))
    else:
        entries = [(e["signature"], e["slot"]) for e in history if e.get("err") is None]
        sigs = [s for s, _ in entries]
        if cut.signature in sigs:
            valid = entries[sigs.index(cut.signature) + 1:]
        else:
            valid = [(s, slot) for s, slot in entries if slot < cut.slot]
    cap = key.max_signatures
    if len(valid) > cap:
        threshold = valid[cap][1]
        return funding.HistoryScan(entries=tuple(e for e in valid if e[1] >= threshold), truncated=True)
    return funding.HistoryScan(entries=tuple(valid), truncated=False)


def _assert_memo_matches_oracle(state: CollectionState, data: dict, cfg) -> int:
    """Кожен запис мемо — повний знімок свого джерела (атомарність, принцип V); ключ — з поточного конфігу."""
    for key, record in state.scan_memo.items():
        assert record == _oracle(data, key), key
        assert key.collect_spl_inbound == cfg.collect_spl_inbound and key.commitment == cfg.commitment
        if key.kind != LISTING:
            assert key.max_signatures == cfg.max_signatures_per_wallet
    return len(state.scan_memo)


def _assert_memo_only_for_live_unfinished_nodes(state: CollectionState) -> None:
    """Історії — лише для вершин, що ще не розгорнуті й мають саме цю межу (рішення про чистку)."""
    live = {(w, c) for level in state.frontier_by_depth.values() for w, c in level.items()
            if w not in state.expanded}
    for key in state.scan_memo:
        if key.kind != LISTING:
            assert (key.owner, key.cutoff.signature) in live, key


# --- 1. Бюджет, менший за розгортання одного гаманця: повтори доходять до complete ---------------


def _service_until_complete(cfg, budget: float, directory: Path = BASIC, mint: str = M, limit: int = 80):
    cache = ResultCache()
    clock = FakeClock(advance_per_call=1.0)
    source = FixtureRpcSource(directory, clock=clock)
    service = IngestService(dataclasses.replace(cfg, time_budget_seconds=budget), source, clock=clock, cache=cache)
    results = [service.collect(mint)]
    while results[-1].completeness.status is CompletenessStatus.INCOMPLETE and len(results) < limit:
        results.append(service.collect(mint))
    return results, source, cache


@pytest.mark.parametrize("budget", [3.0, 4.0, 5.0, 6.0])
def test_wallet_exceeding_budget_completes_across_resumes(budget):
    # Розгортання P2 (перший у порядку sorted(): історія гаманця 2 сторінки + список токен-рахунків +
    # 2 токен-рахунки по 2 сторінки + пакет ≈ 8 звернень) не вміщується в бюджет (крок 4 сервісу забирає 1).
    # До T-022 кожен повтор робив ті самі звернення й не завершувався ніколи (known-issues §1).
    cfg = _cfg()
    results, source, cache = _service_until_complete(cfg, budget)
    final = results[-1]
    assert final.completeness.status is CompletenessStatus.COMPLETE, f"no progress after {len(results)} runs"
    _s, fresh_source, fresh = _fresh(BASIC, dataclasses.replace(cfg, time_budget_seconds=budget), M)
    assert _stable(final) == _stable(fresh)
    assert final.metadata.resumed is True
    # повторів не більше, ніж звернень у свіжому прогоні (кожен повтор завершує ≥ 1 сканування)
    assert len(results) <= len(fresh_source.calls)
    assert cache.get_partial(M) is None and cache.get_complete(M) == final


def test_budget_shorter_than_any_source_scan_honestly_never_completes():
    # межа гарантії: бюджет 2 с = крок 4 + одне звернення; історія гаманця P2 — дві сторінки (сторінка +
    # порожня), тож жодне сканування не завершується. Результат — чесний incomplete, не хибний complete.
    results, _source, cache = _service_until_complete(_cfg(), 2.0, limit=12)
    assert all(r.completeness.status is CompletenessStatus.INCOMPLETE for r in results)
    assert cache.get_complete(M) is None


# --- 2. Гарантія прогресу: кожен повтор завершує щонайменше одне сканування ---------------------


def _progress(state: CollectionState) -> tuple[int, int]:
    """(завершені сканування джерел вершин, збережений прогрес загалом). Друге — сума монотонних у
    послідовності обрізань бюджетом величин: сторінки історії mint, закешовані транзакції (пакети),
    завершені сканування, розгорнуті вершини. Кожне завершене звернення-сторінка mint чи пакет
    транзакцій теж зберігається до T-022 — мемо закриває саме сканування джерел вершин."""
    scans = len(state.scan_memo.log)
    return scans, scans + len(state.mint_signatures) + len(state.tx_cache) + len(state.expanded)


def _resume_sequence(directory: Path, cfg, mint: str, budget: float, limit: int = 120):
    """Повтори `resume` того самого стану з бюджетом `budget`; мемо — `_Recorder` (журнал записів)."""
    state = CollectionState(mint=mint, config_version=cfg.version)
    state.scan_memo = _Recorder()
    calls: list[tuple] = []
    runs = []
    while len(runs) < limit:
        cfg_b = dataclasses.replace(cfg, time_budget_seconds=budget)
        clock = FakeClock(advance_per_call=1.0)
        source = FixtureRpcSource(directory, clock=clock)
        offset = len(calls)
        state.scan_memo.position = lambda: offset + len(source.calls)
        result = (collect if not runs else resume)(state, source, cfg_b, clock)
        calls.extend(_calls(source))
        runs.append((result, _progress(state), len(source.calls)))
        if result.completeness.status is CompletenessStatus.COMPLETE:
            break
    return state, calls, runs


@pytest.mark.parametrize("budget", [2.0, 3.0, 5.0])
def test_each_resume_makes_progress_at_least_one_source_scan(budget):
    # найдорожче сканування basic при page_size=1000 — 2 звернення (сторінка + порожня)
    cfg = _cfg()
    state, calls, runs = _resume_sequence(BASIC, cfg, M, budget)
    assert runs[-1][0].completeness.status is CompletenessStatus.COMPLETE
    previous = (0, 0)
    for _result, (scans, total), n_calls in runs[:-1]:
        assert n_calls > 0
        # кожен неповний повтор зберіг щонайменше одне завершене сканування: джерела вершини (мемо),
        # сторінку історії mint чи пакет транзакцій (tx_cache) — і нічого не втратив
        assert total > previous[1], "a resume saved no progress"
        assert scans >= previous[0]
        previous = (scans, total)
    assert previous[0] > 0  # прогрес справді йшов і через мемо
    # жодне сканування не записано двічі й не починалось знову після завершення (журнал джерела)
    keys = [key for key, _pos in state.scan_memo.log]
    assert len(keys) == len(set(keys))
    for key, position in state.scan_memo.log:
        assert _start_call(key) not in calls[position:], key
    _s, _src, fresh = _fresh(BASIC, dataclasses.replace(cfg, time_budget_seconds=budget), M)
    assert _stable(runs[-1][0]) == _stable(fresh)


# --- 3. Ключ: межа та інші параметри --------------------------------------------------------------


def _clone(data: dict, template: str, new_sig: str, slot: int, replace: dict[str, str]) -> None:
    text = json.dumps(data["getTransaction"][template]).replace(template, new_sig)
    for old, new in replace.items():
        text = text.replace(old, new)
    tx = json.loads(text)
    tx["slot"], tx["blockTime"] = slot, 1759400000 + slot
    data["getTransaction"][new_sig] = tx


def _insert(data: dict, address: str, sig: str, slot: int) -> None:
    history = data["getSignaturesForAddress"].setdefault(address, [])
    at = next((i for i, e in enumerate(history) if e["slot"] < slot), len(history))
    history.insert(at, {"signature": sig, "slot": slot, "err": None, "memo": None,
                        "blockTime": 1759400000 + slot, "confirmationStatus": "finalized"})


def _a_funds_p3_variant(tmp_path: Path) -> Path:
    """A -> P3 @140: у свіжому прогоні межа A — 140 (у вікні F -> A @128); поки P3 не розгорнуто — 125."""
    data = json.loads(json.dumps(RPC))
    _clone(data, SIG_G_A, SIG_A_P3, 140, {A: P3, G: A})
    _insert(data, A, SIG_A_P3, 140)
    _insert(data, P3, SIG_A_P3, 140)
    directory = tmp_path / "a_funds_p3"
    directory.mkdir()
    (directory / "rpc.json").write_text(json.dumps(data))
    return directory


def test_memo_key_changes_when_cutoff_moves_and_source_is_rescanned(tmp_path):
    directory = _a_funds_p3_variant(tmp_path)
    data = json.loads((directory / "rpc.json").read_text())
    cfg = _cfg()
    # перший прохід: P3 недоступний (A -> P3 не видно, межа A = 125); у A завершено історію гаманця і
    # список токен-рахунків, але пакет транзакцій A падає — A не розгорнута, її сканування в мемо
    failures = [FailFor(P3, RpcUnavailable("down"), times=1), FailFor(SIG_G_A, RpcUnavailable("down"), times=1)]
    state = CollectionState(mint=M, config_version=cfg.version)
    first = collect(state, FixtureRpcSource(directory, failures=failures), cfg, FakeClock())
    assert {m.wallet for m in first.completeness.missing} >= {A, P3}
    assert state.frontier_by_depth[1][A] == SIG_A_P2
    old_keys = {k for k in state.scan_memo if k.owner == A}
    assert {(k.kind, k.cutoff.signature if k.cutoff else None) for k in old_keys} == {
        (WALLET, SIG_A_P2), (LISTING, None)}
    _assert_memo_matches_oracle(state, data, cfg)

    source = FixtureRpcSource(directory)
    result = resume(state, source, cfg, FakeClock())
    calls = _calls(source)
    # нова межа → новий ключ → перескан історії A від A -> P3; старий знімок не використано й прибрано
    assert ("getSignaturesForAddress", A, SIG_A_P3) in calls
    assert ("getSignaturesForAddress", A, SIG_A_P2) not in calls
    assert ("getTokenAccountsByOwner", A, None) not in calls  # список — за власником, без межі
    assert not [k for k in state.scan_memo if k.owner == A and k.kind == WALLET]
    _s, _src, fresh = _fresh(directory, cfg, M)
    assert _stable(result) == _stable(fresh)
    assert result.completeness.status is CompletenessStatus.COMPLETE


def _key(kind: str, address: str, owner: str, cutoff, cfg):
    return funding.ScanKey(kind=kind, address=address, owner=owner, cutoff=cutoff,
                           max_signatures=cfg.max_signatures_per_wallet if kind != LISTING else None,
                           collect_spl_inbound=cfg.collect_spl_inbound, commitment=cfg.commitment)


P2_BUY = next(b for b in EXPECTED["buyers"] if b["wallet"] == P2)
P2_CUTOFF = funding.Cutoff(P2_BUY["first_buy_signature"], P2_BUY["first_buy_slot"])
P2_ACCOUNTS = sorted(a["pubkey"] for a in RPC["getTokenAccountsByOwner"][P2])
SIG_S_P2 = _sig("5UkNdV")  # S -> P2 @127 — запис в історії P2, тож придатний як ІНША межа P2
OTHER_CUTOFF = funding.Cutoff(SIG_S_P2, 127)


def _scan_p2(state: CollectionState, cfg, cutoff=P2_CUTOFF):
    source = FixtureRpcSource(BASIC)
    problems: list = []
    window = funding._node_signatures(source, state, P2, cutoff, cfg, _NeverExpires(), problems)
    assert problems == []
    return window, _calls(source)


class _NeverExpires:
    def expired(self) -> bool:
        return False

    def remaining(self) -> float:
        return float("inf")


def _poisoned(memo: dict, *, keep_listing: bool) -> dict:
    """Ті самі ключі, знімки історій — пастки (порожні: влучання змінило б вікно/журнал). Список
    токен-рахунків, якщо `keep_listing`, лишається справжнім: від межі й ліміту він не залежить."""
    return {key: (record if key.kind == LISTING and keep_listing
                  else funding.TokenAccountsListing(accounts=()) if key.kind == LISTING
                  else funding.HistoryScan(entries=(), truncated=False)) for key, record in memo.items()}


@pytest.mark.parametrize("field", ["cutoff", "max_signatures", "collect_spl_inbound", "commitment"])
def test_every_key_field_matters_record_of_other_scan_parameters_is_not_used(field):
    # мемо, заповнене ВИРОБНИЧИМ кодом для сканування P2 з іншим значенням рівно одного параметра (інша межа,
    # ліміт, collect_spl_inbound, commitment), з отруєними знімками історій — не використовується: джерело
    # гортається, вікно й журнал — як у свіжого сканування. Мутант, що не кладе поле в ключ, влучає в пастку
    cfg = _cfg()
    trap_cfg = {"max_signatures": dataclasses.replace(cfg, max_signatures_per_wallet=1),
                "collect_spl_inbound": dataclasses.replace(cfg, collect_spl_inbound=False),
                "commitment": dataclasses.replace(cfg, commitment="confirmed")}.get(field, cfg)
    trap_cutoff = OTHER_CUTOFF if field == "cutoff" else P2_CUTOFF
    reference, reference_calls = _scan_p2(CollectionState(mint=M, config_version=cfg.version), cfg)

    trap = CollectionState(mint=M, config_version=cfg.version)
    _scan_p2(trap, trap_cfg, trap_cutoff)
    assert any(k.kind != LISTING for k in trap.scan_memo)
    shared_listing = field in ("cutoff", "max_signatures")  # список — за власником, без межі й ліміту
    state = CollectionState(mint=M, config_version=cfg.version,
                            scan_memo=_poisoned(trap.scan_memo, keep_listing=shared_listing))
    window, calls = _scan_p2(state, cfg)
    assert window == reference
    if shared_listing:
        reference_calls = [c for c in reference_calls if c[0] != "getTokenAccountsByOwner"]
    assert calls == reference_calls  # жодного влучання в пастку


@pytest.mark.parametrize("field", ["address", "kind"])
def test_lookup_distinguishes_source_address_and_kind(field):
    # знімок-пастка під ключем, що відрізняється лише адресою джерела або типом джерела, не використовується
    cfg = _cfg()
    empty = funding.HistoryScan(entries=(), truncated=False)
    reference, reference_calls = _scan_p2(CollectionState(mint=M, config_version=cfg.version), cfg)
    state = CollectionState(mint=M, config_version=cfg.version)
    if field == "address":
        state.scan_memo[_key(WALLET, A, P2, P2_CUTOFF, cfg)] = empty
        for _pubkey in P2_ACCOUNTS:
            state.scan_memo[_key(TOKEN_ACCOUNT, A, P2, P2_CUTOFF, cfg)] = empty
        state.scan_memo[_key(LISTING, A, P2, None, cfg)] = funding.TokenAccountsListing(accounts=())
    else:
        state.scan_memo[_key(TOKEN_ACCOUNT, P2, P2, P2_CUTOFF, cfg)] = empty
        for pubkey in P2_ACCOUNTS:
            state.scan_memo[_key(WALLET, pubkey, P2, P2_CUTOFF, cfg)] = empty
    window, calls = _scan_p2(state, cfg)
    assert (window, calls) == (reference, reference_calls)


def test_record_under_exact_key_is_used_without_source_calls():
    # той самий знімок під ТОЧНИМ ключем — використовується: нуль звернень, вікно == свіжому
    cfg = _cfg()
    fresh_state = CollectionState(mint=M, config_version=cfg.version)
    reference, reference_calls = _scan_p2(fresh_state, cfg)
    assert {k.kind for k in fresh_state.scan_memo} == {WALLET, LISTING, TOKEN_ACCOUNT}
    assert len(fresh_state.scan_memo) == 2 + len(P2_ACCOUNTS)
    assert len(reference_calls) == 2 + 1 + 2 * len(P2_ACCOUNTS)
    state = CollectionState(mint=M, config_version=cfg.version, scan_memo=dict(fresh_state.scan_memo))
    window, calls = _scan_p2(state, cfg)
    assert (window, calls) == (reference, [])


def test_page_size_not_in_key_memo_records_identical_for_every_page_size():
    # T-013: вікно не залежить від page_size; нормалізований знімок — теж, тож page_size у ключі не потрібен
    for directory, mint, config in [(BASIC, M, EXPECTED["config"]),
                                    (BASIC, M, {**EXPECTED["config"], "max_signatures_per_wallet": 2}),
                                    (BASIC, M, {**EXPECTED["config"], "max_signatures_per_wallet": 1}),
                                    (HUB, HUB_MINT, HUB_CASES["hub_signature_cap"])]:
        records = []
        for page_size in (1, 2, 3, 7, 1000):
            state = CollectionState(mint=mint, config_version=load_config(SHIPPED).version, scan_memo=_Recorder())
            collect(state, FixtureRpcSource(directory), _cfg(config, page_size=page_size), FakeClock())
            records.append(state.scan_memo.records)
        assert records[0], directory
        assert all(r == records[0] for r in records[1:]), (directory, config)
        data = json.loads((directory / "rpc.json").read_text())
        for key, record in records[0].items():
            assert record == _oracle(data, key)


# --- 4. Свіжий прогін: журнал викликів не змінився ------------------------------------------------

# sha256 журналу `FixtureRpcSource.calls` свіжого `collect` на HEAD до T-022 (73fa297), json з sort_keys.
HEAD_CALL_LOG_SHA256 = {
    "basic|1|spl": "df2c7c7282414ec675a58ab161ba394420d320174ca10448e4c6155ae15ca77e",
    "basic|1|nospl": "2b25f7809cd2610314efcfa539abae11264d565334825b155191d52f4b5f8e35",
    "basic|2|spl": "15561e2d8b2af293d7867b8e7f8d15edabee22dc6c6f81ef06d712ce94dfdb31",
    "basic|2|nospl": "8af21f1b84a0a41bfcafff6ebf50004336ef461613a610cd8aea0424b563a19b",
    "basic|1000|spl": "d02d2420128e752303cacc645e61459b89e13b799e8a7e06c4a17cff0ff31a35",
    "basic|1000|nospl": "3c8b95ac659e31d773cff79f1b7f4600c42b28844eea3ff6df136bda6b3fc519",
    "basic_caps|2": "4db3e9d89219126409fc676dc57a569876626fbd9c226b953ab7dac845bbc3c7",
    "hub|hub_high_degree|7": "2357f1cf7d1067ef7c5d068c2afc206c4e7d61b5889d17fe26b55a52a92af64c",
    "hub|hub_signature_cap|7": "e5bb31ce7f655ae863ce72f414a1c95ece9aa30f6b4b8c5db63a370e5e0a23ca",
    "hub|hub_control|7": "95fe3fc6df842702dcf2df959f97ead8b3884581f56a1441c84f508e324ab06c",
    "corrupt|1000": "17023431ffc9b2c5346dd4d8c24d0d9d206210c2458ba78d0f477c146279fdfb",
}


def _call_log_cases():
    hub_cfg = HUB_CASES
    for page_size in (1, 2, 1000):
        for spl in (True, False):
            yield f"basic|{page_size}|{'spl' if spl else 'nospl'}", BASIC, M, _cfg(
                page_size=page_size, collect_spl_inbound=spl)
    yield "basic_caps|2", BASIC, M, _cfg(counterparty_threshold=1, max_signatures_per_wallet=2, page_size=2)
    for name, config in hub_cfg.items():
        yield f"hub|{name}|7", HUB, HUB_MINT, _cfg(config, page_size=7)
    yield "corrupt|1000", CORRUPT, CORRUPT_MINT, _cfg({"first_buyers_n": 3, "funding_depth": 2}, page_size=1000)


def _log_hash(calls) -> str:
    return hashlib.sha256(json.dumps(calls, sort_keys=True).encode()).hexdigest()


class _NoMemo(dict):
    """Мемо, що нічого не зберігає: журнал «як без T-022»."""

    def __setitem__(self, key, value) -> None:
        return None


def test_memo_does_not_change_fresh_run_call_log():
    seen = set()
    for name, directory, mint, cfg in _call_log_cases():
        state = CollectionState(mint=mint, config_version=cfg.version)
        assert state.scan_memo == {}  # свіжий прогін стартує з порожнього мемо
        source = FixtureRpcSource(directory)
        result = collect(state, source, cfg, FakeClock())
        assert _log_hash(source.calls) == HEAD_CALL_LOG_SHA256[name], name
        bare = CollectionState(mint=mint, config_version=cfg.version, scan_memo=_NoMemo())
        bare_source = FixtureRpcSource(directory)
        bare_result = collect(bare, bare_source, cfg, FakeClock())
        assert bare_source.calls == source.calls, name  # мемо в свіжому прогоні ні разу не влучає
        assert _stable(bare_result) == _stable(result), name
        assert result.metadata.rpc_calls == len(source.calls)
        seen.add(name)
    assert seen == set(HEAD_CALL_LOG_SHA256)


# --- 5. resume == fresh з мемо: перебір обрізань на кожному k -------------------------------------

PROPERTY_CASES = [
    ("basic", BASIC, M, EXPECTED["config"], None),
    ("basic_ps2", BASIC, M, EXPECTED["config"], 2),
    ("basic_caps", BASIC, M, {**EXPECTED["config"], "counterparty_threshold": 1, "max_signatures_per_wallet": 2}, 1),
    ("basic_nospl", BASIC, M, {**EXPECTED["config"], "collect_spl_inbound": False}, None),
    ("hub_cap", HUB, HUB_MINT, HUB_CASES["hub_signature_cap"], 50),
    ("hub_degree", HUB, HUB_MINT, HUB_CASES["hub_high_degree"], None),
    ("corrupt", CORRUPT, CORRUPT_MINT, {"first_buyers_n": 3, "funding_depth": 2}, None),
]


@pytest.mark.parametrize(("name", "directory", "mint", "config", "page_size"), PROPERTY_CASES,
                         ids=[c[0] for c in PROPERTY_CASES])
def test_resume_equals_fresh_with_memo_property(name, directory, mint, config, page_size):
    cfg = _cfg(config, page_size=page_size)
    data = json.loads((directory / "rpc.json").read_text())
    _fs, fresh_source, fresh = _fresh(directory, cfg, mint)
    total = len(fresh_source.calls)
    checks = memo_seen = 0
    for k in range(1, total + 2):
        for mode in ("budget", "fail"):
            state = CollectionState(mint=mint, config_version=cfg.version)
            if mode == "budget":
                cut_cfg = dataclasses.replace(cfg, time_budget_seconds=float(k))
                _src, first = _timed(state, directory, cfg, float(k))
            else:
                cut_cfg = cfg
                first = collect(state, FixtureRpcSource(directory, failures=[FailAfter(k, RpcUnavailable("x"))]),
                                cfg, FakeClock())
            memo_seen += _assert_memo_matches_oracle(state, data, cfg)
            _assert_memo_only_for_live_unfinished_nodes(state)
            memo_before = dict(state.scan_memo)
            source = FixtureRpcSource(directory)
            result = resume(state, source, cut_cfg, FakeClock())
            # еталон — свіжий прогін того самого конфігу (бюджет відрізняє лише metadata.time_budget_seconds)
            reference = dataclasses.replace(fresh, metadata=dataclasses.replace(
                fresh.metadata, time_budget_seconds=cut_cfg.time_budget_seconds))
            assert _stable(result) == _stable(reference), (k, mode)
            assert result.metadata.transactions_scanned == reference.metadata.transactions_scanned, (k, mode)
            calls = _calls(source)
            for key in memo_before:  # завершене сканування з тією самою межею не повторюється
                owner_cut = next((lv[key.owner] for lv in state.frontier_by_depth.values() if key.owner in lv), None)
                if key.kind == LISTING or owner_cut == key.cutoff.signature:
                    assert _start_call(key) not in calls, (k, mode, key)
            _assert_memo_matches_oracle(state, data, cfg)
            checks += 1
            if first.completeness.status is CompletenessStatus.INCOMPLETE:
                assert set(first.transfers) <= set(reference.transfers), (k, mode)
    assert checks == 2 * (total + 1)
    if name != "basic_nospl":
        assert memo_seen, "property never exercised a non-empty memo"


def _recurring_calls(directory: Path, cfg, mint: str) -> int:
    """Звернення, які повтор робить на повністю зібраному стані (corrupt: пошкоджені транзакції не кешуються)."""
    state, _src, _r = _fresh(directory, cfg, mint)
    source = FixtureRpcSource(directory)
    resume(state, source, cfg, FakeClock())
    return len(source.calls)


def _max_scan_cost(fresh_calls) -> int:
    """Найдорожче сканування одного джерела у свіжому прогоні: найдовша серія сторінок однієї адреси."""
    best = run = 0
    last = None
    for method, params in fresh_calls:
        address = params.get("address") if method == "getSignaturesForAddress" else None
        run = run + 1 if address is not None and address == last else (1 if address else 0)
        last = address
        best = max(best, run, 1)
    return best


@pytest.mark.parametrize(("name", "directory", "mint", "config", "page_size"), PROPERTY_CASES,
                         ids=[c[0] for c in PROPERTY_CASES])
def test_small_budget_sequence_reaches_fresh_result_with_progress_each_time(name, directory, mint, config, page_size):
    # ГАРАНТІЯ ПРОГРЕСУ: бюджет ≥ (звернення, що повторюються завжди) + (вартість одного сканування) →
    # кожен неповний повтор завершує щонайменше одне сканування, а послідовність доходить до результату
    # свіжого прогону (corrupt — до того самого чесного incomplete)
    cfg = _cfg(config, page_size=page_size)
    _fs, fresh_source, _fresh_result = _fresh(directory, cfg, mint)
    budget = float(_recurring_calls(directory, cfg, mint) + _max_scan_cost(fresh_source.calls))
    _s, _src, reference = _fresh(directory, dataclasses.replace(cfg, time_budget_seconds=budget), mint)
    state, calls, runs = _resume_sequence(directory, cfg, mint, budget, limit=len(fresh_source.calls) + 2)
    if reference.completeness.status is CompletenessStatus.COMPLETE:
        assert runs[-1][0].completeness.status is CompletenessStatus.COMPLETE, (name, budget, len(runs))
    final_index = next((i for i, run in enumerate(runs) if _stable(run[0]) == _stable(reference)), None)
    assert final_index is not None, (name, budget, len(runs))
    previous = -1
    for _result, (_scans, total), _n in runs[:final_index]:
        assert total > previous, (name, budget)
        previous = total
    assert final_index < len(fresh_source.calls)  # повторів менше, ніж звернень у свіжому прогоні


# --- 6. stale config_version скидає мемо ----------------------------------------------------------


class _StateSpyClock(FakeClock):
    """Знімок стану при першому зверненні до годинника (до будь-якого кроку збору)."""

    def __init__(self, state: CollectionState) -> None:
        super().__init__()
        self._state, self.first_seen = state, None

    def monotonic(self) -> float:
        if self.first_seen is None:
            self.first_seen = dataclasses.replace(self._state, scan_memo=dict(self._state.scan_memo))
        return super().monotonic()


def test_stale_config_version_clears_memo():
    cfg = _cfg()
    old_cfg = dataclasses.replace(cfg, version=cfg.version + 1)
    state = CollectionState(mint=M, config_version=old_cfg.version)
    _timed(state, BASIC, old_cfg, 6.0)
    assert state.scan_memo, "stale state must carry memo records for the check to mean anything"
    clock = _StateSpyClock(state)
    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, clock)
    assert clock.first_seen.scan_memo == {}
    _fs, fresh_source, fresh = _fresh(BASIC, cfg, M)
    assert source.calls == fresh_source.calls  # нічого зі старого мемо не використано
    assert _stable(result) == _stable(fresh) and result.metadata.resumed is False


# --- 7. Кеш: мемо копіюється структурно й ізольовано ----------------------------------------------


def test_cache_isolation_of_scan_memo():
    cfg = _cfg()
    state = CollectionState(mint=M, config_version=cfg.version)
    _timed(state, BASIC, cfg, 6.0)
    assert state.scan_memo
    snapshot = dict(state.scan_memo)
    cache = ResultCache()
    cache.put_partial(state)
    state.scan_memo.clear()  # мутація збереженого викликачем
    got = cache.get_partial(M)
    assert got.scan_memo == snapshot
    assert got.scan_memo is not cache.get_partial(M).scan_memo
    got.scan_memo.clear()  # мутація отриманої копії (resume її мутує)
    again = cache.get_partial(M)
    assert again.scan_memo == snapshot
    # значення незмінні й діляться (структурна копія), тож копія не залежить від розміру знімків
    for key, record in again.scan_memo.items():
        assert record is snapshot[key]
        assert type(key).__dataclass_params__.frozen and type(record).__dataclass_params__.frozen
        hash(key), hash(record)
    # продовження з кешу == свіжому
    result = resume(again, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert _stable(result) == _stable(_fresh(BASIC, cfg, M)[2])


# --- 8. Список токен-рахунків — раз на власника за життя партиційного стану -----------------------


def test_token_accounts_listing_memoized_once_per_owner_across_resumes(tmp_path):
    # (а) послідовність повторів сервісу з малим бюджетом: кожен власник — щонайбільше одне звернення
    results, source, _cache = _service_until_complete(_cfg(), 3.0)
    assert results[-1].completeness.status is CompletenessStatus.COMPLETE and len(results) > 3
    owners = [p["owner"] for m, p in source.calls if m == "getTokenAccountsByOwner"]
    assert owners and len(owners) == len(set(owners))
    _s, fresh_source, _f = _fresh(BASIC, _cfg(), M)
    assert sorted(owners) == sorted(p["owner"] for m, p in fresh_source.calls if m == "getTokenAccountsByOwner")
    # (б) межа власника змінилась між проходами (пізніше ребро) — список не перезапитується
    directory = _a_funds_p3_variant(tmp_path)
    cfg = _cfg()
    failures = [FailFor(P3, RpcUnavailable("down"), times=1), FailFor(SIG_G_A, RpcUnavailable("down"), times=1)]
    state = CollectionState(mint=M, config_version=cfg.version)
    first_source = FixtureRpcSource(directory, failures=failures)
    collect(state, first_source, cfg, FakeClock())
    second_source = FixtureRpcSource(directory)
    result = resume(state, second_source, cfg, FakeClock())
    listing = [p["owner"] for s in (first_source, second_source) for m, p in s.calls if m == "getTokenAccountsByOwner"]
    assert listing.count(A) == 1
    assert len(listing) == len(set(listing))
    assert result.completeness.status is CompletenessStatus.COMPLETE


# --- Межові: атомарність знімка, бюджет посеред вершини, truncated ---------------------------------


def test_budget_cut_mid_node_keeps_completed_scans_and_drops_unfinished():
    # бюджет 5 с при 1 с/звернення: перелічення (3 звернення), P2 — історія гаманця (2) завершена,
    # список токен-рахунків — уже ні. У мемо — рівно історія гаманця P2; незавершеного немає.
    cfg = _cfg()
    data = RPC
    state = CollectionState(mint=M, config_version=cfg.version)
    source, first = _timed(state, BASIC, cfg, 5.0)
    assert (P2, MissingReason.BUDGET_EXHAUSTED) in {(m.wallet, m.reason) for m in first.completeness.missing}
    assert [(k.kind, k.address) for k in state.scan_memo] == [(WALLET, P2)]
    _assert_memo_matches_oracle(state, data, cfg)
    # бюджет обірвав сканування посеред сторінок (7 с: перша сторінка першого токен-рахунку) — його немає
    state6 = CollectionState(mint=M, config_version=cfg.version)
    source6, _r = _timed(state6, BASIC, cfg, 7.0)
    assert _calls(source6)[-1] == ("getSignaturesForAddress", P2_ACCOUNTS[0], None)
    assert {(k.kind, k.address) for k in state6.scan_memo} == {(WALLET, P2), (LISTING, P2)}


def test_rpc_failure_mid_history_is_not_memoized():
    # збій на другій сторінці історії гаманця P2 (page_size=1): неповне сканування не кладеться,
    # а вже завершені (список і токен-рахунки P2) — кладуться; повтор гортає історію P2 від межі знову
    cfg = _cfg(page_size=1)
    state = CollectionState(mint=M, config_version=cfg.version)
    fresh_calls = _fresh(BASIC, cfg, M)[1].calls
    second_page = next(i for i, (m, p) in enumerate(fresh_calls)
                       if m == "getSignaturesForAddress" and p["address"] == P2 and p["before"] != P2_BUY["first_buy_signature"])
    first = collect(state, _FailOnce(BASIC, at=second_page + 1), cfg, FakeClock())
    assert any(m.wallet == P2 for m in first.completeness.missing)
    p2_kinds = {k.kind for k in state.scan_memo if k.owner == P2}
    assert WALLET not in p2_kinds and {LISTING, TOKEN_ACCOUNT} <= p2_kinds
    _assert_memo_matches_oracle(state, RPC, cfg)
    source = FixtureRpcSource(BASIC)
    result = resume(state, source, cfg, FakeClock())
    assert ("getSignaturesForAddress", P2, P2_BUY["first_buy_signature"]) in _calls(source)
    assert ("getTokenAccountsByOwner", P2, None) not in _calls(source)
    assert _stable(result) == _stable(_fresh(BASIC, cfg, M)[2])


class _FailOnce(FixtureRpcSource):
    """Відмова рівно на `at`-му зверненні (рахунок з 1), далі джерело відпускає."""

    def __init__(self, *args, at: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._at, self._n = at, 0

    def _enter(self, method, params):
        self._n += 1
        super()._enter(method, params)
        if self._n == self._at:
            raise RpcUnavailable("once")


def test_truncated_window_reproduced_from_memo_identically():
    # hub_signature_cap: H — 1197 підписів до межі при cap=300 → signature_cap. Обрізання бюджетом після
    # завершення історії H, але до її транзакцій: повтор бере знімок (truncated) і дає той самий
    # UnexpandedNode (signatures_seen, signatures_truncated), що й свіжий прогін
    cfg = _cfg(HUB_CASES["hub_signature_cap"], page_size=1000)
    _fs, fresh_source, fresh = _fresh(HUB, cfg, HUB_MINT)
    assert any(u.reason is UnexpandedReason.SIGNATURE_CAP for u in fresh.unexpanded)
    hits = 0
    for k in range(1, len(fresh_source.calls) + 1):
        state = CollectionState(mint=HUB_MINT, config_version=cfg.version)
        _timed(state, HUB, cfg, float(k))
        truncated = [k_ for k_, r in state.scan_memo.items() if k_.kind != LISTING and r.truncated]
        if not truncated:
            continue
        hits += 1
        source = FixtureRpcSource(HUB)
        result = resume(state, source, cfg, FakeClock())
        for key in truncated:
            assert _start_call(key) not in _calls(source)
        assert result.unexpanded == fresh.unexpanded
        assert _stable(result) == _stable(fresh)
    assert hits


def test_memo_after_complete_fresh_run_holds_only_listings():
    # чистка: історії розгорнутих вершин не зберігаються (потрібні лише для недорозгорнутих);
    # списки токен-рахунків лишаються на все життя стану (малі: K адрес на власника)
    cfg = _cfg()
    state, _src, result = _fresh(BASIC, cfg, M)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert state.scan_memo and {k.kind for k in state.scan_memo} == {LISTING}
    _assert_memo_matches_oracle(state, RPC, cfg)
