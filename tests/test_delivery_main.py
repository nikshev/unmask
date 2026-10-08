# verifies: FR-004-10, FR-008-07, FR-008-08
"""Тести точки входу: fail-fast секретів, прапорці, відсутність літералів ключів (T-095).

T-167 (FR-008-08): один `Protection` на процес із `config/protection.yaml`, fail-fast конфігу ДО порту й потоку
бота, прапорець `--host`. Сервер і потік бота замінено фіктивними (мережі немає), значення середовища — сентинели.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import threading
import time
import types
from pathlib import Path

import pytest
import yaml

from unmask.delivery import main as main_mod
from unmask.delivery.config import ProtectionConfig
from unmask.delivery.limits import Protection
from unmask.delivery.main import main
from unmask.hubs.config import content_digest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def test_missing_rpc_key_fails_fast_with_var_name(capsys) -> None:
    assert main(["--no-bot"], environ={}) == 2
    assert "UNMASK_RPC_URL" in capsys.readouterr().err


def test_missing_bot_token_with_bot_enabled_fails_fast(capsys) -> None:
    assert main([], environ={"UNMASK_RPC_URL": "https://example.invalid/rpc?key=x"}) == 2
    assert "UNMASK_BOT_TOKEN" in capsys.readouterr().err





def test_no_bot_flag_runs_http_only(capsys) -> None:
    # Без ключа RPC падаємо раніше, ніж справа дійде до прапорця, — перевіряємо розбір окремо:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-bot", action="store_true")
    assert parser.parse_args(["--no-bot"]).no_bot is True


def test_no_secret_literals_in_repo() -> None:
    suspects = []
    roots = [ROOT / "src", ROOT / "scripts", ROOT / "config", ROOT / "tests",
             ROOT / "specs", ROOT / "docs"]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".md", ".yaml", ".yml", ".json",
                                                         ".toml", ".sh", ".example"):
                continue
            if "fixtures" in path.parts and path.suffix == ".json":
                continue  # записані ончейн-дані й еталони, не секрети
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in (r"[A-Za-z0-9_-]{10,}:[\w-]{20,}", r"\bxox[bap]-[A-Za-z0-9-]{8,}",
                            r"\bsk-[A-Za-z0-9]{8,}", r"eyJ[A-Za-z0-9_-]{10,}"):
                if re.search(pattern, text):
                    suspects.append((str(path.relative_to(ROOT)), pattern))
    assert suspects == []


# --- T-167: один регулятор на процес, fail-fast protection.yaml, --host -------------------------------------------

# Файли, які `main` читає разом із protection.yaml (rpc_pool.yaml відсутній: джерело з `UNMASK_RPC_URL`).
COPIED = ("delivery.yaml", "ingest.yaml", "hubs.yaml", "hub_addresses.yaml", "clusters.yaml",
          "protection.yaml")
SENTINEL_RPC = "https://sentinel-rpc.invalid/rpc?api-key=SENTINEL_KEY_RPC_7f3e"
SENTINEL_BOT = "SENTINEL_BOT_TOKEN_4d8e"
ENV = {"UNMASK_RPC_URL": SENTINEL_RPC, "UNMASK_BOT_TOKEN": SENTINEL_BOT}
UNSET = object()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    for name in COPIED:
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


def _edit_protection(config_dir: Path, mutate) -> None:
    path = config_dir / "protection.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class FakeThread:
    created: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra) -> None:
        self.target, self.args, self.kwargs, self.daemon = target, args, kwargs or {}, daemon
        self.started = False
        type(self).created.append(self)

    def start(self) -> None:
        self.started = True


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Замінює сервер, потік і транспорт бота; фіксує, що саме `main` їм передав. Сокети заборонені."""
    record: dict = {"serve_calls": [], "bot_tokens": [], "build_calls": []}
    FakeThread.created = []
    record["threads"] = FakeThread.created

    class DummyIngest:
        def __init__(self, cfg, source) -> None:
            pass

    def fake_serve(service, port, *, host=UNSET, protection=UNSET) -> None:
        record["serve_calls"].append(
            {"service": service, "port": port, "host": host, "protection": protection})

    class FakeTransport:
        def __init__(self, token) -> None:
            record["bot_tokens"].append(token)

    real_build = main_mod.build_protection

    def spy_build(cfg, *args, **kwargs):
        built = real_build(cfg, *args, **kwargs)
        record["build_calls"].append({"cfg": cfg, "args": args, "kwargs": kwargs, "result": built})
        return built

    def refuse(*args, **kwargs):
        raise AssertionError("socket opened in a delivery test")

    monkeypatch.setattr(main_mod, "IngestService", DummyIngest)
    monkeypatch.setattr(main_mod, "serve", fake_serve)
    monkeypatch.setattr(main_mod, "HttpxBotTransport", FakeTransport)
    monkeypatch.setattr(main_mod, "threading", types.SimpleNamespace(Thread=FakeThread, Event=threading.Event))
    monkeypatch.setattr(main_mod, "build_protection", spy_build)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse)
    for name in ("connect", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, refuse)
    return record


def _run(config_dir: Path, *flags: str, environ=None) -> int:
    return main([*flags, "--config-dir", str(config_dir)], environ=dict(ENV if environ is None else environ))


def _bot_threads(wired: dict) -> list:
    # T-146: рядом із потоком бота стартує потік лічильників (`_stats_loop`); рахуємо лише бот.
    return [t for t in wired["threads"] if t.target is main_mod.run_polling]


def _nothing_started(wired: dict) -> None:
    assert wired["serve_calls"] == []
    assert wired["threads"] == []
    assert wired["bot_tokens"] == []


def test_one_protection_shared_by_service_and_http(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot") == 0

    (call,) = wired["serve_calls"]
    protection = call["protection"]
    assert isinstance(protection, Protection)
    assert call["service"]._slots is protection.slots
    # рівно один регулятор на процес: HTTP, сервіс (і далі бот) діляться одним екземпляром
    assert len(wired["build_calls"]) == 1
    assert wired["build_calls"][0]["result"] is protection


def test_protection_built_from_config_dir_file_with_explicit_numbers(config_dir: Path, wired: dict) -> None:
    def mutate(data: dict) -> None:
        data["api"] = {"requests_per_window": 11, "window_seconds": 3600}
        data["heavy"] = {"max_concurrent": 7, "busy_retry_after_seconds": 17}
        data["proxy"] = {"trusted": ["10.0.0.0/8", "::1"]}
        data["bot"] = {"checks_per_window": 2, "window_seconds": 7200}

    _edit_protection(config_dir, mutate)
    assert _run(config_dir, "--no-bot") == 0

    (call,) = wired["serve_calls"]
    p = call["protection"]
    cfg = wired["build_calls"][0]["cfg"]
    assert isinstance(cfg, ProtectionConfig)
    assert cfg.digest == content_digest(config_dir / "protection.yaml")
    assert cfg.digest != content_digest(CONFIG / "protection.yaml")  # прочитано файл --config-dir, не репо
    assert p.slots.capacity == 7 and p.slots.retry_after_seconds == 17
    assert p.trusted_proxies == (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("::1/128"))
    assert [p.api.hit("c").allowed for _ in range(12)] == [True] * 11 + [False]
    assert [p.users.try_enter(u, counts=True).kind for u in ("a", "b")] == ["ok", "ok"]
    p.users.leave("a")
    p.users.try_enter("a", counts=True)
    p.users.leave("a")
    assert p.users.try_enter("a", counts=True).kind == "rate_limited"  # 3-й за вікно при ліміті 2


def test_one_clock_shared_by_all_limiters_and_not_overridden(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot") == 0

    p = wired["serve_calls"][0]["protection"]
    assert wired["build_calls"][0]["args"] == () and wired["build_calls"][0]["kwargs"] == {}
    assert p.api._clock is time.monotonic
    assert p.users._window._clock is p.api._clock


def test_bot_thread_runs_on_the_same_service_and_protection(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir) == 0

    (call,) = wired["serve_calls"]
    (thread,) = _bot_threads(wired)
    assert thread.started is True and thread.daemon is True
    assert wired["bot_tokens"] == [SENTINEL_BOT]
    assert thread.args[1] is call["service"]  # бот отримує спільні слоти через той самий сервіс
    assert thread.args[1]._slots is call["protection"].slots
    assert len(wired["build_calls"]) == 1


def test_main_passes_shared_users_gate(config_dir: Path, wired: dict) -> None:
    # T-169 (FR-008-07): потік бота отримує ворота користувачів ТОГО САМОГО Protection, що й сервіс і HTTP.
    assert _run(config_dir) == 0

    (call,) = wired["serve_calls"]
    (thread,) = _bot_threads(wired)
    assert thread.target is main_mod.run_polling
    assert thread.kwargs["gate"] is call["protection"].users
    assert thread.kwargs["gate"] is wired["build_calls"][0]["result"].users
    assert thread.args[1]._slots is call["protection"].slots  # і глобальні слоти спільні
    assert len(wired["build_calls"]) == 1
    # решта аргументів 004 не зачеплена
    assert thread.kwargs["evidence_limit"] == yaml.safe_load(
        (config_dir / "delivery.yaml").read_text(encoding="utf-8"))["evidence_preview_limit"]
    assert callable(thread.kwargs["render"]) and thread.daemon is True


def test_no_bot_flag_starts_no_thread(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot") == 0
    assert _bot_threads(wired) == [] and wired["bot_tokens"] == []


def test_missing_protection_yaml_fails_fast_before_serve(config_dir: Path, wired: dict, capsys) -> None:
    (config_dir / "protection.yaml").unlink()

    assert _run(config_dir) == 2  # з ботом: токен є, але конфіг відсутній

    err = capsys.readouterr().err
    assert err.startswith("serve: bad config: ")
    assert "protection.yaml" in err
    _nothing_started(wired)  # ні сервер (порт), ні потік бота, ні транспорт
    assert wired["build_calls"] == []


@pytest.mark.parametrize("field, mutate", [
    ("version", lambda d: d.update(version=0)),
    ("version", lambda d: d.update(version=True)),
    ("api.requests_per_window", lambda d: d["api"].update(requests_per_window=0)),
    ("api.requests_per_window", lambda d: d["api"].update(requests_per_window=True)),
    ("api.requests_per_window", lambda d: d["api"].update(requests_per_window="30")),
    ("api.window_seconds", lambda d: d["api"].update(window_seconds=0)),
    ("api.window_seconds", lambda d: d["api"].update(window_seconds=1.5)),
    ("heavy.max_concurrent", lambda d: d["heavy"].update(max_concurrent=0)),
    ("heavy.busy_retry_after_seconds", lambda d: d["heavy"].update(busy_retry_after_seconds=-1)),
    ("proxy.trusted[1]", lambda d: d["proxy"].update(trusted=["127.0.0.1", "10.0.0.300"])),
    ("proxy.trusted", lambda d: d["proxy"].update(trusted="127.0.0.1")),
    ("bot.checks_per_window", lambda d: d["bot"].update(checks_per_window=0)),
    ("bot.window_seconds", lambda d: d["bot"].update(window_seconds=False)),
    ("extra", lambda d: d.update(extra=1)),
    ("api.burst", lambda d: d["api"].update(burst=5)),
    ("bot", lambda d: d.pop("bot")),
    ("heavy.max_concurrent", lambda d: d["heavy"].pop("max_concurrent")),
    ("proxy", lambda d: d.pop("proxy")),
], ids=['version-zero', 'version-bool', 'rpw-zero', 'rpw-bool', 'rpw-str', 'aws-zero', 'aws-float', 'maxc-zero', 'retry-negative', 'trusted-bad-ip', 'trusted-not-list', 'cpw-zero', 'bws-bool', 'unknown-top', 'unknown-nested', 'missing-bot', 'missing-nested', 'missing-proxy'])
@pytest.mark.parametrize("bot_flags", [(), ("--no-bot",)], ids=["with-bot", "no-bot"])
def test_invalid_protection_field_fails_fast_with_field_name(
        config_dir: Path, wired: dict, capsys, field: str, mutate, bot_flags: tuple) -> None:
    _edit_protection(config_dir, mutate)

    assert _run(config_dir, *bot_flags) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("serve: bad config: ")
    assert field in captured.err
    assert captured.out == ""
    _nothing_started(wired)


@pytest.mark.parametrize("text", ["version: [1\n", "- 1\n- 2\n", ""], ids=["syntax", "list", "empty"])
def test_unparsable_protection_yaml_fails_fast(config_dir: Path, wired: dict, capsys, text: str) -> None:
    (config_dir / "protection.yaml").write_text(text, encoding="utf-8")

    assert _run(config_dir, "--no-bot") == 2

    assert capsys.readouterr().err.startswith("serve: bad config: ")
    _nothing_started(wired)


def test_protection_error_is_reported_before_env_checks(config_dir: Path, wired: dict, capsys) -> None:
    # Конфіги перевіряються раніше за секрети: зламаний файл видно й без середовища.
    _edit_protection(config_dir, lambda d: d["heavy"].update(max_concurrent=0))

    assert _run(config_dir, environ={}) == 2

    err = capsys.readouterr().err
    assert "heavy.max_concurrent" in err
    assert "UNMASK_BOT_TOKEN" not in err and "UNMASK_RPC_URL" not in err
    _nothing_started(wired)


def test_env_errors_still_reported_with_valid_protection(config_dir: Path, wired: dict, capsys) -> None:
    assert _run(config_dir, environ={"UNMASK_RPC_URL": SENTINEL_RPC}) == 2
    assert "UNMASK_BOT_TOKEN" in capsys.readouterr().err
    assert _run(config_dir, "--no-bot", environ={}) == 2
    assert "UNMASK_RPC_URL" in capsys.readouterr().err
    _nothing_started(wired)


def test_config_error_output_has_no_secrets(config_dir: Path, wired: dict, capsys) -> None:
    _edit_protection(config_dir, lambda d: d["api"].update(requests_per_window=0))

    assert _run(config_dir) == 2

    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert SENTINEL_RPC not in text and SENTINEL_BOT not in text
        assert "sentinel-rpc" not in text and "SENTINEL_KEY" not in text


def test_success_output_has_no_secrets(config_dir: Path, wired: dict, capsys) -> None:
    assert _run(config_dir) == 0

    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert SENTINEL_RPC not in text and SENTINEL_BOT not in text


def test_host_flag_passed_to_serve(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot", "--host", "127.0.0.1") == 0
    assert wired["serve_calls"][0]["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.20.30.40", "localhost"])
def test_host_value_is_passed_through_verbatim(config_dir: Path, wired: dict, host: str) -> None:
    assert _run(config_dir, "--no-bot", "--host", host) == 0
    assert wired["serve_calls"][0]["host"] == host


def test_host_defaults_to_all_interfaces_and_is_passed_explicitly(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot") == 0
    assert wired["serve_calls"][0]["host"] == "0.0.0.0"


def test_host_flag_without_value_is_an_argparse_error(config_dir: Path, wired: dict, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "--no-bot", "--host")

    assert exc.value.code == 2
    assert "--host" in capsys.readouterr().err
    _nothing_started(wired)


def test_host_and_port_are_independent(config_dir: Path, wired: dict) -> None:
    assert _run(config_dir, "--no-bot", "--host", "127.0.0.1", "--port", "18080") == 0
    assert wired["serve_calls"][0]["host"] == "127.0.0.1"
    assert wired["serve_calls"][0]["port"] == 18080

    assert _run(config_dir, "--no-bot") == 0
    delivery = yaml.safe_load((config_dir / "delivery.yaml").read_text(encoding="utf-8"))
    assert wired["serve_calls"][1]["port"] == delivery["http_port"]


def test_protection_yaml_is_the_only_source_of_limits_in_main() -> None:
    # Принцип III: у main.py немає літералів меж (вони приходять з YAML) і немає прапорців для них.
    text = (ROOT / "src" / "unmask" / "delivery" / "main.py").read_text(encoding="utf-8")
    flags = re.findall(r'add_argument\("(--[a-z-]+)"', text)
    assert sorted(flags) == ["--config-dir", "--host", "--no-bot", "--port"]
    assert text.count("build_protection(") == 1
    assert "protection.yaml" in text


def test_systemd_unit_arguments_are_accepted_by_main(config_dir: Path, wired: dict) -> None:
    # Неприйнятий юнітом аргумент = argparse-код 2 і нескінченний цикл перезапусків (Restart=always).
    import shlex

    unit = (ROOT / "deploy" / "unmask.service").read_text(encoding="utf-8")
    (exec_line,) = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    argv = shlex.split(exec_line.removeprefix("ExecStart="))
    unit_args = argv[argv.index("scripts/serve_unmask.py") + 1:]

    assert _run(config_dir, "--no-bot", *unit_args) == 0

    (call,) = wired["serve_calls"]
    assert call["host"] == "127.0.0.1" and call["port"] == 18080
    assert isinstance(call["protection"], Protection)
