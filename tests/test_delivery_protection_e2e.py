# verifies: FR-008-02, FR-008-09, FR-008-14
"""Наскрізний прогін захисту (T-173; SC-002, SC-003, SC-006; FR-008-14).

Справжні `DeliveryService` (записані фікстури `tests/fixtures/real/*.json`, справжні `GraphService` /
`ClusterService`), `handle_get` для HTTP і `run_polling` на `JournalTransport` для бота. Один `Protection`
(`build_protection`) на всі канали; годинник — `ManualClock`. Межі беруться з `config/protection.yaml`
через `load_protection_config`; лише ліміти запитів і бота підняті, щоб тести не впирались у них випадково.

Синхронізація — лише `threading.Event` / `threading.Barrier` з таймаутом `WAIT`, без `sleep`. Мережі немає
(гард `conftest`). Потоки під час збирання очікуваних станів детерміновані: відмови й приєднання не чекають
на таймер.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from test_delivery_service import RecordedIngest, _real_services
from test_delivery_service_slots import WAIT, GatedIngest
from unmask.delivery.bot import JournalTransport, run_polling
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.config import load_delivery_config, load_protection_config
from unmask.delivery.http import handle_get
from unmask.delivery.limits import build_protection
from unmask.delivery.report import to_json
from unmask.delivery.service import DeliveryService
from unmask.ingest.model import RejectKind, Rejection
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"
PATH = "/api/token/"
PEER = "203.0.113.9"
RESULT_FIELDS = {"risk_score", "band", "clusters", "band_reasons", "provenance", "evidence"}
BUSY_BOT_TEXT_TEMPLATE = "service is busy with other analyses — try again in ~{} s"
RATE_BOT_TEXT_TEMPLATE = "too many checks — try again in {} s"


# --------------------------------------------------------------------------- дублі та хелпери


class ManualClock:
    """Керований годинник: `now` змінює лише `advance`; `time.*` тут не використовується."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class BarrierIngest:
    """Дубль збору: для `armed` mint'ів стає у `Barrier` (сигнал «слот зайнятий»), далі чекає `release`.

    Незаармовані mint'и збираються одразу. `calls` — журнал викликів збору, `in_use_seen` — значення
    `slots.in_use` на вході в кожен збір (слот мусить бути взятий до збору).
    """

    def __init__(self, files: dict[str, Path], slots, parties: int) -> None:
        self._files = files
        self._slots = slots
        self.calls: list[str] = []
        self.in_use_seen: list[int] = []
        self.armed: set[str] = set()
        self.entered = threading.Barrier(parties)
        self.release = threading.Event()
        self._lock = threading.Lock()

    def collect(self, mint: str):
        with self._lock:
            self.calls.append(mint)
            if self._slots is not None:
                self.in_use_seen.append(self._slots.in_use)
        if mint in self.armed:
            self.entered.wait(WAIT)
            if not self.release.wait(WAIT):
                raise RuntimeError("release never opened")
        data = json.loads(self._files[mint].read_text(encoding="utf-8"))
        return from_dict(data)


class CountingCache(DeliveryCache):
    """Кеш із подією: `joined` спрацьовує, коли другий виклик `single_flight` для `watch` дійшов до
    очікування в польоті (власник уже в single_flight). Так приєднання доводиться подією, а не таймером."""

    def __init__(self, watch: str) -> None:
        super().__init__()
        self._watch = watch
        self._calls = 0
        self._calls_lock = threading.Lock()
        self.joined = threading.Event()

    def single_flight(self, mint, build):
        if mint == self._watch:
            with self._calls_lock:
                self._calls += 1
                if self._calls >= 2:
                    self.joined.set()
        return super().single_flight(mint, build)


def _tokens() -> dict[str, tuple[str, Path]]:
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    return {t["label"]: (t["mint"], REAL / t["file"]) for t in manifest["tokens"]}


def _cfg(**overrides):
    """`config/protection.yaml` без змін; поля з `overrides` — лише в пам'яті тесту (config/ не чіпаємо)."""
    base = load_protection_config(ROOT / "config" / "protection.yaml")
    defaults = {"api_requests_per_window": 1000, "bot_checks_per_window": 1000}
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


def _protection(clock: ManualClock | None = None, **overrides):
    return build_protection(_cfg(**overrides), clock=clock if clock is not None else ManualClock())


def _update(uid: int, text: str, *, chat: int = 7, frm: int = 42) -> dict:
    return {"update_id": uid, "message": {"message_id": uid, "text": text,
                                          "chat": {"id": chat}, "from": {"id": frm}}}


def sync_spawn(fn) -> None:
    fn()


class Threads:
    """Потоки тесту: запуск і гарантоване дожидання, навіть коли тест упав."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self.results: dict[str, tuple] = {}

    def run(self, name: str, fn, *args) -> threading.Thread:
        def target() -> None:
            self.results[name] = fn(*args)

        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def join_all(self) -> None:
        for thread in self.threads:
            thread.join(WAIT)
            assert not thread.is_alive(), "worker thread hung"


# --------------------------------------------------------------------------- рядок T-173: SC-002


def test_n_plus_one_cold_exactly_one_503() -> None:
    tokens = _tokens()
    p = _protection()
    assert p.slots.capacity == 3 and p.slots.retry_after_seconds == 60  # N з config/protection.yaml
    files = {mint: path for mint, path in tokens.values()}
    ingest = BarrierIngest(files, p.slots, parties=4)  # 3 власники + тест
    graph, clusters = _real_services()
    cache = CountingCache(watch=tokens["ins1"][0])
    service = DeliveryService(ingest, graph, clusters, cache, slots=p.slots)
    mint = {label: tokens[label][0] for label in tokens}

    warm = handle_get(PATH + mint["cln2"], PEER, None, service, p)  # кеш-хіт пізніше
    assert warm[0] == 200 and p.slots.in_use == 0

    ingest.armed = {mint["ins1"], mint["ins2"], mint["ins4"]}
    threads = Threads()
    try:
        for label in ("ins1", "ins2", "ins4"):
            threads.run(label, handle_get, PATH + mint[label], PEER, None, service, p)
        ingest.entered.wait(WAIT)                # 3 власники всередині збору
        saturated = p.slots.in_use               # очікуємо 3; перевіряється після SC-002

        joiner = threads.run("join-ins1", handle_get, PATH + mint["ins1"], PEER, None, service, p)
        assert cache.joined.wait(WAIT), "joiner never reached single-flight (not a join)"

        cold = threads.run("cold-cln1", handle_get, PATH + mint["cln1"], PEER, None, service, p)
        cold.join(WAIT)                          # холодна відмова не блокується: дочекатись ДО release
        assert not cold.is_alive(), "cold request blocked instead of 503"

        hit = handle_get(PATH + mint["cln2"], PEER, None, service, p)          # кеш-хіт під насиченням
        health = handle_get("/healthz", PEER, None, service, p)                # проба живості
        assert hit == warm
        assert health[0] == 200

        ingest.release.set()
        for t in [*threads.threads]:
            t.join(WAIT)
            assert not t.is_alive(), "owner or joiner hung"
    finally:
        ingest.release.set()
        threads.join_all()

    results = threads.results
    # SC-002 (першим): рівно один 503 серед N+1 різних mint; решта 200
    statuses = {name: results[name][0] for name in ("ins1", "ins2", "ins4", "cold-cln1")}
    assert [name for name, s in statuses.items() if s == 503] == ["cold-cln1"]
    assert statuses["ins1"] == statuses["ins2"] == statuses["ins4"] == 200
    cold_status, cold_doc, cold_headers = results["cold-cln1"]
    assert cold_doc["error"]["kind"] == "busy"
    assert cold_headers == [("Retry-After", "60")]
    assert not (RESULT_FIELDS & set(cold_doc))
    assert saturated == 3                        # насичення справді було під час холодного запиту
    # приєднаний до in-flight: 200, без другого збору того самого mint
    assert results["join-ins1"][0] == 200
    assert ingest.calls.count(mint["ins1"]) == 1
    assert mint["cln1"] not in ingest.calls
    assert p.slots.in_use == 0


# --------------------------------------------------------------------------- SC-003


def test_no_orphan_slots_after_mixed_outcomes() -> None:
    tokens = _tokens()
    p = _protection()
    mint = {label: tokens[label][0] for label in tokens}
    files = {m: path for m, path in tokens.values()}

    class _Abort(BaseException):
        """Імітація `BaseException` (наприклад, KeyboardInterrupt) всередині збору."""

    ingest = GatedIngest(files, p.slots)
    ingest.reject_for[mint["ins2"]] = Rejection(kind=RejectKind.INVALID_ADDRESS,
                                                mint=mint["ins2"], detail="bad base58")
    ingest.raise_for[mint["ins4"]] = RuntimeError("defect")
    ingest.raise_for[mint["ins3"]] = TimeoutError("collect timed out")
    ingest.raise_for[mint["cln2"]] = _Abort()
    graph, clusters = _real_services()
    service = DeliveryService(ingest, graph, clusters, DeliveryCache(), slots=p.slots)

    doc = service.analyze(mint["ins1"])                      # успіх
    assert doc.get("error") is None and doc["mint"] == mint["ins1"]
    assert p.slots.in_use == 0 and ingest.in_use_seen[-1] == 1

    doc = service.analyze(mint["ins2"])                      # відмова збору -> документ-помилка
    assert doc["error"]["kind"] == "invalid_address"
    assert p.slots.in_use == 0 and ingest.in_use_seen[-1] == 1

    with pytest.raises(RuntimeError, match="defect"):        # виняток
        service.analyze(mint["ins4"])
    assert p.slots.in_use == 0

    with pytest.raises(TimeoutError):                        # таймаут
        service.analyze(mint["ins3"])
    assert p.slots.in_use == 0

    with pytest.raises(_Abort):                              # BaseException
        service.analyze(mint["cln2"])
    assert p.slots.in_use == 0

    doc = service.analyze(mint["cln1"])                      # неповний збір -> insufficient_data
    assert doc["band"] == "insufficient_data"
    assert p.slots.in_use == 0

    assert ingest.calls == [mint["ins1"], mint["ins2"], mint["ins4"], mint["ins3"], mint["cln2"],
                            mint["cln1"]]
    leases = [p.slots.try_acquire() for _ in range(p.slots.capacity)]
    assert all(lease is not None for lease in leases) and p.slots.try_acquire() is None
    for lease in leases:
        lease.release()
    assert p.slots.in_use == 0


# --------------------------------------------------------------------------- спільний слот HTTP і бота


def test_http_busy_is_shared_with_bot() -> None:
    tokens = _tokens()
    p = _protection()
    mint = {label: tokens[label][0] for label in tokens}
    files = {m: path for m, path in tokens.values()}
    ingest = BarrierIngest(files, p.slots, parties=4)
    graph, clusters = _real_services()
    service = DeliveryService(ingest, graph, clusters, DeliveryCache(), slots=p.slots)
    ingest.armed = {mint["ins1"], mint["ins2"], mint["ins4"]}
    evidence_limit = load_delivery_config(ROOT / "config" / "delivery.yaml").evidence_preview_limit

    threads = Threads()
    try:
        for label in ("ins1", "ins2", "ins4"):  # HTTP займає всі слоти
            threads.run(label, handle_get, PATH + mint[label], PEER, None, service, p)
        ingest.entered.wait(WAIT)
        assert p.slots.in_use == 3

        status, doc, headers = handle_get(PATH + mint["cln1"], PEER, None, service, p)
        assert status == 503 and doc["error"]["kind"] == "busy"
        assert headers == [("Retry-After", "60")]

        transport = JournalTransport([_update(1, f"/check {mint['cln1']}")])
        run_polling(transport, service, evidence_limit=evidence_limit, stop_after=1,
                    gate=p.users, spawn=sync_spawn)     # бот бере той самий сервіс і той самий Protection
        assert transport.calls == [
            ("send_message", (7, BUSY_BOT_TEXT_TEMPLATE.format(p.slots.retry_after_seconds)),
             {"reply_markup": None}),
        ]
        assert mint["cln1"] not in ingest.calls            # відмова до збору: жодного аналізу
        assert p.slots.in_use == 3                         # бот не звільнив і не взяв чужий слот
    finally:
        ingest.release.set()
        threads.join_all()
    assert p.slots.in_use == 0


# --------------------------------------------------------------------------- SC-006


def test_success_bytes_identical_with_and_without_protection() -> None:
    tokens = _tokens()
    graph, clusters = _real_services()
    evidence_limit = load_delivery_config(ROOT / "config" / "delivery.yaml").evidence_preview_limit
    checked = 0
    for label, (mint, path) in tokens.items():
        files = {mint: path}
        p = _protection()
        plain = DeliveryService(RecordedIngest(files), graph, clusters, DeliveryCache())
        guarded = DeliveryService(RecordedIngest(files), graph, clusters, DeliveryCache(),
                                  slots=p.slots)

        api_plain = to_json(plain.analyze(mint)).encode("utf-8")
        api_guarded = to_json(guarded.analyze(mint)).encode("utf-8")
        assert api_guarded == api_plain, label

        http_plain = handle_get(PATH + mint, PEER, None, plain, None)
        http_guarded = handle_get(PATH + mint, PEER, None, guarded, p)
        assert http_guarded[0] == http_plain[0] and http_guarded[2] == http_plain[2], label
        assert to_json(http_guarded[1]).encode("utf-8") == to_json(http_plain[1]).encode("utf-8"), label

        bot_plain = JournalTransport([_update(1, f"/check {mint}")])
        run_polling(bot_plain, plain, evidence_limit=evidence_limit, stop_after=1)
        bot_guarded = JournalTransport([_update(1, f"/check {mint}")])
        run_polling(bot_guarded, guarded, evidence_limit=evidence_limit, stop_after=1,
                    gate=p.users, spawn=sync_spawn)
        assert bot_plain.calls, label
        assert bot_guarded.calls == bot_plain.calls, label
        assert p.slots.in_use == 0
        checked += 1
    assert checked == len(tokens) == 9  # ins0..ins4, cln1..cln4 з manifest.yaml


# --------------------------------------------------------------------------- FR-008-14


def test_no_wall_clock_used(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("wall clock or sleep used on the e2e path")

    monkeypatch.setattr(time, "monotonic", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)

    tokens = _tokens()
    mint = {label: tokens[label][0] for label in tokens}
    files = {m: path for m, path in tokens.values()}
    clock = ManualClock(0.0)
    p = _protection(clock, api_requests_per_window=2, api_window_seconds=60,
                    bot_checks_per_window=1, bot_window_seconds=600)
    graph, clusters = _real_services()
    ingest = RecordedIngest(files)
    service = DeliveryService(ingest, graph, clusters, DeliveryCache(), slots=p.slots)
    evidence_limit = load_delivery_config(ROOT / "config" / "delivery.yaml").evidence_preview_limit

    # HTTP-канал: 2 запити на вікно 60 с, третій відхилено з Retry-After = 60 (за контрольованим годинником)
    assert handle_get(PATH + mint["cln2"], PEER, None, service, p)[0] == 200
    assert handle_get(PATH + mint["cln2"], PEER, None, service, p)[0] == 200
    status, doc, headers = handle_get(PATH + mint["cln2"], PEER, None, service, p)
    assert status == 429 and headers == [("Retry-After", "60")]

    # канал бота: 1 перевірка на 600 с; другий холодний /check відхилено з 600 с
    bot = JournalTransport([_update(1, f"/check {mint['cln3']}")])
    run_polling(bot, service, evidence_limit=evidence_limit, stop_after=1,
                gate=p.users, spawn=sync_spawn)
    assert not any("too many checks" in str(c) for c in bot.calls)  # перша перевірка допущена
    second = JournalTransport([_update(2, f"/check {mint['ins4']}")])
    run_polling(second, service, evidence_limit=evidence_limit, stop_after=1,
                gate=p.users, spawn=sync_spawn)
    assert second.calls == [("send_message", (7, RATE_BOT_TEXT_TEMPLATE.format(600)),
                             {"reply_markup": None})]
    assert mint["ins4"] not in ingest.calls  # відмова бота до збору: жодного аналізу

    # годинник рухає обидва канали: 61 с — API знову відкритий, бот ще ні (600 с)
    clock.advance(61)
    assert handle_get(PATH + mint["cln2"], PEER, None, service, p)[0] == 200
    third = JournalTransport([_update(3, f"/check {mint['ins4']}")])
    run_polling(third, service, evidence_limit=evidence_limit, stop_after=1,
                gate=p.users, spawn=sync_spawn)
    assert third.calls == [("send_message", (7, RATE_BOT_TEXT_TEMPLATE.format(539)),
                            {"reply_markup": None})]

    # ще 539 с — вікно бота вичерпане рівно, перевірка знову проходить
    clock.advance(539)
    fourth = JournalTransport([_update(4, f"/check {mint['ins4']}")])
    run_polling(fourth, service, evidence_limit=evidence_limit, stop_after=1,
                gate=p.users, spawn=sync_spawn)
    assert not any("too many checks" in str(c) for c in fourth.calls)
    assert fourth.calls, "допущена перевірка має відповісти"
    assert p.slots.in_use == 0
