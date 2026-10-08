# impl: FR-004-03, FR-004-04, FR-004-07, FR-004-09, FR-008-07, FR-008-09
"""Telegram-бот доставки: polling-цикл, `/check`, callback «докази» (R-2, контракт `bot-messages.md`).

Транспорт — межа (`BotTransport`): живий на сирих викликах Bot API через `httpx`,
несправжній (журнальний) — у тестах. Форматування — дослівно за контрактом.

Ліміти (фіча 008, R-9, `contracts/overload.md`): `run_polling(..., gate=None, spawn=None)`. Без `gate` цикл
послідовний і побайтово такий, як у 004. З `gate` рішення про допуск `/check <mint>` приймається ТУТ, у
потоці опитування (`gate.try_enter`, атомарно), а сам аналіз іде у воркері через `spawn`: тож другий `/check`
того ж користувача отримує «ще виконується», поки перший триває. `gate.leave` викликається рівно раз на
допуск — у `finally` воркера, а якщо `spawn` кинув до запуску воркера, то одразу тут (`_GateTicket`).

Повернення квоти (T-174): холодний `/check`, відхилений глобальним `busy` (слоти зайняті, аналізу не було),
не на рахунку користувача. Воркер, побачивши `error.kind == "busy"`, викликає `gate.refund` рівно раз і лише
для допуску, що списав квоту (`counts=True`), — ПЕРЕД `leave`; для покинутого воркера й кеш-хіту нічого не
повертається.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Any, Protocol

import httpx

__all__ = [
    "BotTransport",
    "HttpxBotTransport",
    "JournalTransport",
    "RequestRegistry",
    "format_check",
    "format_evidence",
    "handle_check",
    "handle_evidence",
    "run_polling",
]

BAND_WORDS = {
    "clean": "clean",
    "suspicious": "suspicious",
    "high_concentration": "high concentration",
    "insufficient_data": "insufficient data",
}

_EVIDENCE_BUTTON = "evidence"
_STALE_TEXT = "request expired, send /check again"
_START_HINT = "Send /check <mint> to analyze a token."
_IN_PROGRESS_TEXT = "your previous check is still running — please wait for its result"


class BotTransport(Protocol):
    """Межа Bot API: живе й несправжнє втілення взаємозамінні."""

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]: ...
    def send_message(self, chat_id: Any, text: str, reply_markup: Any = None) -> dict[str, Any]: ...
    def send_photo(self, chat_id: Any, photo: bytes, caption: str, reply_markup: Any = None) -> dict[str, Any]: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...


class HttpxBotTransport:
    """Живий транспорт: сирі POST до `https://api.telegram.org/bot<token>/<method>`."""

    def __init__(self, token: str, *, base_url: str = "https://api.telegram.org",
                 timeout: float = 70.0) -> None:
        if not token or not isinstance(token, str):
            raise ValueError("token: expected non-empty string")
        if timeout <= 60.0:
            raise ValueError("timeout: must exceed the 60s long-poll window")
        self._token = token
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def _call(self, method: str, **payload: Any) -> dict[str, Any]:
        response = self._client.post(f"/bot{self._token}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(f"telegram API error in {method}: {data}")
        return data

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return self._call("getUpdates", offset=offset, timeout=timeout).get("result", [])

    def send_message(self, chat_id: Any, text: str, reply_markup: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", **payload)

    def send_photo(self, chat_id: Any, photo: bytes, caption: str, reply_markup: Any = None) -> dict[str, Any]:
        response = self._client.post(
            f"/bot{self._token}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption,
                  **({"reply_markup": json.dumps(reply_markup)} if reply_markup is not None else {})},
            files={"photo": ("graph.png", photo, "image/png")},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(f"telegram API error in sendPhoto: {data}")
        return data

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)


class JournalTransport:
    """Несправжній транспорт для тестів: черга вхідних, журнал вихідних."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self._updates = list(updates or [])
        self.calls: list[tuple[str, tuple, dict]] = []

    def queue(self, update: dict[str, Any]) -> None:
        self._updates.append(update)

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        batch, self._updates = self._updates, []
        return batch

    def send_message(self, chat_id: Any, text: str, reply_markup: Any = None) -> dict[str, Any]:
        self.calls.append(("send_message", (chat_id, text), {"reply_markup": reply_markup}))
        return {"ok": True}

    def send_photo(self, chat_id: Any, photo: bytes, caption: str, reply_markup: Any = None) -> dict[str, Any]:
        self.calls.append(("send_photo", (chat_id, photo, caption), {"reply_markup": reply_markup}))
        return {"ok": True}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.calls.append(("answer_callback", (callback_id, text), {}))


def _short(address: str, prefix_len: int = 8) -> str:
    return address[:prefix_len] + "..." if len(address) > prefix_len else address


def format_check(doc: dict[str, Any], request_id: str | None) -> tuple[str, Any]:
    """`(caption, reply_markup)`: один рядок ризику, топ-3 кластери, походження, кнопка."""
    if doc.get("error") is not None:
        return _format_error(doc["error"]), None
    lines = [f"risk {doc['risk_score']}/100 — {BAND_WORDS.get(doc['band'], doc['band'])}"]
    for i, cluster in enumerate(doc["clusters"][:3]):
        lines.append(f"#{i + 1}: {len(cluster['wallets'])} wallets, share {cluster['supply_share']}")
    if not doc["clusters"]:
        lines.append("no related groups found")
    prov = doc.get("provenance", {})
    lines.append(f"data: ingest v{prov.get('ingest_config_version')} hubs v{prov.get('hub_config_version')} "
                 f"clusters v{prov.get('cluster_config_version')}, {prov.get('graph_status')}")
    markup = None
    if doc["clusters"] and request_id is not None:
        markup = {"inline_keyboard": [[{"text": _EVIDENCE_BUTTON,
                                       "callback_data": f"evidence:{request_id}"}]]}
    return "\n".join(lines), markup


def _rate_limited_text(retry_after_seconds: int) -> str:
    return f"too many checks — try again in {retry_after_seconds} s"


def _format_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "could not analyze the token"
    kind = error.get("kind", "")
    if kind == "invalid_address":
        return "doesn't look like a Solana address — check the mint"
    if kind == "token_not_found":
        return "token not found"
    retry = error.get("retry_after_seconds")
    if type(retry) is int and retry >= 1:  # без коректного числа не вигадуємо його: нижче чесний загальний текст
        if kind == "busy":
            return f"service is busy with other analyses — try again in ~{retry} s"
        if kind == "rate_limited":
            return _rate_limited_text(retry)
    detail = error.get("detail", "")
    return f"incomplete data: {detail}" if detail else "could not analyze the token"


def format_evidence(doc: dict[str, Any], limit: int) -> list[str]:
    """Повний текст доказів, нарізаний шматками ≤ `limit` (R-8)."""
    blocks: list[str] = []
    for i, cluster in enumerate(doc.get("clusters", [])):
        rows = [f"Cluster #{i + 1} (share {cluster['supply_share']}, "
                f"confidence {cluster['confidence']})"]
        for ev in cluster["evidence"]:
            shown = [_short(s) for s in ev["source"][:4]]
            if len(ev["source"]) > 4:
                shown.append(f"+{len(ev['source']) - 4} more")
            window = ev["window"]
            rows.append(f"• {ev['type']}: {', '.join(shown)}, window {window['start']}–{window['end']} ({window['basis']})")
        blocks.append("\n".join(rows))
    full = "\n\n".join(blocks)
    if len(full) <= limit:
        return [full] if full else ["no evidence"]
    total = sum(len(c["evidence"]) for c in doc.get("clusters", []))
    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    for line in full.split("\n"):
        add = len(line) + (1 if current else 0)
        if current and cur_len + add > limit:
            chunks.append("\n".join(current))
            current = []
            cur_len = 0
        current.append(line)
        cur_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    in_first = chunks[0].count("• ")
    chunks[0] += f"\n…and {total - in_first} more pieces of evidence (continued)"
    return chunks


_MAX_REQUESTS = 1000  # межа реєстру callback-запитів; старі витісняються (шлях «застарів»)
_PLAIN_REGISTRY_LOCK = threading.Lock()  # для голого `dict`, який передали напряму в `handle_check`


class RequestRegistry(dict):
    """Реєстр `request_id -> mint` для callback «докази»: потокобезпечний, обмежений (R-9).

    Воркери `/check` пишуть сюди паралельно з потоком опитування, що читає (callback), тож додавання,
    витіснення найстарішого й читання йдуть під одним замком; `request_id` береться з `itertools.count`
    (унікальний і після витіснення, на відміну від `len(реєстр)`). Лічильник росте лише при реєстрації,
    тож до першого витіснення id збігаються з 004 (`<8 символів mint>:<порядковий номер>`).
    """

    def __init__(self, limit: int | None = None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._ids = itertools.count()
        self._limit = _MAX_REQUESTS if limit is None else limit

    def add(self, mint: str) -> str:
        with self._lock:
            request_id = f"{mint[:8]}:{next(self._ids)}"
            self[request_id] = mint
            while len(self) > self._limit:
                self.pop(next(iter(self)))
            return request_id

    def lookup(self, request_id: str) -> str | None:
        with self._lock:
            return dict.get(self, request_id)


def _register(requests: dict[str, str], mint: str) -> str:
    if isinstance(requests, RequestRegistry):
        return requests.add(mint)
    with _PLAIN_REGISTRY_LOCK:  # голий dict (тести 004, прямі виклики): поведінка 004
        request_id = f"{mint[:8]}:{len(requests)}"
        requests[request_id] = mint
        return request_id


def _lookup(requests: dict[str, str], request_id: str) -> str | None:
    if isinstance(requests, RequestRegistry):
        return requests.lookup(request_id)
    return requests.get(request_id)


def _is_busy(error: Any) -> bool:
    """Відмова глобальної межі важких аналізів: лише за `error.kind == "busy"` (не за текстом)."""
    return isinstance(error, dict) and error.get("kind") == "busy"


def handle_check(text: str, service, transport: BotTransport, chat_id: Any,
                 requests: dict[str, str], *, render=None, on_busy=None) -> str | None:
    """Обробка `/check <mint>`: фото + caption + кнопка. Повертає `request_id` або `None`.

    `render` — виклик `render_png(doc, config)` (інжектований точкою входу); без нього
    надсилається лише текст із кнопкою. `on_busy` — виклик без аргументів, коли відповідь сервісу —
    `busy` (аналіз не запускався): викликається ДО відповіді користувачу й не повинен кидати.
    """
    parts = text.strip().split()
    if len(parts) < 2 or not parts[0].startswith("/check"):
        transport.send_message(chat_id, _START_HINT)
        return None
    mint = parts[1]
    try:
        doc = service.analyze(mint, progress=lambda msg: transport.send_message(chat_id, msg))
    except Exception as exc:
        transport.send_message(chat_id, f"analysis error: {type(exc).__name__}")
        return None
    if doc.get("error") is not None:
        if on_busy is not None and _is_busy(doc["error"]):
            on_busy()
        caption, _ = format_check(doc, None)
        transport.send_message(chat_id, caption)
        return None
    request_id = _register(requests, mint)
    caption, markup = format_check(doc, request_id)
    if render is None:
        transport.send_message(chat_id, caption, reply_markup=markup)
    else:
        transport.send_photo(chat_id, render(doc).png, caption, reply_markup=markup)
    return request_id


def handle_evidence(callback_data: str, callback_id: str, service, transport: BotTransport,
                    chat_id: Any, requests: dict[str, str], *, limit: int) -> None:
    """Callback кнопки «докази»: повний текст або відповідь про застарілий запит.

    `limit` — завжди з `delivery.yaml` (FR-004-11); без умовчання, щоб зашите число
    не могло мовчки підмінити версіоновану сталу.
    """
    transport.answer_callback(callback_id)
    kind, _, request_id = callback_data.partition(":")
    mint = _lookup(requests, request_id) if kind == "evidence" else None
    if mint is None:
        transport.send_message(chat_id, _STALE_TEXT)
        return
    try:
        doc = service.analyze(mint)
    except Exception as exc:
        transport.send_message(chat_id, f"analysis error: {type(exc).__name__}")
        return
    if doc.get("error") is not None:
        transport.send_message(chat_id, _format_error(doc["error"]))
        return
    for chunk in format_evidence(doc, limit):
        transport.send_message(chat_id, chunk)


def _spawn_thread(fn) -> None:
    """Умовчання `spawn`: daemon-потік (не тримає процес при виході)."""
    threading.Thread(target=fn, name="unmask-check", daemon=True).start()


class _GateTicket:
    """Один допуск воріт -> рівно один `gate.leave(user)`, що б не сталося між `ok` і кінцем воркера.

    Стани: `pending` (допущено, воркер ще не стартував) -> `running` -> `done`, або `pending` -> `cancelled`.
    Воркер, що почався, сам відповідає за `leave` (у `finally`); якщо `spawn` кинув до його старту, `abort`
    звільняє користувача негайно, а запізнілий запуск покинутого воркера стає no-op (`begin` → `False`):
    він ні не аналізує, ні не знімає позначку з наступного допуску того ж користувача.

    Повернення квоти (`refund`): лише поки воркер `running` (отже, до `leave` і лише для СВОГО допуску), лише
    якщо допуск списав квоту (`counts=True`) і не більше одного разу. Запізніле повернення (після `finish`/
    `abort`, або воркера, що не стартував) — no-op: воно не зачепить мітку наступного допуску того ж користувача.
    Збій самого повернення не перетворює чесну відповідь `busy` на «analysis error».
    """

    __slots__ = ("_gate", "_user", "_counts", "_lock", "_state", "_refunded")

    def __init__(self, gate, user, *, counts: bool = False) -> None:
        self._gate = gate
        self._user = user
        self._counts = counts is True  # рахований допуск лише за явним `True`
        self._lock = threading.Lock()
        self._state = "pending"
        self._refunded = False

    def begin(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            return True

    def refund(self) -> None:
        with self._lock:
            if self._state != "running" or not self._counts or self._refunded:
                return
            self._refunded = True  # позначаємо до виклику: збій не дає другої спроби
            try:
                self._gate.refund(self._user)
            except Exception:  # квота не повернена — як до T-174; відповідь користувачу важливіша
                pass

    def finish(self) -> None:
        with self._lock:
            if self._state == "running":
                self._state = "done"
                self._gate.leave(self._user)

    def abort(self) -> None:
        with self._lock:
            if self._state == "pending":
                self._state = "cancelled"
                self._gate.leave(self._user)


def _id_of(container: Any) -> Any:
    """`id` з `from`/`chat`, якщо це `int`/`str` (не `bool`); інакше `None`."""
    value = container.get("id") if isinstance(container, dict) else None
    return value if type(value) in (int, str) else None


def _user_key(message: dict[str, Any]) -> Any:
    """Ключ користувача: `message.from.id`, за відсутності — `message.chat.id` (контракт overload.md)."""
    user = _id_of(message.get("from"))
    return user if user is not None else _id_of(message.get("chat"))


def _check_gated(text: str, message: dict[str, Any], service, transport: BotTransport, chat_id: Any,
                 requests: dict[str, str], render, gate, spawn) -> None:
    """`/check` з воротами: допуск — тут (потік опитування), аналіз — у воркері через `spawn`."""
    parts = text.strip().split()
    if len(parts) < 2:  # `/check` без mint — не перевірка: підказка, як у 004, без воріт і аналізу
        handle_check(text, service, transport, chat_id, requests, render=render)
        return
    user = _user_key(message)
    if user is None:  # ні від кого, ні з якого чату: нема ключа, щоб лімітувати, і куди відповісти
        return
    counts = bool(service.needs_analysis(parts[1]))  # лише валідний холодний mint їсть вікно; тип — строго bool
    decision = gate.try_enter(user, counts=counts)
    if decision.kind == "in_progress":
        transport.send_message(chat_id, _IN_PROGRESS_TEXT)
        return
    if decision.kind == "rate_limited":
        transport.send_message(chat_id, _rate_limited_text(decision.retry_after_seconds))
        return
    if decision.kind != "ok":
        raise ValueError(f"unexpected gate decision: {decision.kind!r}")  # не мовчки допускати невідоме

    ticket = _GateTicket(gate, user, counts=counts)

    def worker() -> None:
        if not ticket.begin():
            return
        try:
            try:
                handle_check(text, service, transport, chat_id, requests, render=render,
                             on_busy=ticket.refund)
            except Exception as exc:  # як у 004: збій перевірки не лишає користувача без відповіді
                try:
                    if chat_id is not None:
                        transport.send_message(chat_id, f"analysis error: {type(exc).__name__}")
                except Exception:
                    pass
        finally:
            ticket.finish()

    try:
        spawn(worker)
    except BaseException:  # напр. RuntimeError: can't start new thread; воркер не стартував -> звільнити
        ticket.abort()
        raise


def run_polling(transport: BotTransport, service, *, render=None, evidence_limit: int,
                stop_after: int | None = None,
                sleep=None, gate=None, spawn=None) -> dict[str, str]:
    """Цикл опитування. `stop_after=N` — обробити N апдейтів і повернути реєстр (для тестів).

    `evidence_limit` — з `delivery.yaml` (без умовчання, FR-004-11). Збій `get_updates`
    (таймаут long-poll, мережа, 5xx) не вбиває цикл: пауза з нарощуванням і повтор.

    `gate` (`UserGate` з `Protection.users`) вмикає ліміти користувача (FR-008-07): без нього — послідовна
    поведінка 004 без змін, `spawn` ігнорується. `spawn(fn)` запускає воркер `/check` (умовчання — daemon-потік;
    у тестах — синхронний або відкладений виклик). Callback «докази» ніколи не лімітується.
    """
    if sleep is None:
        sleep = time.sleep
    if spawn is None:
        spawn = _spawn_thread
    requests = RequestRegistry()
    offset = 0
    processed = 0
    backoff = 1.0
    while True:
        try:
            updates = transport.get_updates(offset, 60)
        except Exception:
            sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)
            if stop_after is not None and not transport_has_more(transport):
                return requests
            continue
        backoff = 1.0
        for update in updates:
            offset = max(offset, update.get("update_id", 0) + 1)
            try:
                if "callback_query" in update:
                    query = update["callback_query"]
                    handle_evidence(query.get("data", ""),
                                    query.get("id", ""),
                                    service, transport,
                                    query.get("message", {}).get("chat", {}).get("id"),
                                    requests, limit=evidence_limit)
                elif "message" in update:
                    message = update["message"]
                    text = message.get("text", "")
                    chat_id = message.get("chat", {}).get("id")
                    if text.startswith("/check"):
                        if gate is None:
                            handle_check(text, service, transport, chat_id, requests, render=render)
                        else:
                            _check_gated(text, message, service, transport, chat_id, requests,
                                         render, gate, spawn)
                    elif text.startswith("/start"):
                        transport.send_message(chat_id, _START_HINT)
            except Exception as exc:
                try:
                    chat = update.get("message", {}).get("chat", {}).get("id")
                    if chat is not None:
                        transport.send_message(chat, f"analysis error: {type(exc).__name__}")
                except Exception:
                    pass
            processed += 1
            if stop_after is not None and processed >= stop_after:
                return requests
        if stop_after is not None and not transport_has_more(transport):
            return requests


def transport_has_more(transport: BotTransport) -> bool:
    """Чи лишилися апдейти в черзі (лише журнальний транспорт; живий завжди опитується)."""
    queue = getattr(transport, "_updates", None)
    return bool(queue)
