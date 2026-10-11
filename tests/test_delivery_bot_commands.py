# verifies: FR-004-03
"""Меню команд бота (T-100): `setMyCommands` у `HttpxBotTransport`, реєстрація в `main` до потоку опитування.

Без мережі: `httpx.MockTransport` для сирого POST, фіктивні сервер/потік/транспорт для `main`. Токен — сентинел:
він не має потрапити ні в логи, ні в текст повідомлень. Журнал SC-006 (`bot_journal_004.json`) тут не чіпаємо:
`run_polling` і `BotTransport` не змінюються, тож `set_my_commands` є лише в `HttpxBotTransport`.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import types
from pathlib import Path

import httpx
import pytest
import yaml

from unmask.delivery import bot as bot_module
from unmask.delivery import main as main_mod
from unmask.delivery.bot import BotTransport, HttpxBotTransport, JournalTransport, run_polling
from unmask.delivery.main import main

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
TOKEN = "SENTINEL_BOT_TOKEN_4d8e"
SENTINEL_RPC = "https://sentinel-rpc.invalid/rpc?api-key=SENTINEL_KEY_RPC_7f3e"
ENV = {"UNMASK_RPC_URL": SENTINEL_RPC, "UNMASK_BOT_TOKEN": TOKEN}
COPIED = ("delivery.yaml", "ingest.yaml", "hubs.yaml", "hub_addresses.yaml", "clusters.yaml",
          "protection.yaml")
EXPECTED_COMMANDS = [
    {"command": "start", "description": "Show what this bot does"},
    {"command": "help", "description": "How to read the result"},
    {"command": "check", "description": "Check a Solana token: /check <mint>"},
]


def _commands() -> list[tuple[str, str]]:
    return list(bot_module.BOT_COMMANDS)  # AttributeError до реалізації — очікувана червоність


def _http_transport(handler) -> HttpxBotTransport:
    """Реальний `HttpxBotTransport` з `MockTransport` замість мережевого клієнта (сокетів немає)."""
    transport = HttpxBotTransport(TOKEN)
    transport._client = httpx.Client(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler))
    return transport


# --------------------------------------------------------------------------- HTTP-метод, шлях, JSON


def test_set_my_commands_posts_json_to_setmycommands_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    _http_transport(handler).set_my_commands(_commands())

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == f"/bot{TOKEN}/setMyCommands"
    assert request.url.host == "api.telegram.org"
    assert json.loads(request.content) == {"commands": EXPECTED_COMMANDS}


def test_set_my_commands_sends_exactly_the_given_commands() -> None:
    seen: list[httpx.Request] = []
    handler = lambda r: (seen.append(r), httpx.Response(200, json={"ok": True}))[1]  # noqa: E731

    _http_transport(handler).set_my_commands([("ping", "Ping the bot")])

    assert json.loads(seen[0].content) == {"commands": [{"command": "ping", "description": "Ping the bot"}]}


def test_set_my_commands_non_ok_response_raises_runtime_error() -> None:
    handler = lambda r: httpx.Response(200, json={"ok": False, "description": "bad"})  # noqa: E731

    with pytest.raises(RuntimeError, match="setMyCommands"):
        _http_transport(handler).set_my_commands(_commands())


# --------------------------------------------------------------------------- вміст команд


def test_command_names_and_descriptions_meet_telegram_limits() -> None:
    commands = _commands()
    assert [name for name, _ in commands] == ["start", "help", "check"]
    for name, description in commands:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 3 <= len(description) <= 256, description


def test_command_texts_are_the_specified_english_ones() -> None:
    assert [{"command": n, "description": d} for n, d in _commands()] == EXPECTED_COMMANDS


def test_bot_transport_protocol_and_journal_do_not_carry_set_my_commands() -> None:
    # SC-006: метод лише в HttpxBotTransport; Protocol і журнал його не вимагають
    assert not hasattr(BotTransport, "set_my_commands")
    assert not hasattr(JournalTransport, "set_my_commands")


# --------------------------------------------------------------------------- main: порядок, помилки, токен


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Фіктивні сервер, потік і транспорт; `events` — єдиний хронологічний журнал подій."""
    for name in COPIED:
        shutil.copy(CONFIG / name, tmp_path / name)
    h: dict = {"events": [], "fail": None, "config_dir": tmp_path, "factory": None}
    events = h["events"]

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra) -> None:
            self.target = target

        def start(self) -> None:
            events.append(("thread_start", self.target))

    class RecordingTransport:
        def set_my_commands(self, commands) -> None:
            events.append(("set_my_commands", list(commands)))
            if h["fail"] is not None:
                raise h["fail"]

    class DummyIngest:
        def __init__(self, cfg, source) -> None:
            pass

    def fake_serve(service, port, *, host=None, protection=None) -> None:
        events.append(("serve", port))

    h["factory"] = lambda token: RecordingTransport()
    monkeypatch.setattr(main_mod, "IngestService", DummyIngest)
    monkeypatch.setattr(main_mod, "serve", fake_serve)
    monkeypatch.setattr(main_mod, "HttpxBotTransport", lambda token: h["factory"](token))
    monkeypatch.setattr(main_mod, "threading",
                        types.SimpleNamespace(Thread=FakeThread, Event=threading.Event))

    return h


def _run(h: dict, *flags: str) -> int:
    return main([*flags, "--config-dir", str(h["config_dir"])], environ=dict(ENV))


def _bot_starts(h: dict) -> list:
    return [e for e in h["events"] if e == ("thread_start", main_mod.run_polling)]


def test_main_registers_commands_once_before_polling_thread(harness: dict) -> None:
    assert _run(harness) == 0

    registrations = [e for e in harness["events"] if e[0] == "set_my_commands"]
    assert registrations == [("set_my_commands", _commands())]
    kinds = [e[0] for e in harness["events"] if e[0] in ("set_my_commands", "thread_start")]
    assert kinds.index("set_my_commands") < kinds.index("thread_start")
    # реєстрація йде саме перед потоком бота, не після (порядок подій)
    bot_idx = harness["events"].index(("thread_start", main_mod.run_polling))
    assert harness["events"].index(registrations[0]) < bot_idx


def test_no_bot_flag_never_registers_commands(harness: dict) -> None:
    assert _run(harness, "--no-bot") == 0

    assert [e for e in harness["events"] if e[0] == "set_my_commands"] == []
    assert _bot_starts(harness) == []


def test_transport_without_set_my_commands_is_skipped_quietly(harness: dict, caplog) -> None:
    harness["factory"] = lambda token: JournalTransport()
    caplog.set_level(logging.WARNING, logger="unmask.delivery")

    assert _run(harness) == 0

    assert len(_bot_starts(harness)) == 1  # бот стартує як і раніше
    assert [r for r in caplog.records if r.name == "unmask.delivery"] == []


@pytest.mark.parametrize("error", [
    RuntimeError("telegram API error"),
    OSError("network down"),
    httpx.ConnectError(f"connect failed for /bot{TOKEN}/setMyCommands"),
], ids=["runtime", "os", "httpx-with-url"])
def test_registration_failure_is_not_fatal_and_bot_still_starts(harness: dict, error: Exception) -> None:
    harness["fail"] = error

    assert _run(harness) == 0

    assert len(_bot_starts(harness)) == 1
    assert any(e[0] == "serve" for e in harness["events"])


def test_registration_failure_logs_exactly_one_warning_without_token(harness: dict, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="unmask.delivery")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {request.url}")  # URL з токеном у тексті винятку

    harness["factory"] = lambda token: _http_transport(handler)

    assert _run(harness) == 0

    warnings = [r for r in caplog.records if r.name == "unmask.delivery" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert "ConnectError" in warnings[0].getMessage()
    assert TOKEN not in caplog.text
    assert TOKEN not in warnings[0].getMessage()
    assert len(_bot_starts(harness)) == 1


def test_http_error_status_does_not_leak_token_into_logs(harness: dict, caplog) -> None:
    caplog.set_level(logging.DEBUG)  # увесь лог, включно з чужими логерами
    harness["factory"] = lambda token: _http_transport(lambda r: httpx.Response(500, text="oops"))

    assert _run(harness) == 0

    assert TOKEN not in caplog.text
    assert TOKEN not in "".join(r.getMessage() for r in caplog.records)
    assert len(_bot_starts(harness)) == 1


def test_registration_failure_does_not_print_token_to_stderr(harness: dict, capsys) -> None:
    harness["fail"] = httpx.ConnectError(f"connect failed for /bot{TOKEN}/setMyCommands")

    assert _run(harness) == 0

    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


# --------------------------------------------------------------------------- пропуск і порядок


def test_real_transport_failure_path_is_non_fatal_end_to_end(harness: dict, caplog) -> None:
    # Реальний set_my_commands з MockTransport: 500 -> HTTPStatusError (містить URL з токеном) -> не фатально
    caplog.set_level(logging.WARNING, logger="unmask.delivery")
    harness["factory"] = lambda token: _http_transport(lambda r: httpx.Response(500))

    assert _run(harness) == 0

    assert len(_bot_starts(harness)) == 1
    assert TOKEN not in caplog.text


def test_run_polling_never_registers_commands() -> None:
    # SC-006: реєстрація — лише в main до потоку. Транспорт зі шпигуном: цикл опитування його не викликає.
    calls: list = []

    class SpyJournal(JournalTransport):
        def set_my_commands(self, commands) -> None:
            calls.append(list(commands))

    transport = SpyJournal([{"update_id": 1, "message": {"chat": {"id": 1}, "text": "hello"}}])

    run_polling(transport, None, evidence_limit=100, stop_after=1, sleep=lambda s: None)

    assert calls == []
