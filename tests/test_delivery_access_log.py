# verifies: FR-008-15
"""Журнал запитів для оператора (T-175): логер `unmask.access`, HTTP і бот.

Мережі немає: HTTP йде крізь справжній `_Handler` на підробленому зʼєднанні (`FakeRequest`), бот — на
`JournalTransport`. Рядок журналу — операційний запис, а не результат аналізу: тести звіряють і формат, і те,
що журнал НІЧОГО не змінює (байти відповідей, лічильники лімітів, журнал викликів транспорту 004, SC-006).
Безпека: шлях очищається від керувальних/не-ASCII символів (log injection), без query, без заголовків і тіл;
Telegram-id лише як 6 hex від sha256; сирий id і текст повідомлення не логуються.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading

import pytest

from test_delivery_bot_limits import (
    BUSY_DOC,
    DeferredSpawn,
    DocService,
    GOLDEN,
    StubService as BotStub,
    ThreadSpawn,
    _golden,
    _journal,
    busy_slots,
    check,
    feed,
    make_gate,
    make_service,
    mint,
    sync_spawn,
    text_update,
)
from test_delivery_http_protection import (
    GOOD,
    PATH,
    Exploding,
    FakeProtection,
    FakeRequest,
    SpyLimiter,
    StubService,
)
from unmask.delivery import bot as bot_mod
from unmask.delivery import http as http_mod
from unmask.delivery.bot import JournalTransport, handle_check, run_polling

PEER = "203.0.113.9"
TRUSTED = "127.0.0.1"

LINE_RE = re.compile(r"^ACCESS (?P<method>\S+) (?P<path>\S+) (?P<status>\d{3}|-) "
                     r"client=(?P<client>\S+)(?: retry_after=(?P<retry>\d+))?$")
BOT_RE = re.compile(r"^BOT check mint=(?P<mint>[A-Za-z0-9?_-]{1,8}) user=(?P<user>[0-9a-f]{6}|-) "
                    r"outcome=(?P<outcome>ok|busy|rate_limited|in_progress|invalid|error|cached)$")

BUSY_HTTP = {"mint": GOOD, "error": {"kind": "busy", "detail": "d", "retry_after_seconds": 60}}
INVALID_HTTP = {"mint": "x", "error": {"kind": "invalid_address", "detail": "d"}}


# --------------------------------------------------------------------------- дублі й хелпери


class DocServiceHttp:
    """HTTP-сервіс-дубль, що віддає наперед заданий документ."""

    def __init__(self, doc) -> None:
        self.doc = doc

    def analyze(self, mint_: str, **_kw):
        return self.doc


def raw_roundtrip(service, protection, raw: bytes, *, peer: str = PEER):
    """Сирі байти запиту крізь справжній `_Handler`: `(повні байти відповіді)`."""
    cls = type("TestHandler", (http_mod._Handler,), {"service": service, "protection": protection})
    request = FakeRequest(raw)
    cls(request, (peer, 55555), object())
    return bytes(request.sent)


def req(method: str = "GET", path: str = "/healthz", headers: list[tuple[str, str]] = (),
        body: bytes = b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: unmask.test"] + [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def access_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "unmask.access"]


def lines(caplog) -> list[str]:
    return [r.getMessage() for r in access_records(caplog)]


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.INFO, logger="unmask.access")
    return caplog


def one_line(caplog) -> re.Match:
    got = lines(caplog)
    assert len(got) == 1, got
    m = LINE_RE.match(got[0])
    assert m, got[0]
    return m


def one_http(caplog, **kw) -> tuple[str, bytes]:
    service = kw.pop("service", StubService())
    protection = kw.pop("protection", None)
    response = raw_roundtrip(service, protection, **kw)
    return one_line(caplog).string, response


def strip_date(raw: bytes) -> bytes:
    return re.sub(rb"Date: [^\r]*\r\n", b"", raw)


# --------------------------------------------------------------------------- рядок на кожен запит


def test_logger_name_and_level_is_info(caplog) -> None:
    raw_roundtrip(StubService(), None, req())
    (record,) = access_records(caplog)
    assert record.name == "unmask.access" and record.levelno == logging.INFO


def test_200_token_line_format(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path=PATH + GOOD))
    assert lines(caplog) == [f"ACCESS GET {PATH}{GOOD} 200 client={PEER}"]


def test_healthz_is_logged(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/healthz"))
    assert lines(caplog) == [f"ACCESS GET /healthz 200 client={PEER}"]


def test_healthz_logged_even_when_the_limit_is_exhausted(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(1))
    for _ in range(3):
        raw_roundtrip(StubService(), protection, req(path="/healthz"))
    assert lines(caplog) == [f"ACCESS GET /healthz 200 client={PEER}"] * 3


def test_400_is_logged(caplog) -> None:
    raw_roundtrip(DocServiceHttp(INVALID_HTTP), None, req(path=PATH + "!!!"))
    assert lines(caplog) == [f"ACCESS GET {PATH}!!! 400 client={PEER}"]


def test_404_is_logged(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/nope"))
    assert lines(caplog) == [f"ACCESS GET /nope 404 client={PEER}"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_405_is_logged(caplog, method) -> None:
    raw_roundtrip(StubService(), None, req(method, PATH + GOOD, body=b"x=1"))
    assert lines(caplog) == [f"ACCESS {method} {PATH}{GOOD} 405 client={PEER}"]


def test_429_is_logged_with_retry_after_equal_to_header(caplog) -> None:
    spy = SpyLimiter(limit=1, window=60)
    protection = FakeProtection(api=spy)
    raw_roundtrip(StubService(), protection, req(path=PATH + GOOD))
    spy.clock.advance(20)
    response = raw_roundtrip(StubService(), protection, req(path=PATH + GOOD))
    first, second = lines(caplog)
    assert first == f"ACCESS GET {PATH}{GOOD} 200 client={PEER}"
    assert second == f"ACCESS GET {PATH}{GOOD} 429 client={PEER} retry_after=40"
    assert b"Retry-After: 40\r\n" in response


def test_503_is_logged_with_retry_after_equal_to_header(caplog) -> None:
    response = raw_roundtrip(DocServiceHttp(BUSY_HTTP), None, req(path=PATH + GOOD))
    assert lines(caplog) == [f"ACCESS GET {PATH}{GOOD} 503 client={PEER} retry_after=60"]
    assert b"Retry-After: 60\r\n" in response


def test_200_and_404_carry_no_retry_after(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path=PATH + GOOD))
    raw_roundtrip(StubService(), None, req(path="/nope"))
    assert all("retry_after" not in line for line in lines(caplog))


def test_service_exception_still_logs_the_500_line(caplog) -> None:
    """`handle_token_request` ловить виняток сервісу -> 500; рядок є, тексту винятку в ньому немає."""
    response = raw_roundtrip(Exploding(), None, req(path=PATH + GOOD))
    assert response.startswith(b"HTTP/1.0 500 ")
    assert lines(caplog) == [f"ACCESS GET {PATH}{GOOD} 500 client={PEER}"]


def test_exception_outside_the_service_keeps_the_old_behaviour_and_logs_a_dash(caplog) -> None:
    """Виняток у регуляторі: як і раніше, летить нагору (відповіді немає); рядок із статусом `-`, один."""

    class BrokenLimiter:
        def hit(self, key):
            raise RuntimeError("TOPSECRET-limiter")

    protection = FakeProtection(api=BrokenLimiter())
    with pytest.raises(RuntimeError, match="TOPSECRET-limiter"):
        raw_roundtrip(StubService(), protection, req(path=PATH + GOOD))
    assert lines(caplog) == [f"ACCESS GET {PATH}{GOOD} - client={PEER}"]


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_methods_without_a_handler_get_one_line_with_the_stdlib_status(caplog, method) -> None:
    response = raw_roundtrip(StubService(), None, req(method, "/healthz"))
    assert response.startswith(b"HTTP/1.0 501 ")
    assert lines(caplog) == [f"ACCESS {method} /healthz 501 client={PEER}"]


def test_garbage_request_line_is_logged_without_echoing_it(caplog) -> None:
    raw_roundtrip(StubService(), None, b"TOTALLY BROKEN LINE WITH MANY WORDS\r\n\r\n")
    (line,) = lines(caplog)
    assert line == f"ACCESS - - 400 client={PEER}"


def test_too_long_request_line_is_logged(caplog) -> None:
    raw_roundtrip(StubService(), None, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n")
    (line,) = lines(caplog)
    assert re.fullmatch(rf"ACCESS - - 414 client={re.escape(PEER)}", line), line


def test_exactly_one_line_per_request_across_all_branches(caplog) -> None:
    cases = [req(path=PATH + GOOD), req(path="/healthz"), req(path="/x"), req("POST", "/healthz"),
             req("HEAD", "/healthz"), req(path="/api/token/")]
    for raw in cases:
        caplog.clear()
        raw_roundtrip(StubService(), None, raw)
        assert len(caplog.records) == 1, (raw, [r.getMessage() for r in caplog.records])


def test_every_line_matches_the_strict_format(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(2))
    for raw in (req(path=PATH + GOOD), req(path=PATH + GOOD), req(path=PATH + GOOD),
                req(path="/healthz"), req(path="/zz"), req("POST", "/x")):
        raw_roundtrip(StubService(), protection, raw)
    assert len(lines(caplog)) == 6
    assert all(LINE_RE.match(line) for line in lines(caplog))


# --------------------------------------------------------------------------- client: правило R-4


def test_xff_from_untrusted_peer_is_ignored(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path=PATH + GOOD,
                                                 headers=[("X-Forwarded-For", "198.51.100.7")]),
                  peer="192.0.2.50")
    assert one_line(caplog).group("client") == "192.0.2.50"


def test_xff_from_trusted_peer_gives_the_real_client(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path=PATH + GOOD,
                                                 headers=[("X-Forwarded-For", "198.51.100.7")]),
                  peer=TRUSTED)
    assert one_line(caplog).group("client") == "198.51.100.7"


@pytest.mark.parametrize("path", ["/healthz", "/nope", PATH + GOOD])
def test_client_uses_the_same_rule_for_healthz_and_404(caplog, path) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path=path, headers=[("X-Forwarded-For", "198.51.100.7")]),
                  peer=TRUSTED)
    assert one_line(caplog).group("client") == "198.51.100.7"


def test_client_for_405_and_501_follows_the_rule_too(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    for method in ("POST", "HEAD"):
        raw_roundtrip(StubService(), protection, req(method, "/healthz",
                                                     headers=[("X-Forwarded-For", "198.51.100.7")]),
                      peer=TRUSTED)
    assert [LINE_RE.match(line).group("client") for line in lines(caplog)] == ["198.51.100.7"] * 2


def test_client_in_the_line_equals_the_key_of_the_limiter(caplog) -> None:
    spy = SpyLimiter()
    protection = FakeProtection(api=spy, trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path=PATH + GOOD,
                                                 headers=[("X-Forwarded-For", "10.9.9.9, 198.51.100.7")]),
                  peer=TRUSTED)
    assert spy.keys == [one_line(caplog).group("client")] == ["198.51.100.7"]


def test_multiple_xff_headers_are_joined_like_the_handler_does(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path="/healthz", headers=[
        ("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-For", "127.0.0.1")]), peer=TRUSTED)
    assert one_line(caplog).group("client") == "198.51.100.7"


def test_garbage_xff_falls_back_to_the_peer(caplog) -> None:
    protection = FakeProtection(api=SpyLimiter(), trusted_proxies=(TRUSTED,))
    raw_roundtrip(StubService(), protection, req(path="/healthz",
                                                 headers=[("X-Forwarded-For", "evil\x01<script>")]),
                  peer=TRUSTED)
    assert one_line(caplog).group("client") == TRUSTED


def test_no_protection_means_peer_only(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/healthz", headers=[("X-Forwarded-For", "198.51.100.7")]),
                  peer=TRUSTED)
    assert one_line(caplog).group("client") == TRUSTED


@pytest.mark.parametrize("peer,expected", [("::ffff:203.0.113.9", "203.0.113.9"),
                                           ("2001:db8::1", "2001:db8::1")])
def test_ipv6_and_mapped_peers_are_normalised(caplog, peer, expected) -> None:
    raw_roundtrip(StubService(), None, req(path="/healthz"), peer=peer)
    assert one_line(caplog).group("client") == expected


def test_a_hostile_peer_string_is_sanitised(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/healthz"), peer="evil\r\nACCESS GET /x 200 client=1.1.1.1")
    (line,) = lines(caplog)
    assert "\n" not in line and "\r" not in line and LINE_RE.match(line), line


def test_unix_socket_style_peer_keeps_the_old_failure_and_still_logs_one_line(caplog) -> None:
    """AF_UNIX: `client_address` — порожній рядок. `do_GET` і до T-175 падав (серверу це не потрібно: лише TCP);
    поведінка не змінена, а рядок журналу з `-` замість адреси все одно один."""
    cls = type("T", (http_mod._Handler,), {"service": StubService(), "protection": None})
    with pytest.raises(IndexError):
        cls(FakeRequest(req()), "", object())
    assert lines(caplog) == ["ACCESS GET /healthz - client=-"]


# --------------------------------------------------------------------------- шлях: безпека (log injection)


def test_percent_encoded_crlf_is_kept_literal_not_decoded(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/api/token/abc%0d%0aACCESS%20GET%20/x%20200"))
    (line,) = lines(caplog)
    assert "%0d%0a" in line and "\n" not in line and "\r" not in line
    assert LINE_RE.match(line)


def test_raw_control_chars_in_the_path_are_escaped(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/a\x01b\x1bc\x7fd\x00e"))
    (line,) = lines(caplog)
    assert all(0x20 <= ord(c) < 0x7F for c in line), repr(line)
    assert "\\x01" in line and "\\x1b" in line and "\\x7f" in line and "\\x00" in line
    assert LINE_RE.match(line)


def test_null_byte_in_the_path(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path=PATH + "ab\x00cd"))
    (line,) = lines(caplog)
    assert "\x00" not in line and "\\x00" in line and LINE_RE.match(line)


def test_non_ascii_bytes_in_the_path_are_escaped(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/api/token/" + "Ð´ÿ"))
    (line,) = lines(caplog)
    assert all(0x20 <= ord(c) < 0x7F for c in line), repr(line)
    assert "\\xd0" in line and LINE_RE.match(line)


def test_backslash_is_doubled_so_escapes_are_unambiguous(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/a\\x41b"))
    (line,) = lines(caplog)
    assert "/a\\\\x41b" in line  # дійсний «\x41» у шляху не виглядає як екранований символ «A»


def test_a_forged_log_line_in_the_path_cannot_become_a_second_line(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/x\r\n\r\nACCESS GET /admin 200 client=9.9.9.9"))
    assert len(caplog.records) <= 1
    for line in lines(caplog):
        assert "\n" not in line and "\r" not in line


def test_tabs_and_unicode_whitespace_in_the_path(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/a\tb c"))
    got = lines(caplog)
    assert len(got) == 1 and all(0x20 <= ord(c) < 0x7F for c in got[0]), got


def test_log_text_unit_escapes_everything_unsafe() -> None:
    text = "".join(chr(i) for i in range(0, 300)) + "д\U0001f600"
    out = http_mod._log_text(text, 10_000)
    assert " " not in out
    assert "\n" not in out and "\r" not in out and "\t" not in out and "\x00" not in out
    assert all(0x20 <= ord(c) < 0x7F for c in out)
    assert "\\u0434" in out and "\\U0001f600" in out


def test_log_text_has_no_spaces_so_fields_stay_separable() -> None:
    assert " " not in http_mod._log_text("a b c", 64)


def test_empty_path_becomes_a_dash() -> None:
    assert http_mod._log_text("", 64) == "-"


def test_path_is_truncated_to_64(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/" + "a" * 5000))
    m = one_line(caplog)
    assert len(m.group("path")) == 64 and m.group("path").endswith("...")


def test_path_of_exactly_64_chars_is_not_marked(caplog) -> None:
    path = "/" + "a" * 63
    raw_roundtrip(StubService(), None, req(path=path))
    assert one_line(caplog).group("path") == path


def test_path_of_65_chars_is_cut(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/" + "a" * 64))
    path = one_line(caplog).group("path")
    assert len(path) == 64 and path.endswith("...")


def test_truncation_never_cuts_inside_an_escape(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/" + "\x01" * 200))
    path = one_line(caplog).group("path")
    assert len(path) <= 64
    body = path[: -len("...")]
    assert re.fullmatch(r"(?:/|\\x01)*", body), path


def test_a_full_mint_path_is_logged_whole(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path=PATH + "So11111111111111111111111111111111111111112"))
    assert one_line(caplog).group("path") == PATH + "So11111111111111111111111111111111111111112"


def test_log_text_is_total_on_any_input() -> None:
    for text in ["\ud800", "\udfff", "\x00" * 5, "я" * 100, "\U0010ffff"]:
        out = http_mod._log_text(text, 64)
        assert isinstance(out, str) and len(out) <= 64 and out


# --------------------------------------------------------------------------- без query, заголовків, тіл


def test_query_string_is_not_logged(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path=PATH + GOOD + "?apikey=SECRETKEY123&token=abc#frag"))
    (line,) = lines(caplog)
    assert line == f"ACCESS GET {PATH}{GOOD} 200 client={PEER}"
    for leaked in ("SECRETKEY123", "apikey", "token=abc", "frag", "?"):
        assert leaked not in line


def test_query_on_healthz_and_404_is_dropped_too(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/healthz?probe=SECRETKEY123"))
    raw_roundtrip(StubService(), None, req(path="/nope?k=SECRETKEY123"))
    assert lines(caplog) == [f"ACCESS GET /healthz 200 client={PEER}",
                             f"ACCESS GET /nope 404 client={PEER}"]


def test_query_is_cut_before_truncation(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/p" + "?" + "SECRETKEY123" * 50))
    assert "SECRETKEY123" not in lines(caplog)[0]


def test_question_mark_only_path_and_fragment_only(caplog) -> None:
    raw_roundtrip(StubService(), None, req(path="/x#SECRETKEY123"))
    raw_roundtrip(StubService(), None, req(path="/?SECRETKEY123"))
    got = lines(caplog)
    assert got == [f"ACCESS GET /x 404 client={PEER}", f"ACCESS GET / 404 client={PEER}"]


def test_headers_and_bodies_never_reach_the_log(caplog) -> None:
    headers = [("Authorization", "Bearer SECRETKEY123"), ("Cookie", "sid=SECRETKEY123"),
               ("User-Agent", "agent-SECRETKEY123"), ("X-Api-Key", "SECRETKEY123"),
               ("Referer", "https://evil.example/?SECRETKEY123")]
    raw_roundtrip(StubService(), None, req(path=PATH + GOOD, headers=headers))
    raw_roundtrip(StubService(), None, req("POST", PATH + GOOD, headers=headers, body=b"SECRETKEY123"))
    got = lines(caplog)
    assert len(got) == 2
    for line in got:
        assert "SECRETKEY123" not in line and "evil.example" not in line and "Bearer" not in line
    for record in access_records(caplog):
        assert "SECRETKEY123" not in repr(record.__dict__)


def test_no_other_logger_records_are_produced_by_a_request(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    raw_roundtrip(StubService(), None, req(path=PATH + GOOD, headers=[("X-Api-Key", "SECRETKEY123")]))
    assert {r.name for r in caplog.records} == {"unmask.access"}


def test_httpx_and_httpcore_loggers_stay_silenced() -> None:
    """Журнал бота не має відкривати URL з токеном: логери `httpx`/`httpcore` глушаться (T-175 їх не чіпає)."""
    import unmask.delivery.main  # noqa: F401  (процес сервера: глушіння логерів — побічний ефект імпорту ланцюга)

    for name in ("httpx", "httpcore"):
        record = logging.LogRecord(name, logging.INFO, __file__, 1, "GET /bot123:SECRET", None, None)
        assert logging.getLogger(name).filter(record) in (False, 0)
    assert not logging.getLogger("unmask.access").filters  # наш логер не глушиться


# --------------------------------------------------------------------------- журнал нічого не змінює


def _response_set(enabled: bool, monkeypatch):
    if not enabled:
        monkeypatch.setattr(http_mod._Handler, "log_request", lambda self, *a, **k: None)
    spy = SpyLimiter(limit=3, window=60)
    protection = FakeProtection(api=spy, trusted_proxies=(TRUSTED,))
    out = []
    for raw in (req(path=PATH + GOOD), req(path=PATH + GOOD), req(path=PATH + GOOD),
                req(path=PATH + GOOD), req(path="/healthz"), req(path="/nope"),
                req("POST", "/healthz", body=b"abc"), req("HEAD", "/healthz"),
                req(path=PATH + GOOD, headers=[("X-Forwarded-For", "198.51.100.7")])):
        out.append(strip_date(raw_roundtrip(StubService(), protection, raw, peer=TRUSTED)))
    return out, spy


def test_responses_are_byte_identical_with_and_without_logging(monkeypatch, caplog) -> None:
    with_log, spy_on = _response_set(True, monkeypatch)
    logged = len(lines(caplog))
    caplog.clear()
    without_log, spy_off = _response_set(False, monkeypatch)
    assert with_log == without_log
    assert logged == 9 and lines(caplog) == []
    assert with_log[3].startswith(b"HTTP/1.0 429 ")  # сценарій справді зачепив відмову


def test_limiter_counters_are_not_touched_by_logging(monkeypatch, caplog) -> None:
    _, spy_on = _response_set(True, monkeypatch)
    caplog.clear()
    _, spy_off = _response_set(False, monkeypatch)
    assert spy_on.keys == spy_off.keys == [TRUSTED] * 4 + ["198.51.100.7"]
    assert spy_on.clock.now == spy_off.clock.now


def test_logging_does_not_call_the_limiter_or_the_service(caplog) -> None:
    spy = SpyLimiter()
    service = StubService()
    protection = FakeProtection(api=spy, trusted_proxies=(TRUSTED,))
    raw_roundtrip(service, protection, req(path="/healthz"))
    raw_roundtrip(service, protection, req("POST", "/healthz"))
    raw_roundtrip(service, protection, req("HEAD", "/healthz"))
    raw_roundtrip(service, protection, req(path="/nope"))
    assert spy.keys == [] and service.calls == []
    assert len(lines(caplog)) == 4


def test_a_raising_log_handler_does_not_break_the_response(caplog) -> None:
    class Bomb(logging.Handler):
        def emit(self, record):
            raise RuntimeError("TOPSECRET-handler")

    baseline = strip_date(raw_roundtrip(StubService(), None, req(path=PATH + GOOD)))
    logger = logging.getLogger("unmask.access")
    bomb = Bomb()
    logger.addHandler(bomb)
    try:
        spy = SpyLimiter(limit=1)
        protection = FakeProtection(api=spy)
        got = strip_date(raw_roundtrip(StubService(), protection, req(path=PATH + GOOD)))
        denied = raw_roundtrip(StubService(), protection, req(path=PATH + GOOD))
    finally:
        logger.removeHandler(bomb)
    assert got == baseline and denied.startswith(b"HTTP/1.0 429 ")
    assert "TOPSECRET-handler" not in "\n".join(r.getMessage() for r in caplog.records)


def test_failure_while_building_the_line_is_swallowed_without_exception_text(monkeypatch, caplog) -> None:
    def boom(*_a, **_k):
        raise ValueError("TOPSECRET-client")

    monkeypatch.setattr(http_mod, "client_address", boom)
    response = raw_roundtrip(StubService(), None, req(path=PATH + GOOD))
    assert response.startswith(b"HTTP/1.0 200 ")
    for record in caplog.records:
        assert "TOPSECRET" not in record.getMessage()
    got = lines(caplog)
    assert len(got) == 1 and got[0] == f"ACCESS GET {PATH}{GOOD} 200 client=-"  # рядок не губиться


def test_failure_of_the_sanitiser_is_swallowed(monkeypatch, caplog) -> None:
    def boom(*_a, **_k):
        raise ValueError("TOPSECRET-text")

    monkeypatch.setattr(http_mod, "_log_text", boom)
    response = raw_roundtrip(StubService(), None, req(path=PATH + GOOD))
    assert response.startswith(b"HTTP/1.0 200 ")
    assert "TOPSECRET" not in "\n".join(r.getMessage() for r in caplog.records)
    assert len(caplog.records) <= 1


def test_disabled_logger_costs_nothing_and_changes_nothing(caplog) -> None:
    logging.getLogger("unmask.access").setLevel(logging.CRITICAL)
    try:
        response = raw_roundtrip(StubService(), None, req(path=PATH + GOOD))
    finally:
        logging.getLogger("unmask.access").setLevel(logging.INFO)
    assert response.startswith(b"HTTP/1.0 200 ") and lines(caplog) == []


def test_log_message_of_the_stdlib_stays_silent(caplog) -> None:
    """Старий `log_message`/`log_error` лишається порожнім: інакше 400/501 дали б другий рядок."""
    raw_roundtrip(StubService(), None, req("HEAD", "/healthz"))
    assert len(caplog.records) == 1


# --------------------------------------------------------------------------- потоки


def test_concurrent_requests_each_produce_exactly_one_intact_line(caplog) -> None:
    n = 24
    barrier = threading.Barrier(n, timeout=10)
    errors: list[BaseException] = []

    def one(i: int) -> None:
        try:
            barrier.wait()
            raw_roundtrip(StubService(), None, req(path=f"/p{i}"), peer=f"198.51.100.{i + 1}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
        assert not t.is_alive()
    assert errors == []
    got = lines(caplog)
    assert len(got) == n and all(LINE_RE.match(line) for line in got)
    assert {LINE_RE.match(line).group("path") for line in got} == {f"/p{i}" for i in range(n)}
    for i in range(n):
        assert f"ACCESS GET /p{i} 404 client=198.51.100.{i + 1}" in got


# --------------------------------------------------------------------------- стандартна конфігурація main


def test_default_main_logging_prints_the_access_line_to_stderr(capsys) -> None:
    from unmask.delivery.main import _ensure_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        _ensure_logging()
        # відкидаємо рівень, який caplog виставив фікстурою, — перевіряємо успадкований від root
        logging.getLogger("unmask.access").setLevel(logging.NOTSET)
        raw_roundtrip(StubService(), None, req(path="/healthz"))
        for handler in root.handlers:
            handler.flush()
        err = capsys.readouterr().err
    finally:
        for handler in root.handlers:
            root.removeHandler(handler)
        root.handlers = saved[0]
        root.setLevel(saved[1])
    assert err.count("unmask.access ACCESS GET /healthz 200 client=203.0.113.9") == 1, err


# --------------------------------------------------------------------------- бот


def bot_lines(caplog) -> list[str]:
    return [m for m in lines(caplog) if m.startswith("BOT ")]


def tag(uid) -> str:
    return hashlib.sha256(str(uid).encode()).hexdigest()[:6]


def test_bot_ok_then_cached_lines(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5), check(2, mint("ins4"), frm=5))
    m = mint("ins4")
    assert bot_lines(caplog) == [f"BOT check mint={m[:8]} user={tag(5)} outcome=ok",
                                 f"BOT check mint={m[:8]} user={tag(5)} outcome=cached"]
    assert len(lines(caplog)) == 2


def test_bot_busy_line(caplog) -> None:
    slots, _held = busy_slots(37)
    gate, _ = make_gate()
    service, _ = make_service(slots=slots)
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert bot_lines(caplog) == [f"BOT check mint={mint('ins4')[:8]} user={tag(5)} outcome=busy"]


def test_bot_rate_limited_line(caplog) -> None:
    gate, _ = make_gate(1, 600)
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5),
         check(2, mint("cln1"), frm=5))
    assert bot_lines(caplog) == [f"BOT check mint={mint('ins4')[:8]} user={tag(5)} outcome=ok",
                                 f"BOT check mint={mint('cln1')[:8]} user={tag(5)} outcome=rate_limited"]


def test_bot_in_progress_line_then_the_deferred_worker_line(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    spawn = DeferredSpawn()
    feed(JournalTransport(), service, gate, spawn, check(1, mint("ins4"), frm=5),
         check(2, mint("cln1"), frm=5))
    assert bot_lines(caplog) == [f"BOT check mint={mint('cln1')[:8]} user={tag(5)} outcome=in_progress"]
    spawn.run(0)
    assert bot_lines(caplog)[1] == f"BOT check mint={mint('ins4')[:8]} user={tag(5)} outcome=ok"


def test_bot_invalid_address_line(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, text_update(1, "/check not-a-mint", frm=5))
    assert bot_lines(caplog) == [f"BOT check mint=not-a-mi user={tag(5)} outcome=invalid"]


def test_bot_token_not_found_is_invalid(caplog) -> None:
    doc = {"mint": "m", "error": {"kind": "token_not_found", "detail": "d"}}
    gate, _ = make_gate()
    feed(JournalTransport(), DocService(doc), gate, sync_spawn, check(1, "Abcdefgh12345", frm=5))
    assert bot_lines(caplog) == [f"BOT check mint=Abcdefgh user={tag(5)} outcome=invalid"]


@pytest.mark.parametrize("doc", [{"mint": "m", "error": {"kind": "timeout", "detail": "d"}},
                                 {"mint": "m", "error": "weird"},
                                 {"mint": "m", "error": {"kind": "internal", "detail": "d"}}])
def test_bot_error_docs_are_error(caplog, doc) -> None:
    gate, _ = make_gate()
    feed(JournalTransport(), DocService(doc), gate, sync_spawn, check(1, "Abcdefgh12345", frm=5))
    assert bot_lines(caplog) == [f"BOT check mint=Abcdefgh user={tag(5)} outcome=error"]


def test_bot_service_exception_is_error(caplog) -> None:
    gate, _ = make_gate()
    feed(JournalTransport(), DocService(exc=RuntimeError("TOPSECRET-boom")), gate, sync_spawn,
         check(1, "Abcdefgh12345", frm=5))
    assert bot_lines(caplog) == [f"BOT check mint=Abcdefgh user={tag(5)} outcome=error"]
    assert "TOPSECRET" not in "\n".join(lines(caplog))


def test_bot_failure_after_a_good_analysis_is_still_error(caplog) -> None:
    class Failing(JournalTransport):
        def send_message(self, chat_id, text, reply_markup=None):
            if "risk" in text:
                raise RuntimeError("send failed")
            return super().send_message(chat_id, text, reply_markup)

    gate, _ = make_gate()
    service, _ = make_service()
    feed(Failing(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert bot_lines(caplog) == [f"BOT check mint={mint('ins4')[:8]} user={tag(5)} outcome=error"]


def test_bot_check_without_mint_is_logged_as_invalid(caplog) -> None:
    gate, _ = make_gate()
    feed(JournalTransport(), BotStub(), gate, sync_spawn, text_update(1, "/check", frm=5))
    assert bot_lines(caplog) == [f"BOT check mint=- user={tag(5)} outcome=invalid"]


def test_bot_exactly_one_line_per_check_and_none_for_other_updates(caplog) -> None:
    gate, _ = make_gate(100)
    service, _ = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, text_update(1, "/start", frm=5), text_update(2, "hello", frm=5),
         check(3, mint("ins4"), frm=5), {"update_id": 4, "callback_query": {
             "id": "c", "data": "evidence:zzz", "message": {"chat": {"id": 7}}}})
    assert len(lines(caplog)) == 1


def test_bot_user_is_six_hex_of_sha256_and_raw_id_is_never_logged(caplog) -> None:
    uid = 1234567890123
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn,
         text_update(1, f"/check {mint('ins4')} some private words", chat=999888777, frm=uid))
    (line,) = bot_lines(caplog)
    m = BOT_RE.match(line)
    assert m and m.group("user") == tag(uid)
    assert len(m.group("user")) == 6
    blob = "\n".join(lines(caplog)) + repr([r.__dict__ for r in access_records(caplog)])
    for leaked in (str(uid), "999888777", "private words", "some", hashlib.sha256(str(uid).encode()).hexdigest()):
        assert leaked not in blob, leaked


def test_bot_user_falls_back_to_chat_id(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), chat=424242))
    assert f"user={tag(424242)}" in bot_lines(caplog)[0]


def test_bot_string_ids_are_hashed_as_str(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn,
         {"update_id": 1, "message": {"text": f"/check {mint('ins4')}", "chat": {"id": 1},
                                      "from": {"id": "42"}}})
    assert f"user={tag(42)}" in bot_lines(caplog)[0]  # str(42) == "42"


def test_bot_different_users_get_different_tags(caplog) -> None:
    gate, _ = make_gate(100)
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5),
         check(2, mint("cln1"), frm=6))
    tags = {BOT_RE.match(line).group("user") for line in bot_lines(caplog)}
    assert tags == {tag(5), tag(6)}


def test_bot_mint_is_at_most_eight_chars_and_hostile_text_is_neutralised(caplog) -> None:
    gate, _ = make_gate(100)
    hostile = ["A" * 400, "ab\x1b[31mcdefgh", "ж" * 20, "a‮b" * 10, "\x00\x01\x02" * 5,
               "x\u0085y z" * 3]
    for i, text in enumerate(hostile):
        feed(JournalTransport(), DocService({"mint": "m", "error": {"kind": "invalid_address", "detail": "d"}}),
             gate, sync_spawn, check(i + 1, text, frm=i + 1))
    got = bot_lines(caplog)
    assert len(got) == len(hostile)
    for line in got:
        m = BOT_RE.match(line)
        assert m and len(m.group("mint")) <= 8, line
        assert all(0x20 <= ord(c) < 0x7F for c in line), repr(line)


def test_bot_newline_in_message_cannot_forge_a_second_log_line(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn,
         text_update(1, "/check abc\nBOT check mint=ZZZZZZZZ user=000000 outcome=ok", frm=5))
    got = lines(caplog)
    assert len(got) == 1 and "\n" not in got[0] and BOT_RE.match(got[0])


def test_bot_lines_have_no_message_text_even_for_extra_words(caplog) -> None:
    gate, _ = make_gate()
    feed(JournalTransport(), DocService(BUSY_DOC), gate, sync_spawn,
         text_update(1, f"/check {mint('ins4')} password=SECRETKEY123", frm=5))
    assert "SECRETKEY123" not in "\n".join(lines(caplog))


def test_bot_ungated_flow_logs_too(caplog) -> None:
    service, _ = make_service()
    t = JournalTransport()
    t.queue(check(1, mint("ins4"), frm=5))
    run_polling(t, service, evidence_limit=4000, stop_after=1)
    assert bot_lines(caplog) == [f"BOT check mint={mint('ins4')[:8]} user={tag(5)} outcome=ok"]


def test_bot_handle_check_direct_call_without_user_logs_a_dash(caplog) -> None:
    service, _ = make_service()
    handle_check(f"/check {mint('ins4')}", service, JournalTransport(), 7, {})
    assert bot_lines(caplog) == [f"BOT check mint={mint('ins4')[:8]} user=- outcome=ok"]


def test_bot_handle_check_return_value_is_unchanged(caplog) -> None:
    service, _ = make_service()
    registry: dict = {}
    request_id = handle_check(f"/check {mint('ins4')}", service, JournalTransport(), 7, registry)
    assert request_id == f"{mint('ins4')[:8]}:0" and registry == {request_id: mint("ins4")}
    assert handle_check("/check", service, JournalTransport(), 7, {}) is None


def test_bot_dropped_update_without_any_id_is_not_logged(caplog) -> None:
    gate, _ = make_gate()
    feed(JournalTransport(), BotStub(), gate, sync_spawn, text_update(1, f"/check {mint('ins4')}", chat=None))
    assert lines(caplog) == []


def test_bot_concurrent_workers_log_one_line_each(caplog) -> None:
    gate, _ = make_gate(100)
    service, _ = make_service()
    spawn = ThreadSpawn()
    updates = [check(i, mint("ins4"), frm=i) for i in range(1, 9)]
    feed(JournalTransport(), service, gate, spawn, *updates)
    spawn.join_all()
    got = bot_lines(caplog)
    assert len(got) == 8 and all(BOT_RE.match(line) for line in got)
    assert {BOT_RE.match(line).group("user") for line in got} == {tag(i) for i in range(1, 9)}
    assert sorted(BOT_RE.match(line).group("outcome") for line in got).count("ok") >= 1


# --------------------------------------------------------------------------- бот: журнал транспорту (SC-006)


def _golden_run(gate, spawn):
    data = _golden()
    service, _ = make_service(("ins4", "cln1"))
    t = JournalTransport(data["updates"])
    registry = run_polling(t, service, evidence_limit=4000, stop_after=len(data["updates"]),
                           gate=gate, spawn=spawn)
    return data, t, registry


@pytest.mark.parametrize("gated", [False, True])
def test_bot_journal_is_byte_identical_to_004_with_logging_on(caplog, gated) -> None:
    gate = make_gate(100)[0] if gated else None
    data, t, registry = _golden_run(gate, sync_spawn if gated else None)
    assert _journal(t) == data["calls"] and dict(registry) == data["registry"]
    checks = [u for u in data["updates"] if u.get("message", {}).get("text", "").startswith("/check")]
    assert len(bot_lines(caplog)) == len(checks) > 0
    assert GOLDEN.is_file()


@pytest.mark.parametrize("gated", [False, True])
def test_bot_journal_is_unchanged_when_the_log_handler_raises(caplog, gated) -> None:
    class Bomb(logging.Handler):
        def emit(self, record):
            raise RuntimeError("TOPSECRET-handler")

    logger = logging.getLogger("unmask.access")
    bomb = Bomb()
    logger.addHandler(bomb)
    try:
        gate = make_gate(100)[0] if gated else None
        data, t, registry = _golden_run(gate, sync_spawn if gated else None)
    finally:
        logger.removeHandler(bomb)
    assert _journal(t) == data["calls"] and dict(registry) == data["registry"]


def test_bot_logging_failure_does_not_change_replies(monkeypatch, caplog) -> None:
    def boom(*_a, **_k):
        raise ValueError("TOPSECRET-hash")

    monkeypatch.setattr(bot_mod, "_user_tag", boom)
    gate, _ = make_gate()
    service, _ = make_service()
    t = JournalTransport()
    feed(t, service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert any(c[0] == "send_message" and "risk" in c[1][1] or c[0] == "send_photo" for c in t.calls)
    assert "TOPSECRET" not in "\n".join(r.getMessage() for r in caplog.records)


def test_bot_log_does_not_change_the_gate_log(caplog) -> None:
    gate, _ = make_gate()
    service, _ = make_service()
    feed(JournalTransport(), service, gate, sync_spawn, check(1, mint("ins4"), frm=5))
    assert gate.log == [("enter", 5, True), ("leave", 5)]
