# verifies: FR-001-13, FR-001-16
"""Окремий розмір пачки транзакцій ядра: `rpc.tx_batch_size` (T-052, конфіг версії 2; known-issues §6.6).

Знахідка живого прогону (токен `95DELX…pump`, 5880 транзакцій): `rpc.page_size`=1000 керував і сторінкою
`getSignaturesForAddress`, і розміром пачки `get_transactions` ядра. Адаптер «все або нічого» з клієнтським
пейсером (кошик токенів 30, 12/с) обробляє виклик на 1000 транзакцій 80+ с > бюджету 40 с — і викидає всі вже
отримані під-пачки: нуль прогресу на кожному повторі, resume не допомагає.

Що перевіряється (мережі немає: `FixtureRpcSource`, `FakeClock`, емулятор пейсера `_PacedSource`):
- конфіг: `rpc.tx_batch_size` обов'язковий, ціле 1..1000, у поставленому конфігу 25; версія 2 із записом у
  `config/CHANGELOG.md`, що закінчується `sha256:` канонічного вмісту файла (правило research R-14 фічі 002);
- `page_size` — лише limit сторінки підписів; пачки транзакцій — лише `tx_batch_size`;
- ІНВАРІАНТ: результат свіжого збору не залежить від `tx_batch_size` (1, 2, 7, 25, 1000): ті самі перекази,
  покупці, missing, unexpanded, повнота, `transactions_scanned`, кеш транзакцій, знімки мемо; відрізняється
  лише розбиття `get_transactions` (журнал, `rpc_calls`);
- ПРОГРЕС: під пейсером великий виклик не вміщається в бюджет ніколи (нуль прогресу — задокументована
  знахідка), а пачки по `tx_batch_size` ≤ під-batch адаптера просуваються на кожному повторі й дають результат
  свіжого прогону.
"""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import ConfigError, load_config
from unmask.ingest.model import CompletenessStatus, IngestResult
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcTimeout
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, HUB, CORRUPT = SCENARIOS / "basic", SCENARIOS / "hub", SCENARIOS / "corrupt"
SHIPPED = ROOT / "config" / "ingest.yaml"
CHANGELOG = ROOT / "config" / "CHANGELOG.md"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
RPC = json.loads((BASIC / "rpc.json").read_text())
M = EXPECTED["mint"]
BUY_SIG = {b["wallet"]: b["first_buy_signature"] for b in EXPECTED["buyers"]}
P1, P4 = EXPECTED["wallets"]["P1"], EXPECTED["wallets"]["P4"]
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
HUB_MINT = HUB_EXPECTED["mint"]
HUB_CASES = {c["name"]: c["config"] for c in HUB_EXPECTED["cases"]}
H = HUB_EXPECTED["wallets"]["H"]
HUB_RPC = json.loads((HUB / "rpc.json").read_text())
CORRUPT_MINT = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]["M"]

SIZES = (1, 2, 7, 25, 1000)
VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")


def _cfg(config: dict | None = None, *, page_size: int | None = None, tx_batch_size: int | None = None,
         **overrides):
    cfg = load_config(SHIPPED)
    values = dict(EXPECTED["config"] if config is None else config)
    values.update(overrides)
    cfg = dataclasses.replace(cfg, **values)
    rpc = {}
    if page_size is not None:
        rpc["page_size"] = page_size
    if tx_batch_size is not None:
        rpc["tx_batch_size"] = tx_batch_size
    return dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, **rpc)) if rpc else cfg


def _stable(result: IngestResult) -> tuple:
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _tx_calls(calls) -> list[list[str]]:
    return [params["signatures"] for method, params in calls if method == "getTransaction"]


def _other_calls(calls) -> list:
    return [(method, params) for method, params in calls if method != "getTransaction"]


# --- 1. Конфіг ------------------------------------------------------------------------------------


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "ingest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_config_requires_tx_batch_size_and_rejects_out_of_range(tmp_path):
    shipped = load_config(SHIPPED)
    assert shipped.rpc.tx_batch_size == 25  # == max_batch адаптера: жоден виклик не марнує під-пачок
    assert shipped.rpc.page_size == 1000

    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    del raw["rpc"]["tx_batch_size"]
    with pytest.raises(ConfigError, match=r"rpc\.tx_batch_size"):
        load_config(_write(tmp_path, raw))

    for bad in (0, -1, 1001, 10**6, True, False, 2.5, "25", None, [25]):
        raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
        raw["rpc"]["tx_batch_size"] = bad
        with pytest.raises(ConfigError, match=r"rpc\.tx_batch_size"):
            load_config(_write(tmp_path, raw))

    for good in (1, 2, 7, 25, 999, 1000):
        raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
        raw["rpc"]["tx_batch_size"] = good
        cfg = load_config(_write(tmp_path, raw))
        assert cfg.rpc.tx_batch_size == good
        assert cfg.rpc.page_size == 1000  # поля незалежні

    # невідоме поле поруч (наприклад, друкарська помилка в назві) — теж гучно, з назвою поля
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    raw["rpc"]["tx_batch"] = raw["rpc"].pop("tx_batch_size")
    with pytest.raises(ConfigError, match=r"rpc\.(tx_batch_size|tx_batch)"):
        load_config(_write(tmp_path, raw))
    # на верхньому рівні поле не приймається: воно належить секції rpc
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    raw["tx_batch_size"] = 25
    with pytest.raises(ConfigError, match="tx_batch_size"):
        load_config(_write(tmp_path, raw))

    # IngestConfig в обхід YAML без значення — ядро гучно відмовляє, а не бере тихе умовчання
    bare = dataclasses.replace(shipped, rpc=dataclasses.replace(shipped.rpc, tx_batch_size=None))
    source = FixtureRpcSource(BASIC)
    with pytest.raises(ValueError, match="tx_batch_size"):
        collect(CollectionState(mint=M, config_version=bare.version), source, bare, FakeClock())
    assert source.calls == []


def _canonical_digest(path: Path) -> str:
    """sha256 канонічного вмісту YAML (research R-14 фічі 002): коментарі й форматування не впливають."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _changelog_entries(text: str) -> dict[int, str]:
    """{version: текст запису} із записів `## <version> — <дата>`."""
    entries: dict[int, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = int(line[3:].split()[0])
            entries[current] = ""
        elif current is not None:
            entries[current] += line + "\n"
    return entries


def test_config_version_2_changelog_digest_matches(tmp_path):
    cfg = load_config(SHIPPED)
    assert cfg.version == 2
    entries = _changelog_entries(CHANGELOG.read_text(encoding="utf-8"))
    assert set(entries) >= {1, 2}
    entry = entries[cfg.version]
    lines = [line for line in entry.splitlines() if line.strip()]
    assert lines[-1].startswith("sha256: "), "останній рядок запису — sha256 канонічного вмісту"
    assert lines[-1] == f"sha256: {_canonical_digest(SHIPPED)}"
    assert "tx_batch_size" in entry and "25" in entry and "page_size" in entry
    # дайджест канонічний: коментар і форматування його не змінюють, значення — змінює (тож зміна без нового
    # запису в журналі робить цей тест червоним)
    text = SHIPPED.read_text(encoding="utf-8")
    reformatted = tmp_path / "reformatted.yaml"
    reformatted.write_text("# інший коментар\n" + yaml.safe_dump(yaml.safe_load(text), sort_keys=False), encoding="utf-8")
    assert _canonical_digest(reformatted) == _canonical_digest(SHIPPED)
    changed = tmp_path / "changed.yaml"
    changed.write_text(text.replace("tx_batch_size: 25", "tx_batch_size: 24"), encoding="utf-8")
    assert _canonical_digest(changed) != _canonical_digest(SHIPPED)


# --- 2. page_size проти tx_batch_size ---------------------------------------------------------------


def test_page_size_no_longer_controls_transaction_batches():
    mint_history = [e["signature"] for e in RPC["getSignaturesForAddress"][M]]

    # сторінка підписів по 1, пачка транзакцій 1000: усі 7 транзакцій mint — одним викликом
    source = FixtureRpcSource(BASIC)
    collect(CollectionState(mint=M, config_version=2), source, _cfg(page_size=1, tx_batch_size=1000,
                                                                    first_buyers_n=300), FakeClock())
    pages = [p for method, p in source.calls if method == "getSignaturesForAddress"]
    assert pages and all(p["limit"] == 1 for p in pages)
    batches = _tx_calls(source.calls)
    assert sorted(batches[0]) == sorted(mint_history)  # перелічення покупців: одна пачка на всю історію

    # сторінка підписів 1000, пачка транзакцій 1: кожен виклик — одна транзакція
    source = FixtureRpcSource(BASIC)
    collect(CollectionState(mint=M, config_version=2), source, _cfg(page_size=1000, tx_batch_size=1), FakeClock())
    pages = [p for method, p in source.calls if method == "getSignaturesForAddress"]
    assert pages and all(p["limit"] == 1000 for p in pages)
    assert all(len(b) == 1 for b in _tx_calls(source.calls))

    # funding: хаб H (1200 підписів) при page_size=1000 і tx_batch_size=25 — пачки рівно по 25
    source = FixtureRpcSource(HUB)
    collect(CollectionState(mint=HUB_MINT, config_version=2), source,
            _cfg(HUB_CASES["hub_control"], page_size=1000, tx_batch_size=25), FakeClock())
    sizes = [len(b) for b in _tx_calls(source.calls)]
    assert max(sizes) == 25 and sizes.count(25) >= 40  # ≈ 1200 транзакцій H (частина вже в кеші від інших вершин)
    assert all(p["limit"] == 1000 for method, p in source.calls if method == "getSignaturesForAddress")


def _cases():
    """(назва, сценарій, mint, config, page_size): basic і варіанти конфігу, hub (усі три випадки), corrupt."""
    base = EXPECTED["config"]
    yield "basic", BASIC, M, base, None
    yield "basic_n1", BASIC, M, {**base, "first_buyers_n": 1}, None
    yield "basic_n2_ps1", BASIC, M, {**base, "first_buyers_n": 2}, 1
    yield "basic_n300_d3", BASIC, M, {**base, "first_buyers_n": 300, "funding_depth": 3}, 2
    yield "basic_d1", BASIC, M, {**base, "funding_depth": 1}, None
    yield "basic_nospl", BASIC, M, {**base, "collect_spl_inbound": False}, None
    yield "basic_caps", BASIC, M, {**base, "counterparty_threshold": 1, "max_signatures_per_wallet": 2}, 2
    yield "basic_cap1", BASIC, M, {**base, "max_signatures_per_wallet": 1}, None
    for name, config in HUB_CASES.items():
        yield name, HUB, HUB_MINT, config, None
    yield "hub_degree_ps7", HUB, HUB_MINT, HUB_CASES["hub_high_degree"], 7
    yield "corrupt", CORRUPT, CORRUPT_MINT, {"first_buyers_n": 3, "funding_depth": 2}, None
    yield "corrupt_n300_d3", CORRUPT, CORRUPT_MINT, {"first_buyers_n": 300, "funding_depth": 3}, 1


def _variant_dirs(tmp_path: Path):
    """Варіанти basic із дефектами даних (неповнота з різних причин): пошкоджена купівля P4, відсутня (null)
    купівля P1, відсутня транзакція переказу у вершину."""
    funding_sig = next(s for s in RPC["getTransaction"] if s.startswith("2avhiv"))  # G -> A

    def corrupt_p4(data):
        data["getTransaction"][BUY_SIG[P4]]["meta"] = None

    def null_p1(data):
        data["getTransaction"][BUY_SIG[P1]] = None

    def null_funding(data):
        data["getTransaction"][funding_sig] = None

    for name, mutate in (("v_corrupt_p4", corrupt_p4), ("v_null_p1", null_p1), ("v_null_funding", null_funding)):
        data = copy.deepcopy(RPC)
        mutate(data)
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / "rpc.json").write_text(json.dumps(data))
        yield name, directory


class _Recorder(dict):
    """`scan_memo`, що пам'ятає кожен записаний знімок (чистка прибирає зі словника, не з `records`)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: dict = {}

    def __setitem__(self, key, value) -> None:
        self.records[key] = value
        super().__setitem__(key, value)


def _fresh_snapshot(directory: Path, mint: str, cfg):
    state = CollectionState(mint=mint, config_version=cfg.version, scan_memo=_Recorder())
    source = FixtureRpcSource(directory)
    result = collect(state, source, cfg, FakeClock())
    assert result.metadata.rpc_calls == len(source.calls)
    snapshot = dict(
        stable=_stable(result),
        transactions_scanned=result.metadata.transactions_scanned,
        missing=sorted(state.missing.items(), key=lambda kv: (kv[0][0], kv[0][1].value)),
        tx_cache=sorted(state.tx_cache),
        memo_records=state.scan_memo.records,
        memo_left=dict(state.scan_memo),
        expanded=sorted(state.expanded),
        frontier=state.frontier_by_depth,
        mint_signatures=state.mint_signatures,
    )
    return snapshot, source.calls


def test_result_independent_of_tx_batch_size_property(tmp_path):
    cases = list(_cases())
    cases += [(name, d, M, EXPECTED["config"], None) for name, d in _variant_dirs(tmp_path)]
    cases += [(name + "_n300_d3", d, M, {**EXPECTED["config"], "first_buyers_n": 300, "funding_depth": 3}, 1)
              for name, d in _variant_dirs(tmp_path / "more")]
    statuses = set()
    for name, directory, mint, config, page_size in cases:
        reference = reference_calls = None
        for size in SIZES:
            cfg = _cfg(config, page_size=page_size, tx_batch_size=size)
            snapshot, calls = _fresh_snapshot(directory, mint, cfg)
            batches = _tx_calls(calls)
            assert all(1 <= len(b) <= size for b in batches), (name, size)
            if reference is None:
                reference, reference_calls = snapshot, calls
                assert snapshot["memo_records"] or not cfg.funding_depth, name
                statuses.add(snapshot["stable"][1].status)
                continue
            for key in reference:
                assert snapshot[key] == reference[key], (name, size, key)
            # журнал відрізняється ЛИШЕ розбиттям get_transactions: решта звернень і їхній порядок — ті самі,
            # а пачка 1 запитує рівно потрібне (більша пачка може лише прихопити зайве, але не пропустити)
            assert _other_calls(calls) == _other_calls(reference_calls), (name, size)
            assert {s for b in _tx_calls(reference_calls) for s in b} <= {s for b in batches for s in b}, (name, size)
    assert statuses == {CompletenessStatus.COMPLETE, CompletenessStatus.INCOMPLETE}


def test_call_log_batches_never_exceed_tx_batch_size():
    for name, directory, mint, config, page_size in _cases():
        for size in SIZES:
            source = FixtureRpcSource(directory)
            cfg = _cfg(config, page_size=page_size, tx_batch_size=size)
            collect(CollectionState(mint=mint, config_version=cfg.version), source, cfg, FakeClock())
            sizes = [len(b) for b in _tx_calls(source.calls)]
            assert sizes and all(1 <= s <= size for s in sizes), (name, size, max(sizes, default=0))
            # пачки повні, поки є що дозапитувати: хаб H (понад 1000 підписів у вікні) дає пачку рівно `size`
            if name == "hub_control":
                assert max(sizes) == size, (name, size)
    # поставлений конфіг (25) на тому самому хабі: жодного виклику понад під-batch адаптера
    source = FixtureRpcSource(HUB)
    shipped = dataclasses.replace(load_config(SHIPPED), **HUB_CASES["hub_control"])
    collect(CollectionState(mint=HUB_MINT, config_version=shipped.version), source, shipped, FakeClock())
    assert max(len(b) for b in _tx_calls(source.calls)) == 25


# --- 3. Прогрес під пейсером (відтворення знахідки) ---------------------------------------------------


class _PacedSource:
    """Емулятор `HttpRpcSource` із клієнтським кошиком токенів (contracts/rpc-source.md, «Пейсер»).

    Поверх `FixtureRpcSource`: `get_transactions` іде під-batch'ами по `max_batch`; перед кожним — поповнення
    кошика (`burst` токенів на старті, `rate`/с за `clock`), нестача — пауза `(k - tokens) / rate` (просуває
    `FakeClock`). Пауза, що не вміщається в залишок бюджету, — відмова «все або нічого»: усі вже отримані
    під-batch'і цього виклику викидаються. Відмова емулюється обізнано щодо дедлайну: годинник доходить до
    межі бюджету (так поводиться запит, обрізаний `request_timeout`) і піднімається `RpcTimeout("budget")` —
    ядро класифікує її як `budget_exhausted` і до, і після T-051 (не покладаємось на `RpcBudgetTimeout`).
    Решта методів часу не займають і пейсером не обмежуються."""

    def __init__(self, directory: Path, clock: FakeClock, *, burst: float = 30, rate: float = 12.0,
                 max_batch: int = 25) -> None:
        self._inner = FixtureRpcSource(directory)
        self._clock = clock
        self._burst, self._rate, self._max_batch = burst, rate, max_batch
        self._tokens = float(burst)
        self._at = clock.monotonic()
        self.refused: list[tuple[int, int]] = []  # (розмір виклику, елементів отримано й викинуто)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def calls(self):
        return self._inner.calls

    def _refuse(self, deadline, size: int, discarded: int):
        self.refused.append((size, discarded))
        self._clock.advance(deadline.remaining())
        raise RpcTimeout("budget")

    def get_transactions(self, signatures, *, deadline):
        sent = 0
        for start in range(0, len(signatures), self._max_batch):
            k = len(signatures[start:start + self._max_batch])
            if deadline.expired():
                self._refuse(deadline, len(signatures), sent)
            now = self._clock.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._at) * self._rate)
            self._at = now
            if self._tokens < k:
                wait = (k - self._tokens) / self._rate
                if wait >= deadline.remaining():
                    self._refuse(deadline, len(signatures), sent)
                self._clock.advance(wait)
                self._tokens, self._at = float(k), self._clock.monotonic()
            self._tokens -= k
            sent += k
        return self._inner.get_transactions(signatures, deadline=deadline)

    def get_signatures_for_address(self, address, **kw):
        self._check(kw["deadline"])
        return self._inner.get_signatures_for_address(address, **kw)

    def get_account_info(self, address, *, deadline):
        self._check(deadline)
        return self._inner.get_account_info(address, deadline=deadline)

    def get_token_accounts_by_owner(self, owner, *, deadline):
        self._check(deadline)
        return self._inner.get_token_accounts_by_owner(owner, deadline=deadline)

    @staticmethod
    def _check(deadline) -> None:
        if deadline.expired():
            raise RpcTimeout("budget")


def _service_reference(directory: Path, mint: str, cfg) -> IngestResult:
    """Свіжий прогін сервісу без обмежень швидкості й часу (еталон)."""
    result = IngestService(cfg, FixtureRpcSource(directory), FakeClock()).collect(mint)
    assert isinstance(result, IngestResult)
    return result


def _paced_runs(directory: Path, mint: str, cfg, *, runs: int, **bucket):
    """Послідовні прогони сервісу зі спільним кешем (кожен — новий процес адаптера з повним кошиком і новим
    бюджетом). Повертає [(результат, стан partial після прогону або None, відмови емулятора)]."""
    cache = ResultCache()
    out = []
    for _ in range(runs):
        clock = FakeClock()
        source = _PacedSource(directory, clock, **bucket)
        result = IngestService(cfg, source, clock, cache).collect(mint)
        assert isinstance(result, IngestResult)
        out.append((result, cache.get_partial(mint), source.refused))
        if result.completeness.status is CompletenessStatus.COMPLETE:
            break
    return out


def _progress(state: CollectionState | None) -> tuple:
    if state is None:
        return None
    return (sorted(state.tx_cache), sorted(state.scan_memo, key=repr), sorted(state.transfers), sorted(state.expanded),
            state.mint_signatures)


def test_large_batch_under_rate_limit_makes_no_progress_regression_scenario():
    """ДОКУМЕНТОВАНА ЗНАХІДКА (known-issues §6.6) і її лікування — в одному тесті.

    hub_control: вікно хаба H — 1200 транзакцій. Пейсер 30 + 12/с, бюджет 40 с (поставлений). Виклик на 1000
    транзакцій потребує ≈ (1000 − 30) / 12 ≈ 80,8 с > 40 с: адаптер встигає отримати 20 під-batch'ів (500
    транзакцій) і викидає їх — щоразу. З `tx_batch_size=25` кожен виклик — один під-batch, отримане лишається
    в кеші, повтори завершують збір із результатом свіжого прогону.
    """
    config = HUB_CASES["hub_control"]
    hub_window = {e["signature"] for e in HUB_RPC["getSignaturesForAddress"][H]}

    # 1000 (старе значення, коли пачку задавав page_size): нуль прогресу на кожному повторі
    stuck_cfg = _cfg(config, tx_batch_size=1000)
    assert stuck_cfg.time_budget_seconds == 40
    stuck = _paced_runs(HUB, HUB_MINT, stuck_cfg, runs=6)
    assert len(stuck) == 6 and all(r.completeness.status is CompletenessStatus.INCOMPLETE for r, _s, _f in stuck)
    after_first = _progress(stuck[0][1])
    for result, state, refused in stuck:
        assert refused and refused[0][0] == 1000 and refused[0][1] >= 400  # сотні отриманих — викинуто
        assert len(hub_window - set(state.tx_cache)) >= 1000  # власні транзакції H так і не отримано
        assert H not in state.expanded
    for result, state, _refused in stuck[1:]:
        assert _progress(state) == after_first  # повтор нічого не додав
        assert _stable(result) == _stable(stuck[0][0])

    # поставлене значення (25; без заміни — тож зміна поставленого конфігу теж ламає цей тест): кожен повтор
    # додає транзакції в кеш, збір завершується == свіжий прогін
    cfg = _cfg(config)
    assert cfg.rpc.tx_batch_size == 25
    healthy = _paced_runs(HUB, HUB_MINT, cfg, runs=10)
    assert healthy[-1][0].completeness.status is CompletenessStatus.COMPLETE
    assert 2 <= len(healthy) <= 4  # ≈ 510 транзакцій на прогін, 1200+ потрібно
    cached = [len(state.tx_cache) for _r, state, _f in healthy[:-1]]
    assert all(b > a for a, b in zip([0] + cached, cached))
    assert all(size <= 25 for _r, _s, refused in healthy for size, _d in refused)
    assert _stable(healthy[-1][0]) == _stable(_service_reference(HUB, HUB_MINT, cfg))
    assert _stable(healthy[-1][0]) == _stable(_service_reference(HUB, HUB_MINT, stuck_cfg))

    # basic: усі пачки менші за кошик (30), тож знахідка не проявляється за жодного розміру
    for size in (25, 1000):
        basic_cfg = _cfg(tx_batch_size=size)
        (only,) = _paced_runs(BASIC, M, basic_cfg, runs=3)
        assert only[0].completeness.status is CompletenessStatus.COMPLETE and not only[2]
        assert _stable(only[0]) == _stable(_service_reference(BASIC, M, basic_cfg))


@pytest.mark.parametrize(
    ("name", "directory", "mint", "config", "bucket", "budget"),
    [
        # тісний кошик: 1 токен, 1/с, бюджет 2,5 с — щонайбільше 3 транзакції за прогін
        ("basic", BASIC, M, EXPECTED["config"], {"burst": 1, "rate": 1.0}, 2.5),
        ("basic_n300_d3", BASIC, M, {**EXPECTED["config"], "first_buyers_n": 300, "funding_depth": 3},
         {"burst": 1, "rate": 1.0}, 2.5),
        # поставлений кошик (30, 12/с), бюджет 1 с: ≈ 42 транзакції за прогін, хаб потребує 300
        ("hub_signature_cap", HUB, HUB_MINT, HUB_CASES["hub_signature_cap"], {}, 1.0),
    ],
    ids=["basic", "basic_n300_d3", "hub_signature_cap"],
)
def test_tx_batch_size_one_makes_progress_every_resume_under_small_budget(name, directory, mint, config, bucket,
                                                                          budget):
    # бюджет вміщає менше за один великий виклик, але більше за одну пачку
    cfg = _cfg(config, tx_batch_size=1, time_budget_seconds=budget)
    reference = _service_reference(directory, mint, cfg)
    runs = _paced_runs(directory, mint, cfg, runs=400, **bucket)
    assert runs[-1][0].completeness.status is CompletenessStatus.COMPLETE, (name, len(runs))
    assert len(runs) >= 3, "бюджет мав бути замалим для одного прогону"
    previous = 0
    for result, state, _refused in runs[:-1]:
        assert result.completeness.status is CompletenessStatus.INCOMPLETE
        assert len(state.tx_cache) > previous, (name, previous)  # ≥ 1 пачка за кожен повтор
        previous = len(state.tx_cache)
    assert _stable(runs[-1][0]) == _stable(reference)

    # той самий бюджет і кошик з однією великою пачкою (1000): транзакції не надходять ніколи
    big = dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, tx_batch_size=1000))
    stuck = _paced_runs(directory, mint, big, runs=4, **bucket)
    assert all(r.completeness.status is CompletenessStatus.INCOMPLETE for r, _s, _f in stuck)
    assert all(state.tx_cache == {} for _r, state, _f in stuck) or name == "hub_signature_cap"
    if name == "hub_signature_cap":
        assert all(_progress(state) == _progress(stuck[0][1]) for _r, state, _f in stuck)


def test_resume_after_budget_cut_equals_fresh_for_every_tx_batch_size():
    # resume == fresh лишається при будь-якому розбитті: обрізання бюджетом після k звернень (1 с/звернення)
    for directory, mint, config in ((BASIC, M, EXPECTED["config"]),
                                    (HUB, HUB_MINT, HUB_CASES["hub_high_degree"]),
                                    (CORRUPT, CORRUPT_MINT, {"first_buyers_n": 3, "funding_depth": 2})):
        for size in (1, 7, 1000):
            cfg = _cfg(config, tx_batch_size=size)
            fresh_source = FixtureRpcSource(directory)
            fresh = collect(CollectionState(mint=mint, config_version=cfg.version), fresh_source, cfg, FakeClock())
            total = len(fresh_source.calls)
            # кожне k для коротких журналів; для довгих (хаб при пачці 1 — сотні звернень) — перші 20 і рівномірна
            # вибірка решти з межею журналу
            ks = range(1, total + 2) if total <= 80 else sorted(
                set(range(1, 21)) | set(range(21, total, max(1, total // 20))) | {total, total + 1})
            for k in ks:
                cut = dataclasses.replace(cfg, time_budget_seconds=float(k))
                state = CollectionState(mint=mint, config_version=cfg.version)
                clock = FakeClock(advance_per_call=1.0)
                collect(state, FixtureRpcSource(directory, clock=clock), cut, clock)
                again = resume(state, FixtureRpcSource(directory), cut, FakeClock())
                reference = dataclasses.replace(fresh, metadata=dataclasses.replace(
                    fresh.metadata, time_budget_seconds=float(k)))
                assert _stable(again) == _stable(reference), (directory.name, size, k)
                assert again.metadata.transactions_scanned == fresh.metadata.transactions_scanned
