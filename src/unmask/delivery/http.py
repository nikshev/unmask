# impl: FR-004-01, FR-004-07, FR-004-08, FR-008-05
"""HTTP-шар доставки: чисті функції маршруту й сокетна обгортка (R-1).

Жодної авторизації: гілок 401/403 не існує (FR-004-08). Логіка тестується без сокетів.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit, unquote

from unmask.delivery.report import to_json

__all__ = ["route", "handle_token_request", "handle_health", "client_address", "serve"]

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
    with ThreadingHTTPServer(("0.0.0.0", port), _Handler) as server:
        server.serve_forever()
