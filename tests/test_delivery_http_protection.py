# verifies: FR-008-01, FR-008-03, FR-008-04, FR-008-09, FR-008-10
"""HTTP-шар під захистом (T-166; контракт `overload.md`, схема 008.1; research R-1, R-4).

Чиста `handle_get` перевіряється без сокетів; `_Handler.do_GET` — через підроблене зʼєднання
(`FakeRequest`: байти запиту всередині, байти відповіді назовні), тож мережі немає навіть на
loopback (гард `conftest`). Регулятор `protection` — мінімальний дубль з тими ж полями, що в
`Protection` (T-167): `.api` і `.trusted_proxies`. Годинник ліміту — `ManualClock`, `sleep` немає.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

from test_delivery_service import RecordedIngest, _mint_of, _real_services
from test_delivery_service_slots import RETRY, WAIT, Rig
from unmask.delivery import http as http_mod
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.http import handle_health, handle_token_request
from unmask.delivery.limits import SlidingWindowLimiter
from unmask.delivery.report import to_json
from unmask.delivery.service import DeliveryService
from unmask.ingest.model import RejectKind, Rejection

ROOT = Path(__file__).resolve().parents[1]
HTTP_PY = ROOT / "src" / "unmask" / "delivery" / "http.py"
SCHEMA_PATH = (ROOT / "specs" / "008-api-protection-ops" / "contracts"
               / "overload-error.schema.json")
REAL = ROOT / "tests" / "fixtures" / "real"

GOOD = "11111111111111111111111111111111"  # валідна base58-адреса (32 байти)
OTHER = "So11111111111111111111111111111111111111112"
PATH = "/api/token/"
RESULT_FIELDS = {"risk_score", "band", "clusters", "band_reasons", "provenance", "evidence"}


# --------------------------------------------------------------------------- дублі


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@dataclass(frozen=True)
class FakeProtection:
    """Дубль `Protection` з тими ж полями, які читає HTTP-шар."""

    api: object
    trusted_proxies: tuple = ()


class SpyLimiter:
    """Обгортка `SlidingWindowLimiter`: журнал ключів `hit` (рахує, ким і скільки разів)."""

    def __init__(self, limit: int = 30, window: float = 60, clock=None) -> None:
        self.clock = clock if clock is not None else ManualClock()
        self.inner = SlidingWindowLimiter(limit, window, self.clock)
        self.keys: list = []

    def hit(self, key):
        self.keys.append(key)
        return self.inner.hit(key)


class StubService:
    """Сервіс-дубль: рахує виклики, віддає успішний документ (без `error`)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, mint: str, **_kw):
        self.calls.append(mint)
        return {"mint": mint, "error": None, "risk_score": 7, "band": "low", "clusters": []}


class Exploding:
    def analyze(self, mint: str, **_kw):
        raise RuntimeError("defect")


def _prot(limit: int = 30, window: float = 60, trusted: tuple = (), clock=None):
    spy = SpyLimiter(limit, window, clock)
    return FakeProtection(api=spy, trusted_proxies=trusted), spy


def get(path: str, service, protection=None, *, peer: str = "203.0.113.9",
        xff: str | None = None):
    return http_mod.handle_get(path, peer, xff, service, protection)


def _validator():
    assert Draft202012Validator is not None, "jsonschema is required for schema tests"
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- сокет-дубль


class FakeRequest:
    """Зʼєднання для `StreamRequestHandler`: читає готові байти, збирає відповідь у `sent`."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.sent = bytearray()

    def makefile(self, mode: str, bufsize: int = -1):
        return io.BytesIO(self._raw)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:  # pragma: no cover - сумісність
        pass


def roundtrip(service, protection, *, method: str = "GET", path: str = "/healthz",
              peer: str = "203.0.113.9", headers: list[tuple[str, str]] = ()):
    """Один запит крізь справжній `_Handler`: `(status, [(name, value)…], body bytes)`."""
    cls = type("TestHandler", (http_mod._Handler,), {"service": service, "protection": protection})
    lines = [f"{method} {path} HTTP/1.1", "Host: unmask.test"]
    lines += [f"{k}: {v}" for k, v in headers]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    request = FakeRequest(raw)
    cls(request, (peer, 55555), object())
    head, _, body = bytes(request.sent).partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    pairs = [tuple(line.split(": ", 1)) for line in header_lines]
    return int(status_line.split()[1]), pairs, body


def _names(pairs) -> list[str]:
    return [k.lower() for k, _ in pairs]


def _one(pairs, name: str) -> str:
    values = [v for k, v in pairs if k.lower() == name.lower()]
    assert len(values) == 1, (name, values)
    return values[0]


# --------------------------------------------------------------------------- Rig-хелпери


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


def _hold(rig: Rig, label: str, threads: list[threading.Thread]):
    gate = rig.ingest.block(rig.mint(label))
    thread, box = rig.background(label)
    threads.append(thread)
    assert rig.ingest.entered[rig.mint(label)].wait(WAIT)
    return gate, thread, box


# --------------------------------------------------------------------------- рядок T-166


def test_over_limit_gets_429_with_retry_after() -> None:
    protection, spy = _prot(limit=2, window=60)
    service = StubService()
    clock = spy.clock
    assert get(PATH + GOOD, service, protection)[0] == 200          # t=0
    clock.advance(10)
    assert get(PATH + GOOD, service, protection)[0] == 200          # t=10
    clock.advance(15)                                               # t=25: найстаріша мітка 0
    status, doc, headers = get(PATH + GOOD, service, protection)
    assert status == 429
    assert doc == {"mint": GOOD, "error": {
        "kind": "rate_limited",
        "detail": "too many requests from this client, retry after 35 s",
        "retry_after_seconds": 35}}
    assert headers == [("Retry-After", "35")]
    assert len(service.calls) == 2                                  # сервіс на 429 не викликався
    clock.advance(15)                                               # t=40: чесно обчислене 20, не стала
    status, doc, headers = get(PATH + GOOD, service, protection)
    assert (status, headers) == (429, [("Retry-After", "20")])
    assert doc["error"]["retry_after_seconds"] == 20
    clock.advance(20)                                               # t=60: мітка 0 випала з вікна
    assert get(PATH + GOOD, service, protection)[0] == 200
    assert len(service.calls) == 3
    # відмови не записуються: вікно не продовжилось (мітки 10 і 60 → наступний через ceil(10+60-60)=10)
    status, _, headers = get(PATH + GOOD, service, protection)
    assert (status, headers) == (429, [("Retry-After", "10")])


def test_under_limit_client_never_refused() -> None:
    protection, spy = _prot(limit=5, window=60)
    service = StubService()
    for _window in range(20):                  # 20 вікон по рівно 5 запитів — жодної відмови
        for _ in range(5):
            status, doc, headers = get(PATH + GOOD, service, protection)
            assert status == 200 and headers == [] and doc["error"] is None
            spy.clock.advance(11)
        spy.clock.advance(5)                   # 5*11+5 = 60: мітки нового вікна не нашаровуються
    assert len(service.calls) == 100


def test_invalid_mint_counts_toward_request_limit_but_returns_400() -> None:
    class Rejecting:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, mint: str):
            self.calls += 1
            return Rejection(kind=RejectKind.INVALID_ADDRESS, mint=mint, detail="bad base58")

    ingest = Rejecting()
    graph, clusters = _real_services()
    service = DeliveryService(ingest, graph, clusters, DeliveryCache())
    protection, spy = _prot(limit=2)
    for _ in range(2):                         # у межах ліміту 400 лишається 400
        status, doc, headers = get(PATH + "!!!", service, protection)
        assert status == 400 and doc["error"]["kind"] == "invalid_address" and headers == []
    assert len(spy.keys) == 2                  # невалідний mint порахований
    status, doc, headers = get(PATH + "!!!", service, protection)
    assert status == 429 and doc["error"]["kind"] == "rate_limited"
    assert doc["mint"] == "!!!" and len(headers) == 1
    assert ingest.calls == 2                   # на 429 ядро не викликалось
    # і валідний mint з тієї ж адреси вже теж відхилено лімітом
    assert get(PATH + GOOD, service, protection)[0] == 429


def test_cache_hits_count_toward_request_limit() -> None:
    mint, path = _mint_of("ins4")
    ingest = RecordedIngest({mint: path})
    graph, clusters = _real_services()
    service = DeliveryService(ingest, graph, clusters, DeliveryCache())
    protection, spy = _prot(limit=3)
    first = get(PATH + mint, service, protection)
    assert first[0] == 200 and ingest.calls == [mint]
    for _ in range(2):
        again = get(PATH + mint, service, protection)
        assert again[0] == 200 and again[1] == first[1]       # кеш-хіт, те саме тіло
    assert ingest.calls == [mint]                              # збір один
    status, doc, _ = get(PATH + mint, service, protection)    # 4-й запит з кешу — рахується
    assert status == 429 and doc["error"]["kind"] == "rate_limited"
    assert spy.keys == ["203.0.113.9"] * 4
    assert "risk_score" not in doc
    assert ingest.calls == [mint]


def test_healthz_never_limited() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    assert get(PATH + GOOD, service, protection)[0] == 200    # єдиний дозволений запит спожито
    assert get(PATH + GOOD, service, protection)[0] == 429
    for _ in range(1000):
        assert get("/healthz", service, protection) == (200, {"ok": True}, [])
    assert get("/healthz?probe=1", service, protection) == (200, {"ok": True}, [])
    assert len(spy.keys) == 2                                  # healthz не дзвонив у ліміт
    spy.clock.advance(60)
    assert get(PATH + GOOD, service, protection)[0] == 200     # лічильник не зачеплено
    # і через справжній обробник, з іншою адресою й підробленим XFF
    for _ in range(50):
        status, pairs, body = roundtrip(service, protection, path="/healthz",
                                        headers=[("X-Forwarded-For", "1.2.3.4")])
        assert status == 200 and json.loads(body) == {"ok": True}
        assert "retry-after" not in _names(pairs)
    assert len(spy.keys) == 3


def test_cold_over_capacity_gets_503_cache_hit_and_join_do_not(cleanup) -> None:
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    protection, spy = _prot(limit=1000)
    warm = get(PATH + rig.mint("ins1"), rig.service, protection)        # кеш: ins1
    assert warm[0] == 200 and rig.slots.in_use == 0
    gate, owner, owner_box = _hold(rig, "ins2", threads)                # єдиний слот зайнятий
    calls_before = list(rig.ingest.calls)

    status, doc, headers = get(PATH + rig.mint("ins4"), rig.service, protection)   # холодний
    assert status == 503
    assert doc == {"mint": rig.mint("ins4"), "error": {
        "kind": "busy",
        "detail": f"too many analyses in progress, retry after {RETRY} s",
        "retry_after_seconds": RETRY}}
    assert headers == [("Retry-After", str(RETRY))]
    assert not (RESULT_FIELDS & set(doc))

    hit = get(PATH + rig.mint("ins1"), rig.service, protection)          # кеш-хіт не відхилений
    assert hit == warm and hit[2] == []

    joined: dict = {}
    joiner = threading.Thread(
        target=lambda: joined.update(r=get(PATH + rig.mint("ins2"), rig.service, protection)),
        daemon=True)
    joiner.start()
    threads.append(joiner)
    joiner.join(0.3)
    assert joiner.is_alive(), "приєднаний до in-flight чекає, а не отримує 503"
    gate.set()
    joiner.join(WAIT)
    owner.join(WAIT)
    assert joined["r"][0] == 200 and joined["r"][2] == []
    assert joined["r"][1]["mint"] == rig.mint("ins2") and "risk_score" in joined["r"][1]
    assert rig.ingest.calls == calls_before                              # збір ins2 лише один (власник)
    assert rig.ingest.calls.count(rig.mint("ins4")) == 0                 # відмовлений не збирався
    assert rig.slots.in_use == 0
    assert len(spy.keys) == 4                                            # усі чотири пораховані


def test_overload_bodies_validate_against_008_schema_and_header_equals_body(cleanup) -> None:
    validator = _validator()
    # 429
    protection, spy = _prot(limit=1)
    service = StubService()
    get(PATH + GOOD, service, protection)
    spy.clock.advance(7)
    status, doc, headers = get(PATH + GOOD, service, protection)
    assert status == 429
    validator.validate(doc)
    assert doc["error"]["kind"] == "rate_limited"
    assert headers == [("Retry-After", str(doc["error"]["retry_after_seconds"]))]
    assert doc["error"]["retry_after_seconds"] == 53
    # 503
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    _hold(rig, "ins1", threads)
    protection2, _ = _prot()
    status, doc, headers = get(PATH + rig.mint("ins2"), rig.service, protection2)
    assert status == 503
    validator.validate(doc)
    assert doc["error"]["kind"] == "busy"
    assert headers == [("Retry-After", str(doc["error"]["retry_after_seconds"]))]
    # те саме на дроті: рівно один заголовок, ціле число, тіло ті самі байти
    status, pairs, body = roundtrip(rig.service, protection2, path=PATH + rig.mint("ins2"))
    assert status == 503
    value = _one(pairs, "Retry-After")
    assert re.fullmatch(r"[1-9][0-9]*", value)
    assert json.loads(body)["error"]["retry_after_seconds"] == int(value) == RETRY
    validator.validate(json.loads(body))
    assert _one(pairs, "Content-Type") == "application/json"
    assert int(_one(pairs, "Content-Length")) == len(body)
    assert body == to_json(doc).encode("utf-8")


def test_success_and_400_500_unchanged_vs_handle_token_request() -> None:
    mint, path = _mint_of("ins4")
    graph, clusters = _real_services()
    real = DeliveryService(RecordedIngest({mint: path}), graph, clusters, DeliveryCache())

    class Rejecting:
        def collect(self, m: str):
            return Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=m, detail="account_missing")

    cases = [
        (mint, real, DeliveryService(RecordedIngest({mint: path}), *_real_services(),
                                     DeliveryCache())),                # 200
        (GOOD, DeliveryService(Rejecting(), *_real_services(), DeliveryCache()),
         DeliveryService(Rejecting(), *_real_services(), DeliveryCache())),   # 400
        (GOOD, Exploding(), Exploding()),                              # 500
    ]
    for m, svc_a, svc_b in cases:
        protection, _ = _prot()
        status, doc, headers = get(PATH + m, svc_a, protection)
        expected_status, expected_doc = handle_token_request(m, svc_b)
        assert (status, headers) == (expected_status, [])
        assert to_json(doc).encode("utf-8") == to_json(expected_doc).encode("utf-8")
    assert get(PATH + mint, real, _prot()[0])[0] == 200


def test_overload_never_looks_like_result(cleanup) -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    get(PATH + GOOD, service, protection)
    _, doc429, _ = get(PATH + GOOD, service, protection)
    rigs, threads = cleanup
    rig = Rig(capacity=1)
    rigs.append(rig)
    _hold(rig, "ins1", threads)
    _, doc503, _ = get(PATH + rig.mint("ins2"), rig.service, _prot()[0])
    for doc in (doc429, doc503):
        assert set(doc) == {"mint", "error"}
        assert set(doc["error"]) == {"kind", "detail", "retry_after_seconds"}
        assert not (RESULT_FIELDS & set(doc)) and not (RESULT_FIELDS & set(doc["error"]))
        assert "".join(json.dumps(doc)).count("risk_score") == 0
        assert doc["error"]["kind"] in ("rate_limited", "busy")


def test_limits_key_only_on_address_no_auth() -> None:
    source = HTTP_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    strings = [n.value.lower() for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    attrs = [n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    for banned in ("authorization", "cookie", "x-api-key", "api-key", "bearer", "token-key"):
        assert not any(banned in s for s in strings + attrs), banned
    # єдиний читаний заголовок запиту — X-Forwarded-For
    read = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("get", "get_all", "getheader", "__getitem__")
                and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "headers"):
            read.extend(a.value for a in n.args if isinstance(a, ast.Constant))
    assert read == ["X-Forwarded-For"]
    assert not any(isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute)
                   and n.value.attr == "headers" for n in ast.walk(tree))
    assert list(inspect.signature(http_mod.handle_get).parameters) == [
        "path", "peer", "forwarded_for", "service", "protection"]
    # поведінково: різні Authorization/Cookie/User-Agent, одна адреса → один лічильник
    protection, spy = _prot(limit=1)
    service = StubService()
    s1, _, _ = roundtrip(service, protection, path=PATH + GOOD, peer="198.51.100.4",
                         headers=[("Authorization", "Bearer aaa"), ("Cookie", "s=1"),
                                  ("User-Agent", "alpha")])
    s2, pairs, _ = roundtrip(service, protection, path=PATH + GOOD, peer="198.51.100.4",
                             headers=[("Authorization", "Bearer bbb"), ("Cookie", "s=2"),
                                      ("User-Agent", "beta")])
    assert (s1, s2) == (200, 429)
    assert spy.keys == ["198.51.100.4", "198.51.100.4"]
    # і ніякої 401/403 за відсутності будь-яких даних автентифікації
    assert roundtrip(StubService(), None, path=PATH + GOOD)[0] == 200


def test_without_protection_identical_to_004() -> None:
    service = StubService()
    for _ in range(200):                                           # жодного ліміту
        assert get(PATH + GOOD, service)[0] == 200
    assert get(PATH + GOOD, service) == (*handle_token_request(GOOD, service), [])
    assert get("/healthz", service) == (*handle_health(), [])
    assert get("/nope", service)[0] == 404
    assert get(PATH + GOOD, Exploding())[0] == 500
    # байти відповіді: тіло — `to_json(doc)`, заголовки лише ті, що були до фічі
    status, pairs, body = roundtrip(service, None, path=PATH + GOOD)
    expected_status, expected_doc = handle_token_request(GOOD, StubService())
    assert (status, body) == (expected_status, to_json(expected_doc).encode("utf-8"))
    assert sorted(_names(pairs)) == ["content-length", "content-type", "date", "server"]
    status, pairs, body = roundtrip(service, None, path="/healthz")
    assert (status, body) == (200, to_json({"ok": True}).encode("utf-8"))
    assert sorted(_names(pairs)) == ["content-length", "content-type", "date", "server"]
    # навіть підроблений XFF без регулятора нічого не змінює
    status, pairs, body = roundtrip(service, None, path=PATH + GOOD,
                                    headers=[("X-Forwarded-For", "1.2.3.4")])
    assert (status, body) == (expected_status, to_json(expected_doc).encode("utf-8"))


# --------------------------------------------------------------------------- власні: адреси клієнтів


def test_ipv4_and_ipv6_clients_are_independent_keys() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    clients = ["198.51.100.1", "198.51.100.2", "2001:db8::1", "2001:db8::2", "::1"]
    for peer in clients:
        assert get(PATH + GOOD, service, protection, peer=peer)[0] == 200, peer
    for peer in clients:
        assert get(PATH + GOOD, service, protection, peer=peer)[0] == 429, peer
    assert spy.keys[:5] == clients                                 # IPv6 — канонічний текст


def test_ipv4_mapped_and_plain_ipv4_are_one_client() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    assert get(PATH + GOOD, service, protection, peer="198.51.100.7")[0] == 200
    assert get(PATH + GOOD, service, protection, peer="::ffff:198.51.100.7")[0] == 429
    assert set(spy.keys) == {"198.51.100.7"}


def test_two_clients_are_independent_and_recover_separately() -> None:
    protection, spy = _prot(limit=2)
    service = StubService()
    a, b = "198.51.100.1", "198.51.100.2"
    for _ in range(2):
        assert get(PATH + GOOD, service, protection, peer=a)[0] == 200
    assert get(PATH + GOOD, service, protection, peer=a)[0] == 429       # A вичерпав
    for _ in range(2):
        assert get(PATH + GOOD, service, protection, peer=b)[0] == 200   # B не постраждав
    assert get(PATH + GOOD, service, protection, peer=b)[0] == 429
    spy.clock.advance(60)
    assert get(PATH + GOOD, service, protection, peer=a)[0] == 200
    assert get(PATH + GOOD, service, protection, peer=b)[0] == 200


def test_xff_spoofing_from_untrusted_peer_does_not_bypass_limit() -> None:
    protection, spy = _prot(limit=1, trusted=("127.0.0.1",))
    service = StubService()
    peer = "203.0.113.50"                                          # не довірений
    assert get(PATH + GOOD, service, protection, peer=peer, xff="9.9.9.1")[0] == 200
    for fake in ("9.9.9.2", "9.9.9.3", "1.1.1.1, 2.2.2.2", "127.0.0.1"):
        assert get(PATH + GOOD, service, protection, peer=peer, xff=fake)[0] == 429, fake
    assert set(spy.keys) == {peer}
    # те саме крізь справжній обробник
    status, _, _ = roundtrip(service, protection, path=PATH + GOOD, peer=peer,
                             headers=[("X-Forwarded-For", "8.8.8.8")])
    assert status == 429


def test_empty_trusted_list_trusts_nobody() -> None:
    protection, spy = _prot(limit=1, trusted=())
    service = StubService()
    assert get(PATH + GOOD, service, protection, peer="127.0.0.1", xff="1.2.3.4")[0] == 200
    assert get(PATH + GOOD, service, protection, peer="127.0.0.1", xff="5.6.7.8")[0] == 429
    assert set(spy.keys) == {"127.0.0.1"}


def test_xff_from_trusted_peer_distinguishes_clients() -> None:
    protection, spy = _prot(limit=1, trusted=("127.0.0.1", "::1"))
    service = StubService()
    assert get(PATH + GOOD, service, protection, peer="127.0.0.1", xff="1.2.3.4")[0] == 200
    assert get(PATH + GOOD, service, protection, peer="127.0.0.1", xff="5.6.7.8")[0] == 200
    assert get(PATH + GOOD, service, protection, peer="::1", xff="2001:0db8:0:0:0:0:0:9")[0] == 200
    assert get(PATH + GOOD, service, protection, peer="127.0.0.1", xff="1.2.3.4")[0] == 429
    assert spy.keys == ["1.2.3.4", "5.6.7.8", "2001:db8::9", "1.2.3.4"]


def test_xff_leftmost_forgery_behind_trusted_proxy_is_ignored() -> None:
    protection, spy = _prot(limit=1, trusted=("127.0.0.1",))
    service = StubService()
    # проксі дописав справжню адресу справа; клієнт підставляє різні ліві записи
    for forged in ("6.6.6.1", "6.6.6.2", "6.6.6.3"):
        get(PATH + GOOD, service, protection, peer="127.0.0.1", xff=f"{forged}, 1.2.3.4")
    assert set(spy.keys) == {"1.2.3.4"}


def test_trusted_networks_accept_cidr_and_network_objects() -> None:
    import ipaddress
    protection, spy = _prot(limit=5, trusted=("10.0.0.0/8", ipaddress.ip_network("192.168.0.0/16")))
    service = StubService()
    get(PATH + GOOD, service, protection, peer="10.1.2.3", xff="1.2.3.4")
    get(PATH + GOOD, service, protection, peer="192.168.5.5", xff="1.2.3.5")
    assert spy.keys == ["1.2.3.4", "1.2.3.5"]


def test_multiple_xff_headers_merged_in_order_on_the_wire() -> None:
    protection, spy = _prot(limit=100, trusted=("127.0.0.1",))
    service = StubService()
    roundtrip(service, protection, path=PATH + GOOD, peer="127.0.0.1",
              headers=[("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "1.2.3.4")])
    roundtrip(service, protection, path=PATH + GOOD, peer="127.0.0.1",
              headers=[("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "10.0.0.1, 127.0.0.1")])
    roundtrip(service, protection, path=PATH + GOOD, peer="127.0.0.1",
              headers=[("x-forwarded-for", "5.5.5.5")])            # регістр імені
    roundtrip(service, protection, path=PATH + GOOD, peer="127.0.0.1", headers=[])
    assert spy.keys == ["1.2.3.4", "10.0.0.1", "5.5.5.5", "127.0.0.1"]


def test_peer_is_the_connection_address_on_the_wire() -> None:
    protection, spy = _prot()
    roundtrip(StubService(), protection, path=PATH + GOOD, peer="198.51.100.77")
    roundtrip(StubService(), protection, path=PATH + GOOD, peer="2001:db8::77")
    assert spy.keys == ["198.51.100.77", "2001:db8::77"]


# --------------------------------------------------------------------------- власні: маршрути й методи


def test_404_and_405_and_other_methods_not_limited_and_unchanged() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    for p in ("/nope", "/api/token/", "/api/token/a/b", "/api/tokens/x", "/", "/healthz/x"):
        status, doc, headers = get(p, service, protection)
        assert status == 404 and headers == [], p
        assert doc == {"mint": "", "error": {"kind": "not_found", "detail": "unknown path"}}
    assert spy.keys == [] and service.calls == []
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        for _ in range(3):
            status, pairs, body = roundtrip(service, protection, method=method, path=PATH + GOOD)
            assert status == 405 and "retry-after" not in _names(pairs)
            assert json.loads(body)["error"]["kind"] == "method_not_allowed"
    for method in ("HEAD", "OPTIONS", "TRACE", "CONNECT"):
        status, pairs, _ = roundtrip(service, protection, method=method, path=PATH + GOOD)
        assert status == 501 and "retry-after" not in _names(pairs)    # як було: немає do_<METHOD>
    assert spy.keys == [] and service.calls == []
    # вичерпаний ліміт не змінює жодної з цих відповідей
    get(PATH + GOOD, service, protection)
    assert get(PATH + GOOD, service, protection)[0] == 429
    assert roundtrip(service, protection, method="POST", path=PATH + GOOD)[0] == 405
    assert roundtrip(service, protection, path="/nope")[0] == 404
    assert roundtrip(service, protection, method="HEAD", path=PATH + GOOD)[0] == 501


def test_query_string_and_raw_segment_in_429_body() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    get(PATH + GOOD, service, protection)
    status, doc, _ = get(PATH + "ab%20c%2F?x=1", service, protection)
    assert status == 429 and doc["mint"] == "ab%20c%2F"             # сегмент як отримано, без запиту
    _validator().validate(doc)


def test_retry_after_present_only_on_overload_and_exactly_once() -> None:
    protection, _ = _prot(limit=1)
    service = StubService()
    ok = roundtrip(service, protection, path=PATH + GOOD)
    assert ok[0] == 200 and "retry-after" not in _names(ok[1])
    over = roundtrip(service, protection, path=PATH + GOOD)
    assert over[0] == 429
    assert _names(over[1]).count("retry-after") == 1
    assert re.fullmatch(r"[1-9][0-9]*", _one(over[1], "Retry-After"))
    assert 1 <= int(_one(over[1], "Retry-After")) <= 60
    assert int(_one(over[1], "Retry-After")) == json.loads(over[2])["error"]["retry_after_seconds"]
    err = roundtrip(Exploding(), _prot()[0], path=PATH + GOOD)
    assert err[0] == 500 and "retry-after" not in _names(err[1])
    bad = roundtrip(StubService(), _prot()[0], path="/nope")
    assert bad[0] == 404 and "retry-after" not in _names(bad[1])


def test_429_wire_format_content_type_and_length() -> None:
    protection, _ = _prot(limit=1)
    service = StubService()
    roundtrip(service, protection, path=PATH + GOOD)
    status, pairs, body = roundtrip(service, protection, path=PATH + GOOD)
    assert status == 429
    assert _one(pairs, "Content-Type") == "application/json"
    assert int(_one(pairs, "Content-Length")) == len(body)
    doc = json.loads(body.decode("utf-8"))
    assert body == to_json(doc).encode("utf-8")
    assert _names(pairs).count("content-length") == 1
    assert _names(pairs).count("content-type") == 1


def test_error_status_table_has_overload_kinds() -> None:
    assert http_mod._ERROR_STATUS["rate_limited"] == 429
    assert http_mod._ERROR_STATUS["busy"] == 503
    assert http_mod._ERROR_STATUS["invalid_address"] == 400
    assert http_mod._ERROR_STATUS["token_not_found"] == 400


def test_limit_checked_before_service_even_for_cold_mint() -> None:
    protection, spy = _prot(limit=1)
    service = StubService()
    get(PATH + GOOD, service, protection)
    calls = len(service.calls)
    for _ in range(10):
        assert get(PATH + OTHER, service, protection)[0] == 429
    assert len(service.calls) == calls                               # ні одного виклику на 429
    assert len(spy.keys) == 11


def test_one_hit_per_token_request() -> None:
    protection, spy = _prot(limit=100)
    service = StubService()
    for _ in range(7):
        get(PATH + GOOD, service, protection)
    assert len(spy.keys) == 7


def test_retry_after_bounds_for_window_edge() -> None:
    protection, spy = _prot(limit=1, window=60)
    service = StubService()
    get(PATH + GOOD, service, protection)
    spy.clock.advance(0.2)                       # лишилось 59.8 с → стеля 60
    _, doc, headers = get(PATH + GOOD, service, protection)
    assert headers == [("Retry-After", "60")] and doc["error"]["retry_after_seconds"] == 60
    spy.clock.advance(59.0)                      # лишилось 0.8 с → не 0, а 1
    _, doc, headers = get(PATH + GOOD, service, protection)
    assert headers == [("Retry-After", "1")] and doc["error"]["retry_after_seconds"] == 1
    assert math.ceil(0.8) == 1
    spy.clock.advance(1)
    assert get(PATH + GOOD, service, protection)[0] == 200           # після Retry-After — пропуск


def test_concurrent_requests_one_client_exactly_limit_admitted() -> None:
    protection, _ = _prot(limit=10, window=60)
    service = StubService()
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker() -> None:
        barrier.wait(WAIT)
        status = get(PATH + GOOD, service, protection)[0]
        with lock:
            results.append(status)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT)
    assert sorted(results) == [200] * 10 + [429] * 30
    assert len(service.calls) == 10


# --------------------------------------------------------------------------- serve()


def test_serve_binds_host_and_hands_protection_to_handler(monkeypatch) -> None:
    created: dict = {}

    class FakeServer:
        def __init__(self, address, handler) -> None:
            created["address"], created["handler"] = address, handler

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def serve_forever(self) -> None:
            created["served"] = True

    monkeypatch.setattr(http_mod, "ThreadingHTTPServer", FakeServer)
    protection, _ = _prot()
    service = StubService()
    http_mod.serve(service, 18080, host="127.0.0.1", protection=protection)
    assert created["address"] == ("127.0.0.1", 18080) and created["served"] is True
    handler = created["handler"]
    assert handler.service is service and handler.protection is protection
    assert issubclass(handler, http_mod._Handler)

    http_mod.serve(service, 9)                                       # умовчання як у 004
    assert created["address"] == ("0.0.0.0", 9)
    assert created["handler"].protection is None
    assert http_mod._Handler.protection is None                      # глобальний стан не протік
    assert http_mod._Handler.service is None
    with pytest.raises(TypeError):
        http_mod.serve(service, 9, "127.0.0.1")                      # host — лише іменований
