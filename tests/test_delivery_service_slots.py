# verifies: FR-008-02, FR-008-06, FR-008-09
"""Слот важкого аналізу у `DeliveryService` (T-164; research R-1, R-2).

Дублі сервісів 001/002/003 стоять на межах `collect` / `GraphService.analyze` /
`ClusterService.analyze` з лічильниками викликів; `collect` блокується на `threading.Event`, тож
стан «аналіз іде, слот зайнятий» тримається детерміновано, без `sleep` як синхронізації
(коротке `join(timeout)` лише підтверджує, що потік ДОСІ чекає). Реальні `GraphService` /
`ClusterService` зі справжніми конфігами рахують записані результати; мережі немає.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

from test_delivery_service import _mint_of, _real_services
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.limits import HeavySlots
from unmask.delivery.report import to_json
from unmask.delivery.service import DeliveryService
from unmask.ingest.model import RejectKind, Rejection
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"
SCHEMA_PATH = (ROOT / "specs" / "008-api-protection-ops" / "contracts"
               / "overload-error.schema.json")
SERVICE_PY = ROOT / "src" / "unmask" / "delivery" / "service.py"

WAIT = 10.0  # верхня межа очікування події; тест не чекає її без потреби
RETRY = 37   # навмисно не збігається з умовчанням 60 з конфігу


# --------------------------------------------------------------------------- дублі


class GatedIngest:
    """Дубль збору: записаний `IngestResult` за mint, лічильник, блокування на `Event`.

    `block(mint)` ставить ворота: `collect` сповіщає `entered[mint]` і чекає `gates[mint]`.
    `in_use_seen` — значення `slots.in_use` на вході в `collect` (слот уже мусить бути взято).
    """

    def __init__(self, mapping: dict[str, Path], slots: HeavySlots | None = None) -> None:
        self._mapping = mapping
        self._slots = slots
        self.calls: list[str] = []
        self.in_use_seen: list[int] = []
        self.gates: dict[str, threading.Event] = {}
        self.entered: dict[str, threading.Event] = {}
        self.raise_for: dict[str, BaseException] = {}
        self.reject_for: dict[str, Rejection] = {}
        self._lock = threading.Lock()

    def block(self, mint: str) -> threading.Event:
        self.gates[mint] = threading.Event()
        self.entered[mint] = threading.Event()
        return self.gates[mint]

    def collect(self, mint: str):
        with self._lock:
            self.calls.append(mint)
            if self._slots is not None:
                self.in_use_seen.append(self._slots.in_use)
        if mint in self.entered:
            self.entered[mint].set()
            assert self.gates[mint].wait(WAIT), "gate never opened"
        if mint in self.raise_for:
            raise self.raise_for[mint]
        if mint in self.reject_for:
            return self.reject_for[mint]
        data = json.loads(self._mapping[mint].read_text(encoding="utf-8"))
        return from_dict(data)


class CountingGraph:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def analyze(self, outcome):
        self.calls += 1
        return self._inner.analyze(outcome)


class CountingClusters:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def analyze(self, graph_result, outcome):
        self.calls += 1
        return self._inner.analyze(graph_result, outcome)


class Rig:
    """Сервіс + дублі + слоти; `mints` — валідні адреси записаних токенів."""

    def __init__(self, capacity: int | None = 1, retry: int = RETRY,
                 labels: tuple[str, ...] = ("ins1", "ins2", "ins4", "cln1")) -> None:
        self.mints: dict[str, str] = {}
        files: dict[str, Path] = {}
        for label in labels:
            mint, path = _mint_of(label)
            self.mints[label] = mint
            files[mint] = path
        self.slots = HeavySlots(capacity, retry) if capacity is not None else None
        self.ingest = GatedIngest(files, self.slots)
        graph, clusters = _real_services()
        self.graph = CountingGraph(graph)
        self.clusters = CountingClusters(clusters)
        self.cache = DeliveryCache()
        self.service = DeliveryService(self.ingest, self.graph, self.clusters, self.cache,
                                       slots=self.slots)

    def mint(self, label: str) -> str:
        return self.mints[label]

    def background(self, label: str, *, progress=None):
        """Запустити `analyze` у потоці; повертає (потік, коробка результату)."""
        mint = self.mints[label]
        box: dict = {}

        def run() -> None:
            try:
                box["doc"] = self.service.analyze(mint, progress=progress)
            except BaseException as exc:  # noqa: BLE001 - тест перевіряє і BaseException
                box["exc"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, box


@pytest.fixture
def cleanup():
    """Відкрити всі ворота й дочекатися потоків, навіть якщо тест упав."""
    rigs: list[Rig] = []
    threads: list[threading.Thread] = []
    yield rigs, threads
    for rig in rigs:
        for gate in rig.ingest.gates.values():
            gate.set()
    for t in threads:
        t.join(WAIT)


def _hold(rig: Rig, label: str, cleanup_threads: list[threading.Thread]):
    """Власник тримає слот: потік блокується в `collect` тримаючи слот."""
    gate = rig.ingest.block(rig.mint(label))
    thread, box = rig.background(label)
    cleanup_threads.append(thread)
    assert rig.ingest.entered[rig.mint(label)].wait(WAIT)
    return gate, thread, box


def _finish(gate: threading.Event, thread: threading.Thread, box: dict) -> dict:
    gate.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert "exc" not in box, box.get("exc")
    return box["doc"]


def _is_busy(doc: dict) -> bool:
    return doc.get("error", None) is not None and doc["error"].get("kind") == "busy"


# --------------------------------------------------------------------------- рядок T-164


def test_cache_hit_served_while_all_slots_busy(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    first = rig.service.analyze(rig.mint("ins1"))
    assert rig.slots.in_use == 0
    gate, thread, box = _hold(rig, "ins2", threads)
    assert rig.slots.in_use == 1
    calls_before = list(rig.ingest.calls)
    hit = rig.service.analyze(rig.mint("ins1"))
    assert hit == first and not _is_busy(hit)
    assert rig.ingest.calls == calls_before          # кеш-хіт не ходив у збір
    assert rig.slots.in_use == 1
    _finish(gate, thread, box)
    assert rig.slots.in_use == 0


def test_join_inflight_served_while_all_slots_busy(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    joiner, joiner_box = rig.background("ins1")
    threads.append(joiner)
    joiner.join(0.3)
    assert joiner.is_alive(), "joiner must wait for the owner, not be answered busy"
    assert rig.slots.in_use == 1                     # приєднання не взяло слот
    owner_doc = _finish(gate, owner, owner_box)
    joiner.join(WAIT)
    assert "exc" not in joiner_box, joiner_box.get("exc")
    assert joiner_box["doc"] == owner_doc
    assert not _is_busy(joiner_box["doc"]) and "risk_score" in joiner_box["doc"]
    assert rig.ingest.calls == [rig.mint("ins1")]    # один збір на двох
    assert rig.slots.in_use == 0


def test_new_cold_mint_gets_busy_doc_without_ingest_call_or_progress(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    graph_before, clusters_before = rig.graph.calls, rig.clusters.calls
    progress: list[str] = []
    doc = rig.service.analyze(rig.mint("ins2"), progress=progress.append)
    assert doc == {
        "mint": rig.mint("ins2"),
        "error": {
            "kind": "busy",
            "detail": f"too many analyses in progress, retry after {RETRY} s",
            "retry_after_seconds": RETRY,
        },
    }
    assert progress == []                            # busy-документ ДО будь-якого progress
    assert rig.ingest.calls == [rig.mint("ins1")]    # збір для відмовленого mint не викликався
    assert (rig.graph.calls, rig.clusters.calls) == (graph_before, clusters_before)
    assert rig.slots.in_use == 1                     # відмова не змінила лічильник
    _finish(gate, owner, owner_box)


def test_busy_is_not_cached(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    busy = rig.service.analyze(rig.mint("ins2"))
    assert _is_busy(busy)
    assert rig.cache.get(rig.mint("ins2")) is None
    assert set(rig.cache._inflight) == {rig.mint("ins1")}   # busy не залишив запису in-flight
    _finish(gate, owner, owner_box)
    assert rig.cache._inflight == {}
    again = rig.service.analyze(rig.mint("ins2"))        # слот вільний: mint аналізується
    assert not _is_busy(again) and again["mint"] == rig.mint("ins2") and "risk_score" in again
    assert rig.ingest.calls == [rig.mint("ins1"), rig.mint("ins2")]
    assert rig.cache.get(rig.mint("ins2")) == again


@pytest.mark.parametrize("bad", ["!!!", "", "abc", "0" * 44, "1" * 50, " ", "O0Il", "x" * 300])
def test_invalid_address_never_takes_slot(bad: str, cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    rig.ingest.reject_for[bad] = Rejection(kind=RejectKind.INVALID_ADDRESS, mint=bad,
                                           detail="bad base58")
    baseline = Rig(capacity=None)
    baseline.ingest.reject_for[bad] = rig.ingest.reject_for[bad]
    expected = baseline.service.analyze(bad)
    gate, owner, owner_box = _hold(rig, "ins1", threads)   # єдиний слот зайнятий
    doc = rig.service.analyze(bad)
    assert doc == expected
    assert doc["error"]["kind"] == "invalid_address" and not _is_busy(doc)
    assert rig.slots.in_use == 1
    assert bad in rig.ingest.calls                          # старий шлях ядра, як без слотів
    _finish(gate, owner, owner_box)


def test_non_string_mint_never_takes_slot(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    doc = rig.service.analyze(123)  # type: ignore[arg-type]
    assert doc == {"mint": "", "error": {"kind": "invalid_address", "detail": "mint must be a string"}}
    _finish(gate, owner, owner_box)


def test_slot_released_on_success_rejection_incomplete_and_exception() -> None:
    rig = Rig(capacity=1, labels=("ins1", "cln1", "ins2", "ins4"))
    # успіх
    ok = rig.service.analyze(rig.mint("ins1"))
    assert "risk_score" in ok and rig.slots.in_use == 0
    # відмова ядра (валідна адреса, токен не знайдено)
    rig.ingest.reject_for[rig.mint("ins2")] = Rejection(
        kind=RejectKind.TOKEN_NOT_FOUND, mint=rig.mint("ins2"), detail="no such token")
    rej = rig.service.analyze(rig.mint("ins2"))
    assert rej["error"]["kind"] == "token_not_found" and rig.slots.in_use == 0
    # неповний збір -> документ insufficient_data
    inc = rig.service.analyze(rig.mint("cln1"))
    assert inc["band"] == "insufficient_data" and rig.slots.in_use == 0
    # виняток у збиранні
    rig.ingest.raise_for[rig.mint("ins4")] = RuntimeError("rpc down")
    with pytest.raises(RuntimeError, match="rpc down"):
        rig.service.analyze(rig.mint("ins4"))
    assert rig.slots.in_use == 0
    # слот був узятий саме на час збору (інакше in_use == 0 було б порожнім твердженням)
    assert rig.ingest.in_use_seen == [1, 1, 1, 1]


def test_slot_released_on_exception_in_graph_clusters_and_progress() -> None:
    rig = Rig(capacity=1)

    def boom(*_a, **_k):
        raise ValueError("stage failed")

    rig.graph.analyze = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        rig.service.analyze(rig.mint("ins1"))
    assert rig.slots.in_use == 0
    rig2 = Rig(capacity=1)
    rig2.clusters.analyze = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        rig2.service.analyze(rig2.mint("ins1"))
    assert rig2.slots.in_use == 0

    def bad_progress(_text: str) -> None:
        raise OSError("transport died")

    rig3 = Rig(capacity=1)
    with pytest.raises(OSError):
        rig3.service.analyze(rig3.mint("ins1"), progress=bad_progress)
    assert rig3.slots.in_use == 0


def test_busy_doc_validates_overload_schema_and_has_no_result_fields(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    doc = rig.service.analyze(rig.mint("ins2"))
    _finish(gate, owner, owner_box)
    assert set(doc) == {"mint", "error"} and set(doc["error"]) == {"kind", "detail", "retry_after_seconds"}
    text = to_json(doc)
    for forbidden in ("risk_score", "band", "clusters", "schema_version", "provenance"):
        assert forbidden not in doc and f'"{forbidden}"' not in text
    assert type(doc["error"]["retry_after_seconds"]) is int
    if Draft202012Validator is not None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(doc)


def test_without_slots_behaviour_is_byte_identical() -> None:
    """Ті самі фікстури: `slots=None` (поведінка 004) і вільні слоти дають однакові байти."""
    labels = ("ins1", "ins4", "cln1")
    plain = Rig(capacity=None, labels=labels)
    slotted = Rig(capacity=3, labels=labels)
    for rig in (plain, slotted):
        rig.ingest.reject_for["!!!"] = Rejection(kind=RejectKind.INVALID_ADDRESS, mint="!!!",
                                                 detail="bad base58")
        rig.ingest.reject_for[_mint_of("ins2")[0]] = Rejection(
            kind=RejectKind.TOKEN_NOT_FOUND, mint=_mint_of("ins2")[0], detail="no such token")
    keys = [plain.mint(label) for label in labels] + ["!!!", _mint_of("ins2")[0]]
    for mint in keys:
        a, b = plain.service.analyze(mint), slotted.service.analyze(mint)
        assert to_json(a) == to_json(b), mint
    # і повторний (кеш) прохід — теж
    for mint in keys:
        assert to_json(plain.service.analyze(mint)) == to_json(slotted.service.analyze(mint))
    assert plain.ingest.calls == slotted.ingest.calls
    assert slotted.slots.in_use == 0


# --------------------------------------------------------------------------- власні


@pytest.mark.parametrize("round_no", range(15))
def test_race_two_cold_requests_for_one_slot_exactly_one_wins(round_no: int, cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gates = {label: rig.ingest.block(rig.mint(label)) for label in ("ins1", "ins2")}
    barrier = threading.Barrier(2)
    boxes: dict[str, dict] = {"ins1": {}, "ins2": {}}

    def run(label: str) -> None:
        barrier.wait(WAIT)
        try:
            boxes[label]["doc"] = rig.service.analyze(rig.mint(label))
        except BaseException as exc:  # noqa: BLE001
            boxes[label]["exc"] = exc

    ts = [threading.Thread(target=run, args=(label,), daemon=True) for label in boxes]
    threads.extend(ts)
    for t in ts:
        t.start()
    # переможець блокується в collect; програвший повертається одразу з busy
    deadline_hit = False
    for _ in range(int(WAIT * 1000)):
        done = [label for label in boxes if boxes[label]]
        if done:
            deadline_hit = True
            break
        threading.Event().wait(0.001)
    assert deadline_hit, "the loser never returned"
    (loser,) = [label for label in boxes if boxes[label]]
    winner = "ins1" if loser == "ins2" else "ins2"
    assert "exc" not in boxes[loser] and _is_busy(boxes[loser]["doc"])
    assert rig.ingest.entered[rig.mint(winner)].wait(WAIT)
    assert rig.ingest.calls == [rig.mint(winner)]          # у збір пішов рівно один
    assert rig.slots.in_use == 1
    gates[winner].set()
    for t in ts:
        t.join(WAIT)
    assert not _is_busy(boxes[winner]["doc"]) and "risk_score" in boxes[winner]["doc"]
    assert rig.slots.in_use == 0


def test_capacity_two_admits_two_owners_and_refuses_third(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=2)
    rigs.append(rig)
    gate1, t1, b1 = _hold(rig, "ins1", threads)
    gate2, t2, b2 = _hold(rig, "ins2", threads)
    assert rig.slots.in_use == 2
    refused = rig.service.analyze(rig.mint("ins4"))
    assert _is_busy(refused) and rig.mint("ins4") not in rig.ingest.calls
    assert rig.slots.in_use == 2
    _finish(gate1, t1, b1)                                  # звільнився один слот
    assert rig.slots.in_use == 1
    admitted = rig.service.analyze(rig.mint("ins4"))
    assert not _is_busy(admitted) and "risk_score" in admitted
    _finish(gate2, t2, b2)
    assert rig.slots.in_use == 0


def test_exception_in_build_does_not_leave_slot_for_next_cold_mint() -> None:
    rig = Rig(capacity=1)
    rig.ingest.raise_for[rig.mint("ins1")] = RuntimeError("rpc down")
    for _ in range(3):                                      # без витоку: можна повторювати
        with pytest.raises(RuntimeError):
            rig.service.analyze(rig.mint("ins1"))
        assert rig.slots.in_use == 0
    nxt = rig.service.analyze(rig.mint("ins2"))
    assert not _is_busy(nxt) and "risk_score" in nxt
    assert rig.cache._inflight == {}


def test_keyboard_interrupt_in_build_releases_slot() -> None:
    rig = Rig(capacity=1)
    rig.ingest.raise_for[rig.mint("ins1")] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        rig.service.analyze(rig.mint("ins1"))
    assert rig.slots.in_use == 0
    assert rig.cache._inflight == {} and rig.cache.get(rig.mint("ins1")) is None
    rig.ingest.raise_for.clear()
    again = rig.service.analyze(rig.mint("ins1"))           # слот не загублено, mint не отруєно
    assert not _is_busy(again) and "risk_score" in again


def test_system_exit_in_build_releases_slot() -> None:
    rig = Rig(capacity=1)
    rig.ingest.raise_for[rig.mint("ins1")] = SystemExit(3)
    with pytest.raises(SystemExit):
        rig.service.analyze(rig.mint("ins1"))
    assert rig.slots.in_use == 0


@pytest.mark.parametrize("retry", [1, 7, 123])
def test_retry_after_seconds_comes_from_heavy_slots(retry: int, cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1, retry=retry)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    doc = rig.service.analyze(rig.mint("ins2"))
    _finish(gate, owner, owner_box)
    assert doc["error"]["retry_after_seconds"] == retry
    assert doc["error"]["detail"] == f"too many analyses in progress, retry after {retry} s"


def test_busy_reaches_joiners_of_a_refused_owner_and_is_not_sticky(cleanup) -> None:
    """Відмовлений власник не лишає отруєного запису: наступний запит після звільнення проходить."""
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    for _ in range(5):                                      # багато відмов поспіль
        assert _is_busy(rig.service.analyze(rig.mint("ins2")))
    assert rig.slots.in_use == 1 and rig.cache._inflight.keys() == {rig.mint("ins1")}
    _finish(gate, owner, owner_box)
    ok = rig.service.analyze(rig.mint("ins2"))
    assert not _is_busy(ok) and "risk_score" in ok


def test_busy_doc_for_each_refused_mint_names_that_mint(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    a = rig.service.analyze(rig.mint("ins2"))
    b = rig.service.analyze(rig.mint("ins4"))
    _finish(gate, owner, owner_box)
    assert a["mint"] == rig.mint("ins2") and b["mint"] == rig.mint("ins4")


def test_needs_analysis_valid_and_uncached_only(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    svc = rig.service
    assert svc.needs_analysis(rig.mint("ins1")) is True
    for bad in ("!!!", "", "abc", "0" * 44, 123, None, b"x"):
        assert svc.needs_analysis(bad) is False             # type: ignore[arg-type]
    gate, owner, owner_box = _hold(rig, "ins1", threads)
    assert svc.needs_analysis(rig.mint("ins1")) is True     # in-flight ще не в кеші
    assert svc.needs_analysis(rig.mint("ins2")) is True     # слоти зайняті, але формат валідний
    assert rig.slots.in_use == 1                            # запит не бере слот
    assert rig.ingest.calls == [rig.mint("ins1")]           # і не ходить у збір
    _finish(gate, owner, owner_box)
    assert svc.needs_analysis(rig.mint("ins1")) is False    # тепер у кеші
    assert svc.needs_analysis(rig.mint("ins2")) is True
    assert rig.slots.in_use == 0


def test_needs_analysis_works_without_slots() -> None:
    rig = Rig(capacity=None)
    assert rig.service.needs_analysis(rig.mint("ins1")) is True
    rig.service.analyze(rig.mint("ins1"))
    assert rig.service.needs_analysis(rig.mint("ins1")) is False


def test_slots_none_never_busy_with_many_parallel_cold_mints(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=None)
    rigs.append(rig)
    labels = ("ins1", "ins2", "ins4", "cln1")
    gates = [rig.ingest.block(rig.mint(label)) for label in labels]
    started = [rig.background(label) for label in labels]
    threads.extend(t for t, _ in started)
    for label in labels:
        assert rig.ingest.entered[rig.mint(label)].wait(WAIT)   # усі чотири одночасно в збиранні
    for gate in gates:
        gate.set()
    for t, box in started:
        t.join(WAIT)
        assert not _is_busy(box["doc"])


def test_cache_property_and_constructor_signature_unchanged() -> None:
    rig = Rig(capacity=None)
    assert rig.service.cache is rig.cache
    graph, clusters = _real_services()
    # старий виклик з 4 позиційними аргументами працює; slots — лише іменований з умовчанням None
    DeliveryService(rig.ingest, graph, clusters, DeliveryCache())
    DeliveryService(rig.ingest, graph, clusters)


def test_with_lease_directly_follows_none_check_in_guarded_build() -> None:
    """Вимога ревʼю T-163: між перевіркою `None` і `with lease:` нічого, що може кинути."""
    tree = ast.parse(SERVICE_PY.read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_guarded_build")
    body = func.body
    idx = next(i for i, st in enumerate(body)
               if isinstance(st, ast.If) and isinstance(st.test, ast.Compare)
               and isinstance(st.test.ops[0], ast.Is)
               and isinstance(st.test.comparators[0], ast.Constant)
               and st.test.comparators[0].value is None
               and isinstance(st.test.left, ast.Name) and st.test.left.id == "lease")
    none_branch = body[idx]
    assert none_branch.orelse == []
    assert isinstance(none_branch.body[-1], ast.Raise)
    nxt = body[idx + 1]
    assert isinstance(nxt, ast.With) and len(nxt.items) == 1
    ctx = nxt.items[0].context_expr
    assert isinstance(ctx, ast.Name) and ctx.id == "lease"
    # progress допускається лише всередині `with` (через `_build`), не в гілці `None`
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "progress"
                   for n in ast.walk(none_branch))
    assert len(body) == idx + 2                             # після `with` більше нічого
