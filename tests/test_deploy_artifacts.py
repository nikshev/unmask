# verifies: FR-008-11, FR-008-13
"""Перевірки артефактів розгортання (T-170): юніт systemd і `deploy/smoke.sh`.

Мережі немає: `curl` і `systemctl` підміняються фейковими скриптами в PATH, які
журналюють виклики і друкують задані коди. systemd не викликається.
Секретів у `deploy/` бути не повинно: юніт бере змінні з файла оператора, скрипт не
містить ключів у URL.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
UNIT = DEPLOY / "unmask.service"
SMOKE = DEPLOY / "smoke.sh"

# Токен бота Telegram: `<8–10 цифр>:<35 символів>`.
BOT_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
API_KEY_RE = re.compile(r"(?i)api[-_]?key=")
URL_RE = re.compile(r"https?://\S+")
SECRET_QUERY_RE = re.compile(r"(?i)[?&](api[-_]?key|key|token|secret|password)=")
IMPL_HEADER_RE = re.compile(r"impl:\s*FR-008-11,\s*FR-008-12,\s*FR-008-13")

BASH = shutil.which("bash") or "/usr/bin/bash"


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} відсутній"
    return path.read_text(encoding="utf-8")


def _unit_values(text: str, key: str) -> list[str]:
    """Усі значення `key=` у юніті (коментарі ігноруються)."""
    values: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")) or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            values.append(v.strip())
    return values


# --- юніт systemd -------------------------------------------------------------


def test_unit_restarts_on_failure():
    text = _read(UNIT)
    assert _unit_values(text, "Restart") == ["always"]
    assert _unit_values(text, "RestartSec") == ["5"]


def test_unit_env_from_operator_file_not_inline():
    text = _read(UNIT)
    assert _unit_values(text, "EnvironmentFile") == ["/opt/unmask/.env"]
    assert _unit_values(text, "Environment") == []
    directives = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert "UNMASK_" not in directives


def test_unit_execstart_uses_venv_python_not_uv_run():
    text = _read(UNIT)
    exec_start = _unit_values(text, "ExecStart")
    assert len(exec_start) == 1, exec_start
    cmd = exec_start[0]
    assert cmd.startswith("/opt/unmask/.venv/bin/python scripts/serve_unmask.py"), cmd
    assert "--port 18080" in cmd, cmd
    assert "uv" not in cmd.split(), cmd


def test_unit_working_dir_and_hardening():
    text = _read(UNIT)
    assert _unit_values(text, "WorkingDirectory") == ["/opt/unmask"]
    assert _unit_values(text, "NoNewPrivileges") in (["yes"], ["true"])
    assert any("network-online.target" in v for v in _unit_values(text, "After"))
    assert _unit_values(text, "User") == []
    assert _unit_values(text, "DynamicUser") == []


def test_unit_has_install_section():
    text = _read(UNIT)
    assert "[Install]" in text
    assert _unit_values(text, "WantedBy") == ["multi-user.target"]


def test_no_secrets_in_deploy_dir():
    assert DEPLOY.is_dir(), "каталог deploy/ відсутній"
    _read(UNIT)
    _read(SMOKE)
    for path in sorted(DEPLOY.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        assert not BOT_TOKEN_RE.search(text), f"{rel}: схоже на токен бота"
        assert not API_KEY_RE.search(text), f"{rel}: містить api-key="


# --- smoke.sh: статика --------------------------------------------------------


def test_smoke_script_syntax_and_checks():
    text = _read(SMOKE)
    result = subprocess.run([BASH, "-n", str(SMOKE)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert text.startswith("#!"), "очікується shebang"
    assert re.search(r"^set -euo pipefail", text, re.MULTILINE), "очікується strict mode"
    header = "\n".join(text.splitlines()[:5])
    assert IMPL_HEADER_RE.search(header), header
    # три обов'язкові перевірки розгортання
    assert "systemctl show unmask -p Restart" in text
    assert "/healthz" in text
    assert "/api/token/" in text


def test_smoke_script_urls_carry_no_keys():
    text = _read(SMOKE)
    for url in URL_RE.findall(text):
        assert not SECRET_QUERY_RE.search(url), f"URL з ключем у запиті: {url}"
    assert not BOT_TOKEN_RE.search(text)


# --- smoke.sh: поведінка з фейковими curl і systemctl ------------------------

FAKE_CURL = r"""#!{bash}
printf '%s\n' "$*" >> "$FAKE_LOG"
url="${{*: -1}}"
case "$url" in
  http://127.0.0.1*/healthz) code="${{FAKE_LOCAL_HEALTHZ:-200}}" ;;
  https://*/healthz)         code="${{FAKE_HTTPS_HEALTHZ:-200}}" ;;
  http://*/healthz)          code="${{FAKE_HTTP_HEALTHZ:-301}}" ;;
  */api/token/burst-*)       code="${{FAKE_BURST:-429}}" ;;
  */api/token/*)             code="${{FAKE_MINT:-400}}" ;;
  *)                         code="999" ;;
esac
printf '%s' "$code"
"""

FAKE_SYSTEMCTL = r"""#!{bash}
printf 'systemctl %s\n' "$*" >> "$FAKE_LOG"
printf '%s\n' "${{FAKE_RESTART:-always}}"
"""


@pytest.fixture
def fake_bin(tmp_path):
    """Каталог із фейковим curl (і systemctl за замовчуванням) і журнал викликів."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL.format(bash=BASH), encoding="utf-8")
    curl.chmod(0o755)
    systemctl = bindir / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL.format(bash=BASH), encoding="utf-8")
    systemctl.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    return bindir, log


def _run_smoke(fake_bin, args, env_extra=None, path_dirs=None):
    bindir, log = fake_bin
    env = {
        "PATH": os.pathsep.join(path_dirs) if path_dirs is not None else f"{bindir}{os.pathsep}/usr/bin:/bin",
        "FAKE_LOG": str(log),
    }
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [BASH, str(SMOKE), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    return result, calls


def _curl_urls(calls):
    return [c.split()[-1] for c in calls if not c.startswith("systemctl")]


def test_smoke_local_ok_on_healthz_200_and_mint_400(fake_bin):
    result, calls = _run_smoke(fake_bin, ["--local", "http://127.0.0.1:18080"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fail=0" in result.stdout
    assert _curl_urls(calls) == [
        "http://127.0.0.1:18080/healthz",
        "http://127.0.0.1:18080/api/token/not-a-mint",
    ]


def test_smoke_local_requests_only_healthz_and_invalid_mint(fake_bin):
    _, calls = _run_smoke(fake_bin, ["--local", "http://127.0.0.1:18080"])
    for line in calls:
        assert "--max-time 10" in line, line
        assert "systemctl" not in line
    for url in _curl_urls(calls):
        assert url.endswith("/healthz") or url.endswith("/api/token/not-a-mint"), url


def test_smoke_local_fails_when_healthz_not_200(fake_bin):
    result, _ = _run_smoke(fake_bin, ["--local"], env_extra={"FAKE_LOCAL_HEALTHZ": "201"})
    assert result.returncode == 1
    assert "очікувався HTTP 200" in result.stdout


def test_smoke_local_fails_when_mint_not_400(fake_bin):
    result, _ = _run_smoke(fake_bin, ["--local"], env_extra={"FAKE_MINT": "200"})
    assert result.returncode == 1
    assert "очікувався HTTP 400" in result.stdout


def test_smoke_local_fails_on_connection_error(fake_bin):
    result, _ = _run_smoke(fake_bin, ["--local"], env_extra={"FAKE_LOCAL_HEALTHZ": "000"})
    assert result.returncode == 1
    assert "отримано 000" in result.stdout


def test_smoke_local_burst_detects_429(fake_bin):
    result, calls = _run_smoke(fake_bin, ["--local", "http://127.0.0.1:18080", "3"])
    assert result.returncode == 0, result.stdout
    burst_urls = [u for u in _curl_urls(calls) if "burst-" in u]
    assert len(burst_urls) == 3


def test_smoke_local_burst_without_429_fails(fake_bin):
    result, _ = _run_smoke(fake_bin, ["--local", "http://127.0.0.1:18080", "2"],
                           env_extra={"FAKE_BURST": "400"})
    assert result.returncode == 1
    assert "жодного 429" in result.stdout


def test_smoke_domain_all_checks_ok(fake_bin):
    result, calls = _run_smoke(fake_bin, ["unmask.example.org"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fail=0" in result.stdout
    assert "systemctl show unmask -p Restart --value" in " ".join(calls)
    assert _curl_urls(calls) == [
        "http://unmask.example.org/healthz",
        "https://unmask.example.org/healthz",
        "https://unmask.example.org/api/token/not-a-mint",
    ]


def test_smoke_domain_fails_when_restart_not_always(fake_bin):
    result, _ = _run_smoke(fake_bin, ["unmask.example.org"], env_extra={"FAKE_RESTART": "on-failure"})
    assert result.returncode == 1
    assert "Restart=on-failure" in result.stdout


def test_smoke_domain_fails_without_systemctl(fake_bin):
    bindir, _ = fake_bin
    (bindir / "systemctl").unlink()
    result, _ = _run_smoke(fake_bin, ["unmask.example.org"], path_dirs=[str(bindir)])
    assert result.returncode == 1
    assert "systemctl недоступний" in result.stdout


def test_smoke_domain_http_must_redirect_301(fake_bin):
    result, _ = _run_smoke(fake_bin, ["unmask.example.org"], env_extra={"FAKE_HTTP_HEALTHZ": "200"})
    assert result.returncode == 1
    assert "очікувався HTTP 301" in result.stdout


def test_smoke_domain_https_healthz_must_be_200(fake_bin):
    result, _ = _run_smoke(fake_bin, ["unmask.example.org"], env_extra={"FAKE_HTTPS_HEALTHZ": "502"})
    assert result.returncode == 1


def test_smoke_domain_https_mint_must_be_400(fake_bin):
    result, _ = _run_smoke(fake_bin, ["unmask.example.org"], env_extra={"FAKE_MINT": "200"})
    assert result.returncode == 1


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["a.example", "extra"],
        ["bad domain!"],
        ["--local", "ftp://x"],
        ["--local", "http://127.0.0.1:18080", "many"],
        ["--local", "http://127.0.0.1:18080", "1", "extra"],
    ],
)
def test_smoke_usage_errors_exit_2(fake_bin, args):
    result, calls = _run_smoke(fake_bin, args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage:" in result.stderr
    assert _curl_urls(calls) == []
