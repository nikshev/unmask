# verifies: FR-008-07, FR-008-09
"""Ліміти бота (T-169; research R-9, контракт `contracts/overload.md`).

Транспорт — `JournalTransport`, годинник воріт — `ManualClock`, мережі й `sleep` немає. Рішення про допуск
приймається в потоці опитування, тож порядок подій керується відкладеним `spawn` (воркер лежить у списку й
запускається тестом), а реальні потоки синхронізуються лише `threading.Event`/`Barrier` з таймаутом.

Без `gate` бот лишається побайтово таким, як у 004 (SC-006): еталонний журнал викликів транспорту
`fixtures/bot_journal_004.json` знято з коду ДО правки T-169 на записаних фікстурах.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from test_delivery_service import RecordedIngest, _mint_of, _real_services
from unmask.delivery import bot as bot_module
from unmask.delivery.bot import JournalTransport, RequestRegistry, handle_check, run_polling
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.limits import HeavySlots, UserGate
from unmask.delivery.service import DeliveryService
from unmask.ingest.model import RejectKind, Rejection

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "bot_journal_004.json"
WAIT = 10.0

IN_PROGRESS = "your previous check is still running — please wait for its result"
START_HINT = "Send /check <mint> to analyze a token."


def rate_limited(n: int) -> str:
    return f"too many checks — try again in {n} s"


def busy(n: int) -> str:
    return f"service is busy with other analyses — try again in ~{n} s"


# --------------------------------------------------------------------------- дублі


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SpyGate:
    """Обгортка `UserGate`: журнал `enter`/`leave` (значення й ТИП `counts`)."""

    def __init__(self, inner: UserGate) -> None:
        self.inner = inner
        self.log: list[tuple] = []

    def try_enter(self, user, *, counts):
        self.log.append(("enter", user, counts))
        return self.inner.try_enter(user, counts=counts)

    def leave(self, user) -> None:
        self.log.append(("leave", user))
        self.inner.leave(user)

    def refund(self, user):
        self.log.append(("refund", user))
        return self.inner.refund(user)

    def refunds(self) -> list:
        return [e for e in self.log if e[0] == "refund"]

    def leaves(self) -> list:
        return [e for e in self.log if e[0] == "leave"]

    def enters(self) -> list:
        return [e for e in self.log if e[0] == "enter"]


def make_gate(limit: int = 5, window: float = 600, clock: ManualClock | None = None):
    clock = clock or ManualClock()
    return SpyGate(UserGate(limit, window, clock)), clock


class DeferredSpawn:
    """`spawn`, що лише складає воркери; тест запускає їх сам."""

    def __init__(self) -> None:
        self.workers: list = []

    def __call__(self, fn) -> None:
        self.workers.append(fn)

    def run(self, index: int) -> None:
        self.workers[index]()


def sync_spawn(fn) -> None:
    fn()


class ThreadSpawn:
    """Справжні потоки (daemon), які тест потім дожидає."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def __call__(self, fn) -> None:
        thread = threading.Thread(target=fn, daemon=True)
        with self._lock:
            self.threads.append(thread)
        thread.start()

    def join_all(self) -> None:
        for thread in list(self.threads):
            thread.join(WAIT)
            assert not thread.is_alive(), "worker thread hung"


class Ingest(RecordedIngest):
    """Записані фікстури; невідомий mint — відмова збору `invalid_address`, як у живому конвеєрі."""

    def collect(self, mint_: str):
        if mint_ not in self._mapping:
            self.calls.append(mint_)
            return Rejection(kind=RejectKind.INVALID_ADDRESS, mint=mint_, detail="bad base58")
        return super().collect(mint_)


def make_service(labels=("ins4", "cln1", "cln2"), slots: HeavySlots | None = None):
    files = {_mint_of(label)[0]: _mint_of(label)[1] for label in labels}
    ingest = Ingest(files)
    graph, clusters = _real_services()
    return DeliveryService(ingest, graph, clusters, DeliveryCache(), slots=slots), ingest


def mint(label: str) -> str:
    return _mint_of(label)[0]


def check(uid: int, text_mint: str, *, chat: int | None = 7, frm: int | None = None) -> dict:
    return text_update(uid, f"/check {text_mint}", chat=chat, frm=frm)


def text_update(uid: int, text: str, *, chat: int | None = 7, frm=None) -> dict:
    message: dict = {"message_id": uid, "text": text}
    if chat is not None:
        message["chat"] = {"id": chat}
    if frm is not None:
        message["from"] = {"id": frm}
    return {"update_id": uid, "message": message}


def feed(transport: JournalTransport, service, gate, spawn, *updates: dict, **kw):
    for update in updates:
        transport.queue(update)
    return run_polling(transport, service, evidence_limit=4000, stop_after=len(updates),
                       gate=gate, spawn=spawn, **kw)


def texts(transport: JournalTransport) -> list[str]:
    return [c[1][1] for c in transport.calls if c[0] == "send_message"]


def replies(transport: JournalTransport) -> list[str]:
    """Тексти без проміжних «🔄»-повідомлень прогресу."""
    return [text for text in texts(transport) if "🔄" not in text]


def message_call(chat: int, text: str) -> tuple:
    return ("send_message", (chat, text), {"reply_markup": None})


class StubService:
    """Сервіс-дубль: `needs_analysis` задається, `analyze` рахує виклики й може блокуватись."""

    def __init__(self, needs=True) -> None:
        self.needs = needs
        self.analyzed: list[str] = []
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def needs_analysis(self, mint_: str):
        return self.needs

    def analyze(self, mint_: str, progress=None):
        with self._lock:
            self.analyzed.append(mint_)
            self.threads.append(threading.current_thread())
        self.entered.set()
        assert self.release.wait(WAIT), "analysis never released"
        return {"mint": mint_, "analyzed_at": 1, "wallets_analyzed": 1, "clusters": [],
                "risk_score": 0, "band": "insufficient_data", "band_reasons": ["empty_input"],
                "provenance": {}, "error": None}


# --------------------------------------------------------------------------- рядок T-169


def test_user_over_window_gets_wait_text_and_no_analysis() -> None:
    gate, clock = make_gate(1, 600)
    service, ingest = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]
    sent = len(t.calls)

    clock.now = 100
    feed(t, service, gate, sync_spawn, check(2, mint("cln1"), frm=5))

    # ОДНЕ повідомлення з контрактним текстом; лічильник сервісу стоїть; аналіз не запускався
    assert t.calls[sent:] == [message_call(7, rate_limited(500))]
    assert ingest.calls == [mint("ins4")]
    assert service.needs_analysis(mint("cln1")) is True  # у кеш нічого не потрапило

    clock.now = 600  # рівно через обіцяні 500 с від першого запиту
    feed(t, service, gate, sync_spawn, check(3, mint("cln1"), frm=5))
    assert ingest.calls == [mint("ins4"), mint("cln1")]


@pytest.mark.parametrize("now, expected", [(0, 600), (1, 599), (300.2, 300), (599, 1), (599.5, 1)])
def test_rate_limited_text_carries_honest_seconds(now: float, expected: int) -> None:
    gate, clock = make_gate(1, 600)
    service, ingest = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    clock.now = now
    sent = len(t.calls)
    feed(t, service, gate, sync_spawn, check(2, mint("cln1"), frm=5))
    assert t.calls[sent:] == [message_call(7, rate_limited(expected))]
    assert ingest.calls == [mint("ins4")]


def test_second_check_while_running_gets_in_progress() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()

    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5), check(2, mint("cln1"), frm=5))

    # перший воркер ще не виконано: аналізу немає, а другий /check уже отримав відповідь
    assert len(spawn.workers) == 1
    assert ingest.calls == []
    assert t.calls == [message_call(7, IN_PROGRESS)]

    spawn.run(0)  # воркер завершується -> користувач вільний
    assert ingest.calls == [mint("ins4")]
    assert gate.leaves() == [("leave", 5)]

    sent = len(t.calls)
    feed(t, service, gate, spawn, check(3, mint("cln1"), frm=5))
    assert len(spawn.workers) == 2  # знову можна
    spawn.run(1)
    assert ingest.calls == [mint("ins4"), mint("cln1")]
    assert IN_PROGRESS not in texts(t)[sent:]


def test_in_progress_does_not_consume_the_window() -> None:
    gate, _ = make_gate(2, 600)
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5),
         *[check(2 + i, mint("cln1"), frm=5) for i in range(5)])
    assert texts(t) == [IN_PROGRESS] * 5
    spawn.run(0)
    feed(t, service, gate, spawn, check(10, mint("cln1"), frm=5))  # 2-й дозволений за вікно
    spawn.run(1)
    assert ingest.calls == [mint("ins4"), mint("cln1")]


def test_global_busy_reply_without_progress_messages() -> None:
    slots = HeavySlots(1, 37)
    held = slots.try_acquire()
    assert held is not None
    gate, _ = make_gate()
    service, ingest = make_service(slots=slots)
    t = JournalTransport()

    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))

    assert t.calls == [message_call(7, busy(37))]  # одне повідомлення, жодного «🔄»
    assert ingest.calls == []
    assert all("🔄" not in text for text in texts(t))
    assert gate.leaves() == [("leave", 5)]  # користувач не застряг

    held.release()
    sent = len(t.calls)
    feed(t, service, gate, sync_spawn, check(2, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]
    assert busy(37) not in texts(t)[sent:]


def test_cached_mint_does_not_consume_window() -> None:
    gate, _ = make_gate(2, 600)
    service, ingest = make_service()
    t = JournalTransport()
    updates = [check(1, mint("ins4"), frm=5)]
    updates += [check(2 + i, mint("ins4"), frm=5) for i in range(3)]  # кеш-хіти
    updates += [check(10, mint("cln1"), frm=5), check(11, mint("cln2"), frm=5)]

    feed(t, service, gate, sync_spawn, *updates)

    assert [e[2] for e in gate.enters()] == [True, False, False, False, True, True]
    assert ingest.calls == [mint("ins4"), mint("cln1")]  # третій холодний — відмова
    assert texts(t)[-1] == rate_limited(600)
    # кешовані повтори отримали справжній результат, не відмову
    assert sum(1 for c in t.calls if c[2].get("reply_markup")) == 4


def test_gate_released_after_worker_exception() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    calls = {"n": 0}

    def flaky_render(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("render defect")
        raise RuntimeError("still broken")

    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5), check(2, mint("ins4"), frm=5),
         render=flaky_render)

    # обидва /check отримали відповідь про помилку (не мовчання і не in_progress), ворота вільні
    assert replies(t) == ["analysis error: RuntimeError", "analysis error: RuntimeError"]
    assert gate.leaves() == [("leave", 5), ("leave", 5)]
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_exception_in_a_worker_thread_still_answers_the_user() -> None:
    # У потоці немає зовнішнього обробника циклу опитування: відповідь про збій дає сам воркер.
    gate, _ = make_gate()
    service, _ = make_service()
    spawn = ThreadSpawn()
    t = JournalTransport()

    def broken_render(doc):
        raise RuntimeError("render defect")

    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5), render=broken_render)
    spawn.join_all()

    assert replies(t) == ["analysis error: RuntimeError"]
    assert gate.leaves() == [("leave", 5)]


def test_gate_released_when_worker_raises_base_exception() -> None:
    class Abort(BaseException):
        pass

    def render(doc):
        raise Abort

    gate, _ = make_gate()
    service, _ = make_service()
    with pytest.raises(Abort):
        feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5), render=render)
    assert gate.leaves() == [("leave", 5)]
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_gate_released_when_error_reply_itself_fails() -> None:
    class Down(JournalTransport):
        def send_message(self, chat_id, text, reply_markup=None):
            raise ConnectionError("telegram down")

    class Exploding(StubService):
        def analyze(self, mint_, progress=None):
            raise RuntimeError("defect")

    gate, _ = make_gate()
    feed(Down(), Exploding(), gate, sync_spawn, check(1, mint("ins4"), frm=5))  # цикл не вмирає
    assert gate.leaves() == [("leave", 5)]
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_users_are_independent() -> None:
    gate, _ = make_gate(1, 600)
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()

    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5), check(2, mint("cln1"), frm=6),
         check(3, mint("cln2"), frm=5), check(4, mint("cln2"), frm=6))

    assert len(spawn.workers) == 2  # обидва користувачі в одному чаті допущені
    assert texts(t) == [IN_PROGRESS, IN_PROGRESS]
    spawn.run(0)
    spawn.run(1)
    assert sorted(ingest.calls) == sorted([mint("ins4"), mint("cln1")])

    # ліміт вікна одного не зачіпає іншого
    feed(t, service, gate, spawn, check(5, mint("cln2"), frm=5))
    assert texts(t)[-1] == rate_limited(600)
    gate2, _ = make_gate(1, 600)
    t2 = JournalTransport()
    feed(t2, service, gate2, sync_spawn, check(1, mint("cln2"), frm=5), check(2, mint("cln2"), frm=6))
    assert not any(text.startswith("too many") for text in texts(t2))


def test_user_key_is_from_id_across_chats() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), chat=100, frm=5),
         check(2, mint("cln1"), chat=200, frm=5))
    assert gate.enters()[0][1] == 5 and gate.enters()[1][1] == 5
    assert t.calls == [message_call(200, IN_PROGRESS)]  # тот самий користувач у іншому чаті
    assert len(spawn.workers) == 1


def test_user_key_falls_back_to_chat_id_without_from() -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), chat=7), check(2, mint("cln1"), chat=7),
         check(3, mint("cln1"), chat=8))
    assert [e[1] for e in gate.enters()] == [7, 7, 8]
    assert texts(t) == [IN_PROGRESS]
    assert len(spawn.workers) == 2


@pytest.mark.parametrize("sender", ["alice", [1], {"id": [1]}, {"id": None}, {"id": 1.5}, {"id": True}, {}],
                         ids=["str", "list", "id-list", "id-none", "id-float", "id-bool", "empty"])
def test_garbage_from_falls_back_to_chat_id(sender) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    update = check(1, mint("ins4"), chat=7)
    update["message"]["from"] = sender
    feed(JournalTransport(), service, gate, sync_spawn, update)
    assert [e[1] for e in gate.enters()] == [7]


def test_update_without_any_user_key_is_dropped_without_gate_or_reply() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), chat=None),
         check(2, mint("ins4"), chat=7, frm=5))
    assert gate.enters() == [("enter", 5, True)]  # перший не мав ключа -> ворота не чіпали
    assert ingest.calls == [mint("ins4")]  # цикл живий


def test_start_and_help_are_never_limited() -> None:
    gate, _ = make_gate(1, 600)
    service, _ = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5))  # користувач 5 тепер активний
    sent = len(t.calls)
    feed(t, service, gate, spawn, text_update(2, "/start", frm=5), text_update(3, "/help", frm=5),
         text_update(4, "/start", frm=5))
    assert t.calls[sent:] == [message_call(7, START_HINT), message_call(7, START_HINT)]
    assert len(gate.enters()) == 1  # ворота бачили лише /check


def test_unknown_updates_do_not_touch_gate_or_transport() -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    t = JournalTransport()
    updates = [
        {"update_id": 1},
        {"update_id": 2, "edited_message": {"text": "/check x", "chat": {"id": 7}}},
        {"update_id": 3, "channel_post": {"text": "/check x", "chat": {"id": 7}}},
        text_update(4, "привіт", frm=5),
        text_update(5, "/help", frm=5),
        {"update_id": 6, "message": {"message_id": 6, "chat": {"id": 7}}},  # без тексту
        {"update_id": 7, "my_chat_member": {}},
    ]
    feed(t, service, gate, sync_spawn, *updates)
    assert t.calls == [] and gate.log == []


def test_check_without_mint_gets_hint_as_in_004_even_while_active() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5), text_update(2, "/check", frm=5),
         text_update(3, "/check   ", frm=5))
    assert texts(t) == [START_HINT, START_HINT]
    assert len(gate.enters()) == 1 and len(spawn.workers) == 1


def test_invalid_mint_replies_as_in_004_and_does_not_consume_window() -> None:
    def journal(gate):
        service, _ = make_service()
        t = JournalTransport()
        feed(t, service, gate, sync_spawn, *[text_update(i, "/check !!!", frm=5) for i in range(1, 4)])
        return t.calls

    gate, _ = make_gate(1, 600)
    with_gate = journal(gate)
    assert with_gate == journal(None)  # та сама відповідь, що й без воріт
    assert any("Solana address" in c[1][1] for c in with_gate)
    assert [e[2] for e in gate.enters()] == [False, False, False]

    service, ingest = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(9, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]  # вікно не з'їдене невалідними адресами


def test_invalid_mint_while_running_gets_in_progress() -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5), text_update(2, "/check !!!", frm=5))
    assert texts(t) == [IN_PROGRESS]  # правило «один одночасний» діє для кожного /check


@pytest.mark.parametrize("raw, expected", [(True, True), (False, False), (1, True), (0, False),
                                           ("yes", True), ("", False), (None, False), ([1], True)])
def test_counts_passed_to_gate_is_strictly_bool(raw, expected) -> None:
    gate, _ = make_gate()
    service = StubService(needs=raw)
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    ((_, _, counts),) = gate.enters()
    assert type(counts) is bool and counts is expected
    assert service.analyzed == [mint("ins4")]  # некоректний тип не зірвав і не обійшов ворота


def test_counts_follows_service_needs_analysis_on_real_service() -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5),
         check(2, mint("ins4"), frm=5), text_update(3, "/check !!!", frm=5))
    assert [e[2] for e in gate.enters()] == [True, False, False]


# --------------------------------------------------------------------------- callback «докази»


@pytest.mark.parametrize("state", ["exhausted_window", "active_user"])
def test_evidence_callback_never_limited(state: str) -> None:
    gate, _ = make_gate(1, 600)
    service, _ = make_service()
    spawn = sync_spawn if state == "exhausted_window" else DeferredSpawn()
    t = JournalTransport()
    callback = {"update_id": 50, "callback_query": {
        "id": "q1", "data": f"evidence:{mint('ins4')[:8]}:0", "message": {"chat": {"id": 7}}}}
    stale = {"update_id": 51, "callback_query": {
        "id": "q2", "data": "evidence:dead:0", "message": {"chat": {"id": 7}}}}
    if state == "exhausted_window":
        updates = [check(1, mint("ins4"), frm=5), callback, stale, check(2, mint("cln1"), frm=5)]
    else:
        updates = [check(1, mint("ins4"), frm=5), stale]
    feed(t, service, gate, spawn, *updates)

    kinds = [c[0] for c in t.calls]
    assert kinds.count("answer_callback") >= 1  # колбек обслуговано
    if state == "exhausted_window":
        assert kinds.count("answer_callback") == 2
        ev = [c for c in t.calls if c[0] == "send_message" and "Cluster #1" in c[1][1]]
        assert len(ev) == 1  # повний текст доказів
        assert texts(t)[-1] == rate_limited(600)  # а новий /check — відмова
    else:
        assert "request expired, send /check again" in texts(t)
    assert len(gate.enters()) == (2 if state == "exhausted_window" else 1)  # колбек воріт не чіпав


# --------------------------------------------------------------------------- SC-006: без воріт — як у 004


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _run_golden(gate, spawn):
    data = _golden()
    service, _ = make_service(("ins4", "cln1"))
    t = JournalTransport(data["updates"])
    registry = run_polling(t, service, evidence_limit=4000, stop_after=len(data["updates"]),
                           gate=gate, spawn=spawn)
    return data, t, registry


def _journal(t: JournalTransport) -> list:
    return json.loads(json.dumps([[n, list(a), k] for n, a, k in t.calls]))


def test_without_gate_journal_identical_to_004() -> None:
    data, t, registry = _run_golden(None, None)
    assert _journal(t) == data["calls"]
    assert dict(registry) == data["registry"]
    assert len(data["calls"]) == 19


def test_without_gate_a_spawn_is_ignored_and_processing_stays_sequential() -> None:
    def forbidden(fn):
        raise AssertionError("spawn must not be used without a gate")

    data, t, registry = _run_golden(None, forbidden)
    assert _journal(t) == data["calls"]


def test_run_polling_signature_defaults_keep_004_behaviour() -> None:
    import inspect

    params = inspect.signature(run_polling).parameters
    assert params["gate"].default is None and params["spawn"].default is None
    assert params["gate"].kind is inspect.Parameter.KEYWORD_ONLY


def test_gated_synchronous_journal_equals_004_when_nobody_is_limited() -> None:
    gate, _ = make_gate(100, 600)
    data, t, registry = _run_golden(gate, sync_spawn)
    assert _journal(t) == data["calls"]  # ворота не змінюють успішні відповіді
    assert dict(registry) == data["registry"]


# --------------------------------------------------------------------------- spawn, що кидає


def test_spawn_that_raises_releases_gate_and_reports_error() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    t = JournalTransport()

    def no_threads(fn):
        raise RuntimeError("can't start new thread")

    feed(t, service, gate, no_threads, check(1, mint("ins4"), frm=5))

    assert gate.leaves() == [("leave", 5)]
    assert ingest.calls == []
    assert texts(t) == ["analysis error: RuntimeError"]  # користувач не лишився без відповіді
    # не застряг: наступний /check допущений
    feed(t, service, gate, sync_spawn, check(2, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]
    assert IN_PROGRESS not in texts(t)


def test_spawn_raising_base_exception_releases_gate_and_propagates() -> None:
    gate, _ = make_gate()
    service, _ = make_service()

    def interrupted(fn):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        feed(JournalTransport(), service, gate, interrupted, check(1, mint("ins4"), frm=5))
    assert gate.leaves() == [("leave", 5)]
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_spawn_that_runs_worker_then_raises_leaves_exactly_once() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()

    def runs_then_fails(fn):
        fn()
        raise RuntimeError("late failure")

    feed(JournalTransport(), service, gate, runs_then_fails, check(1, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]
    assert gate.leaves() == [("leave", 5)]  # єдиний leave, без дубля


def test_worker_abandoned_by_failed_spawn_is_inert_and_never_frees_a_newer_entry() -> None:
    gate, _ = make_gate()
    service, ingest = make_service()
    stash: list = []

    def stores_then_fails(fn):
        stash.append(fn)
        raise RuntimeError("late failure")

    t = JournalTransport()
    feed(t, service, gate, stores_then_fails, check(1, mint("ins4"), frm=5))
    assert gate.leaves() == [("leave", 5)]

    spawn = DeferredSpawn()
    feed(t, service, gate, spawn, check(2, mint("cln1"), frm=5))  # новий допуск того ж користувача
    stash[0]()  # покинутий воркер ніколи не мав би виконатися
    assert ingest.calls == [] and len(gate.leaves()) == 1
    feed(t, service, gate, spawn, check(3, mint("cln1"), frm=5))
    assert texts(t)[-1] == IN_PROGRESS  # чужий leave не звільнив нового допуску
    spawn.run(0)
    assert ingest.calls == [mint("cln1")]
    assert len(gate.leaves()) == 2


def test_gate_ticket_leaves_only_after_begin_and_only_once() -> None:
    gate, _ = make_gate()
    ticket = bot_module._GateTicket(gate, 5)
    ticket.finish()  # воркер не стартував -> звільняти нічого
    assert gate.leaves() == []
    assert ticket.begin() is True and ticket.begin() is False  # старт лише один
    ticket.finish()
    ticket.finish()
    ticket.abort()
    assert gate.leaves() == [("leave", 5)]


def test_at_most_one_worker_and_one_leave_per_admission() -> None:
    gate, _ = make_gate(100, 600)
    service, ingest = make_service()
    spawn = DeferredSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, *[check(i, mint("ins4"), frm=5) for i in range(1, 11)])
    assert len(spawn.workers) == 1 and texts(t) == [IN_PROGRESS] * 9
    spawn.run(0)
    spawn.run(0)  # повторний запуск того самого воркера: no-op
    assert ingest.calls == [mint("ins4")]
    assert gate.leaves() == [("leave", 5)]


# --------------------------------------------------------------------------- реальні потоки


def test_twenty_updates_of_one_user_run_exactly_one_analysis() -> None:
    gate, _ = make_gate(100, 600)
    service = StubService()
    service.release.clear()  # аналіз висить, поки тест не відпустить
    spawn = ThreadSpawn()
    t = JournalTransport()
    updates = [check(i, mint("ins4"), frm=5) for i in range(1, 21)]

    feed(t, service, gate, spawn, *updates)  # усі 20 оброблено, поки перший аналіз ще йде
    assert service.entered.wait(WAIT)
    assert len(spawn.threads) == 1
    assert texts(t) == [IN_PROGRESS] * 19

    service.release.set()
    spawn.join_all()
    assert service.analyzed == [mint("ins4")]
    assert all(th.daemon for th in spawn.threads)
    assert gate.leaves() == [("leave", 5)]
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_twenty_concurrent_pollers_same_user_exactly_one_worker() -> None:
    # Рандеву: 20 потоків опитування одночасно подають апдейт одного користувача в спільні ворота.
    gate, _ = make_gate(100, 600)
    service = StubService()
    service.release.clear()
    spawn = ThreadSpawn()
    barrier = threading.Barrier(20, timeout=WAIT)
    transports = [JournalTransport() for _ in range(20)]
    errors: list[BaseException] = []

    def poller(i: int) -> None:
        try:
            barrier.wait()
            run_polling(transports[i], service, evidence_limit=4000, stop_after=1, gate=gate, spawn=spawn,
                        sleep=lambda s: None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    for i, tr in enumerate(transports):
        tr.queue(check(i + 1, mint("ins4"), frm=5))
    pollers = [threading.Thread(target=poller, args=(i,), daemon=True) for i in range(20)]
    for p in pollers:
        p.start()
    for p in pollers:
        p.join(WAIT)
        assert not p.is_alive()
    assert errors == []

    assert len(spawn.threads) == 1  # рівно один воркер
    refused = [tr for tr in transports if texts(tr) == [IN_PROGRESS]]
    assert len(refused) == 19
    service.release.set()
    spawn.join_all()
    assert service.analyzed == [mint("ins4")]
    assert len(gate.enters()) == 20 and gate.leaves() == [("leave", 5)]


def test_pollers_of_different_users_each_get_their_own_worker() -> None:
    gate, _ = make_gate(100, 600)
    service = StubService()
    service.release.clear()
    spawn = ThreadSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, *[check(i, mint("ins4"), frm=100 + i) for i in range(8)])
    assert len(spawn.threads) == 8 and texts(t) == []
    service.release.set()
    spawn.join_all()
    assert len(service.analyzed) == 8
    assert sorted(e[1] for e in gate.leaves()) == [100 + i for i in range(8)]


def test_default_spawn_is_a_daemon_thread_other_than_the_polling_thread() -> None:
    gate, _ = make_gate()
    service = StubService()
    t = JournalTransport()
    caller = threading.current_thread()
    feed(t, service, gate, None, check(1, mint("ins4"), frm=5))  # spawn=None -> умовчання
    assert service.entered.wait(WAIT)
    worker = service.threads[0]
    assert worker is not caller and worker.daemon is True
    worker.join(WAIT)
    assert not worker.is_alive()
    assert gate.inner.try_enter(5, counts=False).kind == "ok"


def test_default_spawn_function_starts_daemon_thread() -> None:
    done = threading.Event()
    seen: list[threading.Thread] = []

    def work() -> None:
        seen.append(threading.current_thread())
        done.set()

    bot_module._spawn_thread(work)
    assert done.wait(WAIT)
    assert seen[0].daemon is True and seen[0] is not threading.current_thread()


# --------------------------------------------------------------------------- реєстр запитів


def test_request_registry_ids_come_from_a_counter_not_from_len() -> None:
    reg = RequestRegistry(limit=2)
    ids = [reg.add("MINTAAAAxx"), reg.add("MINTAAAAxx"), reg.add("MINTAAAAxx")]
    assert ids == ["MINTAAAA:0", "MINTAAAA:1", "MINTAAAA:2"]  # без колізій після витіснення
    assert list(reg) == ["MINTAAAA:1", "MINTAAAA:2"]
    assert reg.lookup("MINTAAAA:0") is None and reg.lookup("MINTAAAA:2") == "MINTAAAAxx"


def test_request_registry_mutations_hold_the_lock() -> None:
    class SpyLock:
        def __init__(self) -> None:
            self.inner = threading.Lock()
            self.held = False
            self.entries = 0

        def __enter__(self):
            self.inner.acquire()
            self.held = True
            self.entries += 1
            return self

        def __exit__(self, *exc) -> None:
            self.held = False
            self.inner.release()

    class Checked(RequestRegistry):
        def __setitem__(self, key, value) -> None:
            assert self._lock.held, "write outside the lock"
            super().__setitem__(key, value)

        def pop(self, *args):
            assert self._lock.held, "eviction outside the lock"
            return super().pop(*args)

    reg = Checked(limit=3)
    reg._lock = SpyLock()
    for i in range(8):
        reg.add(f"M{i:07d}")
    assert len(reg) == 3
    before = reg._lock.entries
    assert reg.lookup("absent:0") is None
    assert reg.lookup("M0000007:7") == "M0000007"
    assert reg._lock.entries == before + 2  # читання теж під замком


def test_request_registry_survives_concurrent_registration() -> None:
    reg = RequestRegistry(limit=50)
    ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16, timeout=WAIT)

    def work(n: int) -> None:
        try:
            barrier.wait()
            mine = [reg.add(f"T{n:03d}{i:04d}") for i in range(300)]
            with lock:
                ids.extend(mine)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(WAIT * 3)
    assert errors == []
    assert len(ids) == len(set(ids)) == 16 * 300  # усі id унікальні
    assert len(reg) == 50


def test_handle_check_still_accepts_a_plain_dict_registry() -> None:
    service, _ = make_service()
    requests: dict[str, str] = {}
    request_id = handle_check(f"/check {mint('ins4')}", service, JournalTransport(), 7, requests)
    assert requests == {request_id: mint("ins4")}


# --------------------------------------------------------------------------- _format_error


@pytest.mark.parametrize("error, expected", [
    ({"kind": "busy", "detail": "too many analyses in progress, retry after 60 s", "retry_after_seconds": 60},
     busy(60)),
    ({"kind": "busy", "detail": "x", "retry_after_seconds": 7}, busy(7)),
    ({"kind": "rate_limited", "detail": "x", "retry_after_seconds": 12}, rate_limited(12)),
    ({"kind": "invalid_address", "detail": "x"}, "doesn't look like a Solana address — check the mint"),
    ({"kind": "token_not_found", "detail": "x"}, "token not found"),
    ({"kind": "rpc_error", "detail": "limit"}, "incomplete data: limit"),
])
def test_format_error_texts_follow_the_contract(error: dict, expected: str) -> None:
    assert bot_module._format_error(error) == expected


@pytest.mark.parametrize("bad", [None, 0, -3, True, 1.5, "60"])
def test_busy_without_a_valid_retry_never_pretends_a_number(bad) -> None:
    text = bot_module._format_error({"kind": "busy", "detail": "d", "retry_after_seconds": bad})
    assert text == "incomplete data: d"  # чесне загальне повідомлення, не вигадане «~N»
    assert text != busy(0)


# --------------------------------------------------------------------------- рядок T-174
# Повернення квоти, коли холодний /check відхилено глобальним `busy` (FR-008-07).


def busy_slots(retry: int = 37):
    slots = HeavySlots(1, retry)
    held = slots.try_acquire()
    assert held is not None
    return slots, held


class DocService(StubService):
    """`StubService`, що віддає наперед заданий документ (або кидає виняток)."""

    def __init__(self, doc=None, *, needs=True, exc: BaseException | None = None) -> None:
        super().__init__(needs)
        self.doc = doc
        self.exc = exc

    def analyze(self, mint_: str, progress=None):
        if self.exc is not None:
            raise self.exc
        if self.doc is not None:
            self.analyzed.append(mint_)
            return self.doc
        return super().analyze(mint_, progress)


BUSY_DOC = {"mint": "m", "error": {"kind": "busy", "detail": "d", "retry_after_seconds": 37}}


def window_stamps(gate, user) -> list:
    return list(gate.inner._window._hits.get(user, ()))


def test_busy_reply_refunds_quota() -> None:
    slots, held = busy_slots(37)
    gate, clock = make_gate(1, 600)
    service, ingest = make_service(slots=slots)
    t = JournalTransport()

    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))

    # відповідь — лише busy; refund викликано рівно раз і ПЕРЕД leave
    assert t.calls == [message_call(7, busy(37))]
    assert gate.log == [("enter", 5, True), ("refund", 5), ("leave", 5)]
    assert window_stamps(gate, 5) == []

    # слухняний користувач повторює через retry_after (квота 1): аналіз іде, а не rate_limited
    held.release()
    clock.now = 37
    sent = len(t.calls)
    feed(t, service, gate, sync_spawn, check(2, mint("ins4"), frm=5))
    assert ingest.calls == [mint("ins4")]
    assert all(text != rate_limited(600) for text in texts(t)[sent:])
    assert gate.inner.try_enter(5, counts=True).kind == "rate_limited"  # а тепер квота справді витрачена


def test_repeated_busy_never_exhausts_the_quota() -> None:
    slots, held = busy_slots()
    gate, _ = make_gate(1, 600)
    service, ingest = make_service(slots=slots)
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, *[check(i, mint("ins4"), frm=5) for i in range(1, 8)])
    assert replies(t) == [busy(37)] * 7  # жодного rate_limited
    assert len(gate.refunds()) == 7 and ingest.calls == []


def test_busy_refund_is_per_user() -> None:
    slots, held = busy_slots()
    gate, _ = make_gate(1, 600)
    service, ingest = make_service(slots=slots)
    t = JournalTransport()
    held.release()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=6))  # user 6 витрачає квоту по-справжньому
    held = slots.try_acquire()
    assert held is not None
    feed(t, service, gate, sync_spawn, check(2, mint("cln1"), frm=5))  # user 5 — busy -> refund
    assert gate.refunds() == [("refund", 5)]
    assert len(window_stamps(gate, 6)) == 1 and window_stamps(gate, 5) == []


def test_success_and_error_reply_do_not_refund() -> None:
    # успіх, відмова збору, виняток сервісу, виняток рендера: аналіз відбувся (або впав) -> квота спалена
    cases = [
        ("success", make_service()[0], None),
        ("rejection", make_service(labels=())[0], None),  # невалідний mint -> Rejection, не busy
        ("incomplete", DocService({"mint": "m", "error": {"kind": "rpc_error", "detail": "limit"}}), None),
        ("not_found", DocService({"mint": "m", "error": {"kind": "token_not_found", "detail": "x"}}), None),
        ("exception", DocService(exc=RuntimeError("boom")), None),
        ("render_error", make_service()[0], lambda doc: (_ for _ in ()).throw(RuntimeError("render"))),
    ]
    for name, service, render in cases:
        gate, _ = make_gate(1, 600)
        if name == "rejection":
            service.needs_analysis = lambda _m: True  # type: ignore[method-assign]
        t = JournalTransport()
        feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5), render=render)
        assert gate.refunds() == [], name
        assert gate.leaves() == [("leave", 5)], name
        assert len(window_stamps(gate, 5)) == 1, name


def test_error_doc_with_busy_in_detail_or_other_field_does_not_refund() -> None:
    # busy лише за `error.kind == "busy"`, не за текстом
    for doc in (
        {"mint": "m", "error": {"kind": "rpc_error", "detail": "busy busy busy"}},
        {"mint": "m", "busy": True, "error": {"kind": "invalid_address", "detail": "busy"}},
        {"mint": "m", "error": "busy"},
        {"mint": "m", "error": {"kind": "BUSY", "detail": "x"}},
    ):
        gate, _ = make_gate(1, 600)
        feed(JournalTransport(), DocService(doc), gate, sync_spawn, check(1, "x" * 5, frm=5))
        assert gate.refunds() == [], doc


def test_cached_mint_busy_never_refunds_nothing_to_refund() -> None:
    # допуск counts=False (кеш-хіт) не списував квоту; якщо відповідь усе ж busy — повертати нічого,
    # а мітка ПОПЕРЕДНЬОГО допуску цiла
    gate, _ = make_gate(1, 600)
    service = DocService(BUSY_DOC)
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, "m1", frm=5))  # холодний busy -> refund
    assert window_stamps(gate, 5) == []
    gate.inner.try_enter(5, counts=True)  # користувач реально витратив квоту
    gate.inner.leave(5)
    service.needs = False
    feed(t, service, gate, sync_spawn, check(2, "m1", frm=5))  # counts=False + busy-документ
    assert [e[2] for e in gate.enters()] == [True, False]
    assert gate.refunds() == [("refund", 5)]  # лише перший
    assert len(window_stamps(gate, 5)) == 1
    assert gate.inner.try_enter(5, counts=True).kind == "rate_limited"


def test_cached_mint_busy_with_real_service_gate_stays_untouched() -> None:
    service, ingest = make_service()
    gate, _ = make_gate(1, 600)
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    feed(t, service, gate, sync_spawn, check(2, mint("ins4"), frm=5))  # кеш-хіт
    assert gate.refunds() == []
    assert len(window_stamps(gate, 5)) == 1


def test_refund_at_most_once_per_admission() -> None:
    gate, _ = make_gate(2, 600)
    ticket = bot_module._GateTicket(gate, 5, counts=True)
    assert gate.inner.try_enter(5, counts=True).kind == "ok"
    ticket.refund()  # воркер не стартував -> нема чого повертати
    assert gate.refunds() == []
    assert ticket.begin() is True
    ticket.refund()
    ticket.refund()
    ticket.refund()
    assert gate.refunds() == [("refund", 5)]
    ticket.finish()
    ticket.refund()  # після leave — заборонено
    assert gate.refunds() == [("refund", 5)] and gate.leaves() == [("leave", 5)]


def test_worker_calling_on_busy_many_times_refunds_once() -> None:
    gate, _ = make_gate(1, 600)

    original = bot_module.handle_check

    def noisy(text, service, transport, chat_id, requests, *, render=None, on_busy=None, **_log_kw):  # T-175: +user=
        assert on_busy is not None
        on_busy()
        on_busy()
        on_busy()
        return None

    bot_module.handle_check = noisy  # type: ignore[assignment]
    try:
        feed(JournalTransport(), DocService(BUSY_DOC), gate, sync_spawn, check(1, "m1", frm=5))
    finally:
        bot_module.handle_check = original  # type: ignore[assignment]
    assert gate.refunds() == [("refund", 5)]


def test_ticket_without_counts_never_refunds() -> None:
    gate, _ = make_gate(2, 600)
    ticket = bot_module._GateTicket(gate, 5, counts=False)
    assert ticket.begin() is True
    ticket.refund()
    assert gate.refunds() == []
    # значення за умовчанням — не рахований допуск: без явного counts=True повернення неможливе
    legacy = bot_module._GateTicket(gate, 5)
    assert legacy.begin() is True
    legacy.refund()
    assert gate.refunds() == []


def test_abandoned_late_worker_neither_analyzes_nor_refunds() -> None:
    slots, held = busy_slots()
    gate, _ = make_gate(3, 600)
    service, ingest = make_service(slots=slots)
    stash: list = []

    def stores_then_fails(fn):
        stash.append(fn)
        raise RuntimeError("late failure")

    t = JournalTransport()
    feed(t, service, gate, stores_then_fails, check(1, mint("ins4"), frm=5))
    assert gate.leaves() == [("leave", 5)] and gate.refunds() == []  # скасований допуск не повертає квоту

    spawn = DeferredSpawn()
    feed(t, service, gate, spawn, check(2, mint("cln1"), frm=5))  # новий допуск того ж користувача
    assert len(spawn.workers) == 1
    stamps = window_stamps(gate, 5)
    assert len(stamps) == 2

    stash[0]()  # запізнілий покинутий воркер: ні аналізу (отже ні busy-відповіді), ні refund
    assert gate.refunds() == [] and ingest.calls == []
    assert window_stamps(gate, 5) == stamps
    feed(t, service, gate, spawn, check(3, mint("cln1"), frm=5))
    assert texts(t)[-1] == IN_PROGRESS  # новий допуск не звільнено чужим воркером

    spawn.run(0)  # справжній воркер отримує busy -> повертає саме свою мітку
    assert gate.refunds() == [("refund", 5)]
    assert len(window_stamps(gate, 5)) == 1


def test_stale_ticket_refund_never_removes_the_stamp_of_a_newer_admission() -> None:
    gate, _ = make_gate(5, 600)
    assert gate.inner.try_enter(5, counts=True).kind == "ok"
    old = bot_module._GateTicket(gate, 5, counts=True)
    assert old.begin() is True
    old.finish()  # leave
    assert gate.inner.try_enter(5, counts=True).kind == "ok"  # новий допуск, його мітка
    old.refund()  # запізніле повернення старого допуску
    assert gate.refunds() == []
    assert len(window_stamps(gate, 5)) == 2


def test_refund_failure_never_turns_busy_reply_into_error() -> None:
    slots, held = busy_slots()
    gate, _ = make_gate(1, 600)

    def broken_refund(user):
        gate.log.append(("refund", user))
        raise RuntimeError("refund exploded")

    gate.refund = broken_refund  # type: ignore[method-assign]
    service, _ = make_service(slots=slots)
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert t.calls == [message_call(7, busy(37))]  # одна чесна busy-відповідь, без "analysis error"
    assert gate.leaves() == [("leave", 5)]  # користувач не застряг


def test_busy_refund_in_a_real_worker_thread_is_before_leave() -> None:
    slots, held = busy_slots()
    gate, _ = make_gate(1, 600)
    service, ingest = make_service(slots=slots)
    spawn = ThreadSpawn()
    t = JournalTransport()
    feed(t, service, gate, spawn, check(1, mint("ins4"), frm=5))
    spawn.join_all()
    assert [e[0] for e in gate.log] == ["enter", "refund", "leave"]
    assert window_stamps(gate, 5) == []


def test_handle_check_on_busy_is_called_only_for_busy_documents() -> None:
    class Svc:
        def __init__(self, doc):
            self.doc = doc

        def analyze(self, mint_, progress=None):
            return self.doc

    for doc, expected in (
        (BUSY_DOC, 1),
        ({"mint": "m", "error": {"kind": "busy", "detail": "d"}}, 1),  # busy без числа теж не аналіз
        ({"mint": "m", "error": {"kind": "invalid_address", "detail": "d"}}, 0),
        ({"mint": "m", "error": {"kind": "token_not_found", "detail": "d"}}, 0),
        ({"mint": "m", "error": None, "risk_score": 0, "band": "clean", "clusters": [],
          "provenance": {}}, 0),
    ):
        calls: list[int] = []
        t = JournalTransport()
        handle_check("/check abc", Svc(doc), t, 7, {}, on_busy=lambda: calls.append(len(t.calls)))
        assert len(calls) == expected, doc
        if expected:
            assert calls == [0]  # повернення квоти — ДО відповіді користувачу


def test_handle_check_without_on_busy_is_unchanged() -> None:
    class Svc:
        def analyze(self, mint_, progress=None):
            return BUSY_DOC

    t = JournalTransport()
    assert handle_check("/check abc", Svc(), t, 7, {}) is None
    assert t.calls == [message_call(7, busy(37))]
