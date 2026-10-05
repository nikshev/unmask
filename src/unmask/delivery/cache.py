# impl: FR-004-06
"""Кеш доставки: `mint → документ` у памʼяті процесу + single-flight (R-5).

Інвалідації немає; перезапуск чистить кеш. Відмови не кешуються (рішення викликача).
Потоко-безпечно: одночасні запити одного нового токена чекають один `build`.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Hashable

__all__ = ["DeliveryCache"]


class DeliveryCache:
    """Незмінні документи за точною адресою токена."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._docs: dict[str, Any] = {}
        self._inflight: dict[str, tuple[threading.Event, list]] = {}

    def get(self, mint: str) -> Any | None:
        """Збережений документ або `None` (промах — не виняток)."""
        with self._lock:
            return self._docs.get(mint)

    def store(self, mint: str, doc: Any) -> None:
        """Покласти документ; перезаписує мовчки (той самий вміст — та сама відповідь)."""
        with self._lock:
            self._docs[mint] = doc

    def single_flight(self, mint: str, build: Callable[[], Any]) -> Any:
        """Один `build` на одночасні запити: решта чекають і отримують той самий документ.

        Виняток `build` прокидається всім чекаючим і не кешується.
        """
        with self._lock:
            if mint in self._docs:
                return self._docs[mint]
            slot = self._inflight.get(mint)
            if slot is None:
                slot = (threading.Event(), [])
                self._inflight[mint] = slot
                owner = True
            else:
                owner = False
        event, errors = slot
        if not owner:
            event.wait()
            with self._lock:
                if mint in self._docs:
                    return self._docs[mint]
            raise errors[0]
        try:
            doc = build()
        except BaseException as exc:
            errors.append(exc)
            with self._lock:
                self._inflight.pop(mint, None)
                event.set()
            raise
        with self._lock:
            self._docs[mint] = doc
            self._inflight.pop(mint, None)
            event.set()
        return doc
