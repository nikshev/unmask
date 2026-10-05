# impl: FR-004-03, FR-004-04, FR-004-07, FR-004-09
"""Telegram-бот доставки: polling-цикл, `/check`, callback «докази» (R-2, контракт `bot-messages.md`).

Транспорт — межа (`BotTransport`): живий на сирих викликах Bot API через `httpx`,
несправжній (журнальний) — у тестах. Форматування — дослівно за контрактом.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

__all__ = [
    "BotTransport",
    "HttpxBotTransport",
    "JournalTransport",
    "format_check",
    "format_evidence",
    "handle_check",
    "handle_evidence",
    "run_polling",
]

BAND_WORDS = {
    "clean": "чисто",
    "suspicious": "підозріло",
    "high_concentration": "висока концентрація",
    "insufficient_data": "недостатньо даних",
}

_EVIDENCE_BUTTON = "докази"
_STALE_TEXT = "запит застарів, надішліть /check ще раз"
_START_HINT = "Надішліть /check <mint>, щоб перевірити токен."


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
    lines = [f"ризик {doc['risk_score']}/100 — {BAND_WORDS.get(doc['band'], doc['band'])}"]
    for i, cluster in enumerate(doc["clusters"][:3]):
        lines.append(f"#{i + 1}: {len(cluster['wallets'])} гаманців, частка {cluster['supply_share']}")
    if not doc["clusters"]:
        lines.append("повʼязаних груп не знайдено")
    prov = doc.get("provenance", {})
    lines.append(f"дані: ingest v{prov.get('ingest_config_version')} hubs v{prov.get('hub_config_version')} "
                 f"clusters v{prov.get('cluster_config_version')}, {prov.get('graph_status')}")
    markup = None
    if doc["clusters"] and request_id is not None:
        markup = {"inline_keyboard": [[{"text": _EVIDENCE_BUTTON,
                                       "callback_data": f"evidence:{request_id}"}]]}
    return "\n".join(lines), markup


def _format_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "не вдалося проаналізувати токен"
    kind = error.get("kind", "")
    if kind == "invalid_address":
        return "не схоже на адресу Solana — перевірте mint"
    if kind == "token_not_found":
        return "токен не знайдено"
    detail = error.get("detail", "")
    return f"даних неповно: {detail}" if detail else "не вдалося проаналізувати токен"


def format_evidence(doc: dict[str, Any], limit: int) -> list[str]:
    """Повний текст доказів, нарізаний шматками ≤ `limit` (R-8)."""
    blocks: list[str] = []
    for i, cluster in enumerate(doc.get("clusters", [])):
        rows = [f"Кластер #{i + 1} (частка {cluster['supply_share']}, "
                f"впевненість {cluster['confidence']})"]
        for ev in cluster["evidence"]:
            shown = [_short(s) for s in ev["source"][:4]]
            if len(ev["source"]) > 4:
                shown.append(f"+{len(ev['source']) - 4} ще")
            window = ev["window"]
            rows.append(f"• {ev['type']}: {', '.join(shown)}, вікно {window['start']}–{window['end']} ({window['basis']})")
        blocks.append("\n".join(rows))
    full = "\n\n".join(blocks)
    if len(full) <= limit:
        return [full] if full else ["доказів немає"]
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
    chunks[0] += f"\n…і ще {total - in_first} доказів (далі)"
    return chunks


def handle_check(text: str, service, transport: BotTransport, chat_id: Any,
                 requests: dict[str, str], *, render=None) -> str | None:
    """Обробка `/check <mint>`: фото + caption + кнопка. Повертає `request_id` або `None`.

    `render` — виклик `render_png(doc, config)` (інжектований точкою входу); без нього
    надсилається лише текст із кнопкою.
    """
    parts = text.strip().split()
    if len(parts) < 2 or not parts[0].startswith("/check"):
        transport.send_message(chat_id, _START_HINT)
        return None
    mint = parts[1]
    try:
        doc = service.analyze(mint)
    except Exception as exc:
        transport.send_message(chat_id, f"помилка аналізу: {type(exc).__name__}")
        return None
    if doc.get("error") is not None:
        caption, _ = format_check(doc, None)
        transport.send_message(chat_id, caption)
        return None
    request_id = f"{mint[:8]}:{len(requests)}"
    requests[request_id] = mint
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
    mint = requests.get(request_id) if kind == "evidence" else None
    if mint is None:
        transport.send_message(chat_id, _STALE_TEXT)
        return
    try:
        doc = service.analyze(mint)
    except Exception as exc:
        transport.send_message(chat_id, f"помилка аналізу: {type(exc).__name__}")
        return
    if doc.get("error") is not None:
        transport.send_message(chat_id, _format_error(doc["error"]))
        return
    for chunk in format_evidence(doc, limit):
        transport.send_message(chat_id, chunk)


_MAX_REQUESTS = 1000  # межа реєстру callback-запитів; старі витісняються (шлях «застарів»)


def run_polling(transport: BotTransport, service, *, render=None, evidence_limit: int,
                stop_after: int | None = None,
                sleep=None) -> dict[str, str]:
    """Цикл опитування. `stop_after=N` — обробити N апдейтів і повернути реєстр (для тестів).

    `evidence_limit` — з `delivery.yaml` (без умовчання, FR-004-11). Збій `get_updates`
    (таймаут long-poll, мережа, 5xx) не вбиває цикл: пауза з нарощуванням і повтор.
    """
    if sleep is None:
        sleep = time.sleep
    requests: dict[str, str] = {}
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
                        if handle_check(text, service, transport, chat_id, requests,
                                        render=render) is not None:
                            while len(requests) > _MAX_REQUESTS:
                                requests.pop(next(iter(requests)))
                    elif text.startswith("/start"):
                        transport.send_message(chat_id, _START_HINT)
            except Exception as exc:
                try:
                    chat = update.get("message", {}).get("chat", {}).get("id")
                    if chat is not None:
                        transport.send_message(chat, f"помилка аналізу: {type(exc).__name__}")
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
