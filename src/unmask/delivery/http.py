# impl: FR-004-01, FR-004-07, FR-004-08, FR-008-01, FR-008-03, FR-008-04, FR-008-05, FR-008-09, FR-008-10
"""HTTP-шар доставки: чисті функції маршруту й сокетна обгортка (R-1).

Жодної авторизації: гілок 401/403 не існує (FR-004-08). Логіка тестується без сокетів.

Фіча 008 (контракт `overload.md`): `handle_get` лічить кожен `GET /api/token/{mint}` у ліміті
запитів на адресу клієнта ДО виклику сервісу (кеш-хіт і невалідний mint теж; відмова — `429`, сервіс
не викликається). `/healthz`, `404`, `405` поза лімітом. `503 busy` приходить документом від сервісу
(слот важкого аналізу). Ключ ліміту — лише адреса клієнта (FR-008-04): з заголовків читається єдиний
`X-Forwarded-For`, і то лише від довіреного проксі. Без `protection` відповіді як до фічі (FR-008-09).
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit, unquote

from unmask.delivery.report import to_json

__all__ = ["route", "handle_token_request", "handle_health", "handle_get", "client_address", "serve"]

_TOKEN_PREFIX = "/api/token/"
_HEALTH_PATH = "/healthz"

_ERROR_STATUS = {
    "invalid_address": 400,
    "token_not_found": 400,
    "rate_limited": 429,
    "busy": 503,
}

_RETRY_AFTER_KINDS = ("rate_limited", "busy")


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
    """`(статус, документ)`: 200 з відповіддю 004.1, 4xx з документом-помилкою, 5xx при дефекті.

    Непередбачений виняток конвеєра дає 500 з типом винятку (без стеку й без значень);
    процес і зʼєднання лишаються живими.
    """
    try:
        doc = service.analyze(unquote(mint))
    except Exception as exc:
        return 500, {"mint": mint, "error": {"kind": "internal", "detail": type(exc).__name__}}
    error = doc.get("error")
    if error is None:
        return 200, doc
    kind = error.get("kind", "invalid_address") if isinstance(error, dict) else "invalid_address"
    return _ERROR_STATUS.get(kind, 400), doc


# Захист від DoS (R-4): розбирається не більше цього числа символів заголовка, ЗПРАВА. Проксі дописує
# справжню адресу в кінець, тож хвіст — єдина довірена частина; довший заголовок обрізається зліва
# по межі ланки (напівобрізаний запис не стає адресою). Усе обрізане лівіше недоступне: якщо
# довірений хвіст вичерпано — результат `peer`, а не прихований лівий клієнт.
_XFF_MAX_LENGTH = 4096

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_MAPPED_PREFIX = 0xFFFF << 32  # ::ffff:0:0/96


def _unmap(addr: _IPAddress) -> _IPAddress:
    """IPv4-mapped IPv6 (`::ffff:a.b.c.d`) → IPv4; решта без змін; scope id відкидається."""
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return mapped
        if addr.scope_id is not None:
            return ipaddress.IPv6Address(int(addr))
    return addr


def _parse_ip(text: str, *, allow_scope: bool) -> _IPAddress | None:
    """Строга IP-адреса без порту, дужок і масок; `None` для будь-чого іншого (сміття)."""
    if not allow_scope and "%" in text:
        return None  # "fe80::1%a", "fe80::1%b" … були б необмеженою кількістю ключів
    try:
        return _unmap(ipaddress.ip_address(text))
    except ValueError:
        return None


def _trusted_networks(trusted: Iterable[object]) -> list[_IPNetwork]:
    """Нормалізувати `trusted` (мережі, адреси чи рядки). Некоректний запис ігнорується: менше довіри."""
    networks: list[_IPNetwork] = []
    for item in trusted:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            continue
    return networks


def _is_trusted(addr: _IPAddress, networks: list[_IPNetwork]) -> bool:
    forms: list[_IPAddress] = [addr]
    if isinstance(addr, ipaddress.IPv4Address):  # довірена мережа може бути записана як ::ffff:a.b.c.d/N
        forms.append(ipaddress.IPv6Address(_MAPPED_PREFIX | int(addr)))
    return any(form in net for net in networks for form in forms)


def client_address(peer: str, forwarded_for: str | None, trusted: Iterable[object]) -> str:
    """Адреса клієнта для ключа ліміту (FR-008-05, R-4): `str(ip_address)`.

    `peer` — адреса TCP-зʼєднання; `forwarded_for` — значення `X-Forwarded-For` (кілька заголовків
    викликач зливає через кому в порядку надходження) або `None`; `trusted` — довірені проксі
    (`ProtectionConfig.trusted_proxies`: рядки IP/CIDR, або вже `ip_network`/`ip_address`).

    Заголовок береться ЛИШЕ коли `peer` належить `trusted` (порожній `trusted` не довіряє нікому).
    Тоді ланцюг розбирається справа наліво: довірені ланки пропускаються, перша недовірена
    валідна IP — клієнт. Сміття (порт, дужки, маска, scope id, невалідний запис) зупиняє розбір:
    результат `peer`. Порожні елементи ігноруються (RFC 9110 §5.6.1). Весь ланцюг довірений або
    заголовка немає → `peer`. IPv4-mapped IPv6 нормалізується до IPv4 (інакше один клієнт мав би
    два ключі, а dual-stack `::ffff:127.0.0.1` не збігався б з довіреним `127.0.0.1`).
    Розбирається не більше `_XFF_MAX_LENGTH` символів заголовка справа (довжина — захист від DoS).
    `peer`, що не є IP, повертається як є; функція не кидає винятків.
    """
    peer_ip = _parse_ip(peer, allow_scope=True)
    if peer_ip is None:
        return peer
    if not forwarded_for:
        return str(peer_ip)
    networks = _trusted_networks(trusted)
    if not _is_trusted(peer_ip, networks):
        return str(peer_ip)

    header = forwarded_for
    if len(header) > _XFF_MAX_LENGTH:
        header = header[-_XFF_MAX_LENGTH:]
        cut = header.find(",")
        header = header[cut + 1:] if cut >= 0 else ""
    while header:
        header, _, entry = header.rpartition(",")
        entry = entry.strip()
        if not entry:
            continue
        addr = _parse_ip(entry, allow_scope=False)
        if addr is None:
            return str(peer_ip)
        if not _is_trusted(addr, networks):
            return str(addr)
    return str(peer_ip)


def _retry_after_headers(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """`[("Retry-After", N)]` для документів перевантаження (`N` = `retry_after_seconds` тіла), інакше `[]`."""
    error = doc.get("error")
    if isinstance(error, dict) and error.get("kind") in _RETRY_AFTER_KINDS:
        seconds = error.get("retry_after_seconds")
        if type(seconds) is int and seconds >= 1:
            return [("Retry-After", str(seconds))]
    return []


def handle_get(path: str, peer: str, forwarded_for: str | None, service,
               protection=None) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    """Чиста обробка `GET`: `(статус, документ, заголовки відповіді)` (контракт `overload.md`).

    `peer` — адреса TCP-зʼєднання; `forwarded_for` — значення `X-Forwarded-For` (кілька заголовків
    злито комою) або `None`. `protection` (за умовчанням `None` = поведінка до фічі 008) — об'єкт з
    `.api` (`hit(client) -> Decision`) і `.trusted_proxies`.

    Токен-гілка: `client_address` → `protection.api.hit` → відмова `429` (сервіс не викликається,
    невалідний mint теж лічиться) → інакше `handle_token_request` без змін. `Retry-After` додається лише
    до `rate_limited` і `busy`; `/healthz`, `404` поза лімітом.
    """
    name = route("GET", path)
    if name == "health":
        status, doc = handle_health()
        return status, doc, []
    if name != "token":
        return 404, {"mint": "", "error": {"kind": "not_found", "detail": "unknown path"}}, []
    mint = urlsplit(path).path[len(_TOKEN_PREFIX):]
    if protection is not None:
        client = client_address(peer, forwarded_for, protection.trusted_proxies)
        decision = protection.api.hit(client)
        if not decision.allowed:
            seconds = decision.retry_after_seconds
            doc = {"mint": mint, "error": {
                "kind": "rate_limited",
                "detail": f"too many requests from this client, retry after {seconds} s",
                "retry_after_seconds": seconds,
            }}
            return _ERROR_STATUS["rate_limited"], doc, _retry_after_headers(doc)
    status, doc = handle_token_request(mint, service)
    return status, doc, _retry_after_headers(doc)


class _Handler(BaseHTTPRequestHandler):
    """Тонка сокетна обгортка; у тестах не використовується (гард мережі)."""

    service = None  # призначає serve() (у підкласі)
    protection = None  # те саме; `None` — поведінка до фічі 008

    def log_message(self, *args: Any) -> None:  # тихіше за умовчанням
        pass

    def _send(self, status: int, doc: dict[str, Any],
              headers: Iterable[tuple[str, str]] = ()) -> None:
        body = to_json(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        forwarded = self.headers.get_all("X-Forwarded-For")
        status, doc, headers = handle_get(
            self.path, self.client_address[0], ", ".join(forwarded) if forwarded else None,
            self.service, self.protection)
        self._send(status, doc, headers)

    def _method_not_allowed(self) -> None:
        self._send(405, {"mint": "", "error": {"kind": "method_not_allowed", "detail": "use GET"}})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


def serve(service, port: int, *, host: str = "0.0.0.0", protection=None) -> None:
    """Блокувальний HTTP-сервер (викликає точка входу; у тестах не викликається).

    Сервіс і регулятор привʼязуються до підкласу обробника: глобальний стан `_Handler` не чіпається.
    """
    handler = type("_BoundHandler", (_Handler,), {"service": service, "protection": protection})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.serve_forever()
