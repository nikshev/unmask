# impl: FR-004-01, FR-004-07, FR-004-08
"""HTTP-шар доставки: чисті функції маршруту й сокетна обгортка (R-1).

Жодної авторизації: гілок 401/403 не існує (FR-004-08). Логіка тестується без сокетів.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit, unquote

from unmask.delivery.report import to_json

__all__ = ["route", "handle_token_request", "handle_health", "serve"]

_TOKEN_PREFIX = "/api/token/"
_HEALTH_PATH = "/healthz"

_ERROR_STATUS = {
    "invalid_address": 400,
    "token_not_found": 400,
}


def route(method: str, path: str) -> str | None:
    """Маршрут запиту: `"token"`, `"health"` або `None` (невідомий шлях). Метод перевіряє викликач."""
    target = urlsplit(path).path
    if target == _HEALTH_PATH:
        return "health"
    if target.startswith(_TOKEN_PREFIX):
        remainder = target[len(_TOKEN_PREFIX):]
        if remainder and "/" not in remainder:
            return "token"
    return None


def handle_health() -> tuple[int, dict[str, Any]]:
    """Проба живості оператора."""
    return 200, {"ok": True}


def handle_token_request(mint: str, service) -> tuple[int, dict[str, Any]]:
    """`(статус, документ)`: 200 з відповіддю 004.1 або 4xx з документом-помилкою."""
    doc = service.analyze(unquote(mint))
    error = doc.get("error")
    if error is None:
        return 200, doc
    kind = error.get("kind", "invalid_address") if isinstance(error, dict) else "invalid_address"
    return _ERROR_STATUS.get(kind, 400), doc


class _Handler(BaseHTTPRequestHandler):
    """Тонка сокетна обгортка; у тестах не використовується (гард мережі)."""

    service = None  # призначає serve()

    def log_message(self, *args: Any) -> None:  # тихіше за умовчанням
        pass

    def _send(self, status: int, doc: dict[str, Any]) -> None:
        body = to_json(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        name = route("GET", self.path)
        if name == "health":
            status, doc = handle_health()
        elif name == "token":
            mint = urlsplit(self.path).path[len(_TOKEN_PREFIX):]
            status, doc = handle_token_request(mint, self.service)
        else:
            status, doc = 404, {"mint": "", "error": {"kind": "not_found", "detail": "unknown path"}}
        self._send(status, doc)

    def _method_not_allowed(self) -> None:
        self._send(405, {"mint": "", "error": {"kind": "method_not_allowed", "detail": "use GET"}})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


def serve(service, port: int) -> None:
    """Блокувальний HTTP-сервер (викликає точка входу; у тестах не викликається)."""
    _Handler.service = service
    with ThreadingHTTPServer(("127.0.0.1", port), _Handler) as server:
        server.serve_forever()
