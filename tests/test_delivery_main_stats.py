# verifies: FR-007-13
"""T-146 (FR-007-13): лічильники пулу видно в логах сервісу.

Фоновий daemon-потік логує `source.stats_line()` раз на `STATS_LOG_SECONDS` і лише коли рядок змінився; без
`stats_line` потоку немає; збій `stats_line` не вбиває потік (одне попередження без тексту виключення); URL і ключі
з середовища в логах не з'являються. Сервер, потік бота й сокети замінено фіктивними (мережі немає).
"""

from __future__ import annotations

import logging
import shutil
import socket
import threading
import types
from pathlib import Path

import pytest
import yaml

from unmask.delivery import main as main_mod
from unmask.delivery.main import main

CONFIG = Path(__file__).resolve().parents[1] / "config"
COPIED = ("delivery.yaml", "ingest.yaml", "hubs.yaml", "hub_addresses.yaml", "clusters.yaml",
          "protection.yaml")
SENTINEL_RPC = "https://sentinel-stats.invalid/rpc?api-key=SENTINEL_KEY_STATS_51ab"
SENTINEL_BOT = "SENTINEL_BOT_TOKEN_STATS_0c9e"
ENV = {"UNMASK_RPC_URL": SENTINEL_RPC, "UNMASK_BOT_TOKEN": SENTINEL_BOT}


class ScriptedSource:
    """Джерело зі `stats_line`: віддає рядки за чергою; елемент-виняток піднімається."""

    def __init__(self, items) -> None:
        self._items = list(items)
        self.calls = 0

    def stats_line(self) -> str:
        self.calls += 1
        item = self._items.pop(0) if len(self._items) > 1 else self._items[0]
        if isinstance(item, Exception):
            raise item
        return item


class NoStatsSource:
    """Джерело без `stats_line` (наприклад, одиночний HTTP-провайдер до 007)."""


class ScriptedStop:
    """Подія зупинки: `wait` повертає False `ticks` разів (стільки кроків циклу), далі True."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.waits: list = []

    def wait(self, timeout=None) -> bool:
        self.waits.append(timeout)
        return len(self.waits) > self.ticks


def _unmask_records(caplog) -> list:
    return [r for r in caplog.records if r.name == "unmask.delivery"]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Фіксує порядок подій (старт потоків, serve) і потоки; нічого не запускає."""
    for name in COPIED:
        shutil.copy(CONFIG / name, tmp_path / name)
    record: dict = {"events": [], "threads": [], "sources": [], "config_dir": tmp_path}

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_) -> None:
            self.target, self.args, self.kwargs, self.daemon = target, args, kwargs or {}, daemon
            self.started = False
            record["threads"].append(self)

        def start(self) -> None:
            self.started = True
            record["events"].append(("start", self.target))

    class DummyIngest:
        def __init__(self, cfg, source) -> None:
            record["sources"].append(source)

    def fake_serve(service, port, **kwargs) -> None:
        record["events"].append(("serve", None))

    class FakeTransport:
        def __init__(self, token) -> None:
            pass

    def refuse(*args, **kwargs):
        raise AssertionError("socket opened in a delivery test")

    monkeypatch.setattr(main_mod, "IngestService", DummyIngest)
    monkeypatch.setattr(main_mod, "serve", fake_serve)
    monkeypatch.setattr(main_mod, "HttpxBotTransport", FakeTransport)
    monkeypatch.setattr(main_mod, "threading",
                        types.SimpleNamespace(Thread=FakeThread, Event=threading.Event))
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "bind", refuse)
    monkeypatch.setattr(socket.socket, "listen", refuse)
    return record


def _run(wired: dict, *flags: str, environ=None) -> int:
    argv = [*flags, "--config-dir", str(wired["config_dir"])]
    return main(argv, environ=dict(ENV if environ is None else environ))


def _stats_threads(wired: dict) -> list:
    return [t for t in wired["threads"] if t.target is main_mod._stats_loop]


# --- шаг логування: лише при зміні ------------------------------------------------------------------------------

def test_stats_line_logged_when_changed(caplog) -> None:
    source = ScriptedSource(["rpc_pool default requests=1", "rpc_pool default requests=2"])
    caplog.set_level(logging.INFO, logger="unmask.delivery")

    last = main_mod._log_stats_if_changed(source, "")
    last = main_mod._log_stats_if_changed(source, last)

    assert [r.getMessage() for r in _unmask_records(caplog)] == [
        "rpc_pool default requests=1", "rpc_pool default requests=2"]
    assert all(r.levelno == logging.INFO for r in _unmask_records(caplog))
    assert last == "rpc_pool default requests=2"


def test_not_logged_when_unchanged(caplog) -> None:
    source = ScriptedSource(["rpc_pool default requests=5"] * 3)
    caplog.set_level(logging.INFO, logger="unmask.delivery")

    last = ""
    for _ in range(3):
        last = main_mod._log_stats_if_changed(source, last)

    assert source.calls == 3
    assert [r.getMessage() for r in _unmask_records(caplog)] == ["rpc_pool default requests=5"]


# --- цикл: інтервал, збій, зупинка ------------------------------------------------------------------------------

def test_interval_is_reasonable() -> None:
    assert isinstance(main_mod.STATS_LOG_SECONDS, (int, float))
    assert 10 <= main_mod.STATS_LOG_SECONDS <= 300


def test_loop_pauses_by_interval_and_logs_once_per_change(caplog) -> None:
    source = ScriptedSource(["rpc_pool default requests=1"] * 3)
    stop = ScriptedStop(ticks=3)
    caplog.set_level(logging.INFO, logger="unmask.delivery")

    main_mod._stats_loop(source, main_mod.STATS_LOG_SECONDS, stop)

    assert stop.waits == [main_mod.STATS_LOG_SECONDS] * 4  # 3 кроки + вихід
    assert source.calls == 3
    assert len(_unmask_records(caplog)) == 1


def test_loop_survives_stats_error_and_warns_once(caplog) -> None:
    boom = RuntimeError(f"connect failed for {SENTINEL_RPC}")
    source = ScriptedSource([boom, boom, "rpc_pool default requests=7", "rpc_pool default requests=7"])
    stop = ScriptedStop(ticks=4)
    caplog.set_level(logging.INFO)

    main_mod._stats_loop(source, main_mod.STATS_LOG_SECONDS, stop)  # не піднімає

    assert source.calls == 4  # після збою цикл іде далі
    info = [r.getMessage() for r in _unmask_records(caplog) if r.levelno == logging.INFO]
    assert info == ["rpc_pool default requests=7"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # одне попередження на серію збоїв, а не на кожен крок
    assert "RuntimeError" in warnings[0].getMessage()
    assert "SENTINEL" not in caplog.text and "api-key" not in caplog.text and "connect failed" not in caplog.text
    assert "sentinel-stats" not in caplog.text


# --- потік у main: старт, daemon, порядок, порожній випадок -----------------------------------------------------

def test_stats_thread_is_daemon_started_before_serve(wired: dict) -> None:
    assert _run(wired, "--no-bot") == 0

    (thread,) = _stats_threads(wired)
    assert thread.daemon is True and thread.started is True
    assert thread.args[1] == main_mod.STATS_LOG_SECONDS
    assert wired["events"].index(("start", main_mod._stats_loop)) < wired["events"].index(("serve", None))


def test_stats_thread_starts_before_serve_with_bot_too(wired: dict) -> None:
    assert _run(wired) == 0

    stats = _stats_threads(wired)
    bot = [t for t in wired["threads"] if t.target is main_mod.run_polling]
    assert len(stats) == 1 and len(bot) == 1
    assert wired["events"][-1] == ("serve", None)
    assert wired["events"].index(("start", main_mod._stats_loop)) < wired["events"].index(("serve", None))


def test_stats_thread_reads_the_pool_source_of_main(wired: dict) -> None:
    assert _run(wired, "--no-bot") == 0

    (thread,) = _stats_threads(wired)
    assert thread.args[0] is wired["sources"][0]


def test_no_stats_thread_without_stats_line(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "build_rpc_source", lambda *a, **k: NoStatsSource())

    assert _run(wired, "--no-bot") == 0

    assert _stats_threads(wired) == []
    assert ("serve", None) in wired["events"]


@pytest.mark.parametrize("mode", ["bad_protection", "no_bot_token"])
def test_no_stats_thread_when_config_fails_fast(wired: dict, capsys, mode: str) -> None:
    if mode == "bad_protection":
        path = wired["config_dir"] / "protection.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["heavy"]["max_concurrent"] = 0
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        environ = None
    else:
        environ = {"UNMASK_RPC_URL": SENTINEL_RPC}

    assert _run(wired, environ=environ) == 2

    assert wired["threads"] == [] and wired["events"] == []
    assert capsys.readouterr().out == ""


# --- безпека: без URL/ключів ------------------------------------------------------------------------------------

def test_log_has_no_urls_or_keys(wired: dict, capsys, caplog) -> None:
    # Справжнє джерело з `build_rpc_source` і сентинелом у середовищі; його рядок логується в усіх випадках.
    assert _run(wired, "--no-bot") == 0
    (source,) = wired["sources"]
    caplog.set_level(logging.INFO)

    main_mod._log_stats_if_changed(source, "")

    msgs = [r.getMessage() for r in _unmask_records(caplog)]
    assert len(msgs) == 1 and msgs[0].startswith("rpc_pool ")
    captured = capsys.readouterr()
    for text in (caplog.text, captured.out, captured.err, *msgs):
        assert SENTINEL_RPC not in text and "SENTINEL_KEY_STATS" not in text and "api-key" not in text
        assert "https://" not in text and SENTINEL_BOT not in text


# --- логування налаштовано один раз, без дублів -----------------------------------------------------------------

def test_logging_configured_once_when_root_has_no_handlers(wired: dict) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers[:] = []
        assert _run(wired, "--no-bot") == 0
        assert len(root.handlers) == 1
        assert root.level == logging.INFO
        assert _run(wired, "--no-bot") == 0
        assert len(root.handlers) == 1  # повторний виклик не додає хендлер
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_logging_not_reconfigured_when_root_has_handlers(wired: dict) -> None:
    root = logging.getLogger()
    before = root.handlers[:]
    assert before, "pytest має додати хендлери на root"

    assert _run(wired, "--no-bot") == 0

    assert root.handlers == before
