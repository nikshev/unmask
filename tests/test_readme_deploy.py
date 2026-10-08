# verifies: FR-008-13
"""Перевірки розділу `## Deploy` у README.md (T-172).

Мережі немає: лише читання файлів репозиторію. Числа лімітів беруться з
config/protection.yaml, а не вшиваються в тест: якщо YAML змінено, а README не
оновлено, тест червоніє. Адреса демо-інстансу в README — лише плейсхолдер,
яку додає оператор (FR-008-13).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PROTECTION = ROOT / "config" / "protection.yaml"
NGINX = ROOT / "deploy" / "nginx-unmask.conf.example"
UNIT = ROOT / "deploy" / "unmask.service"
SMOKE = ROOT / "deploy" / "smoke.sh"
RPC_POOL = ROOT / "config" / "rpc_pool.yaml"
RPC_QUICKSTART = ROOT / "specs" / "007-rpc-provider-pool" / "quickstart.md"

# Ключові елементи розділу Deploy (FR-008-13 і нотатки T-172).
REQUIRED_TERMS = [
    "uv sync --frozen --no-dev",
    "chmod 600",
    "UNMASK_RPC_URL=",
    "UNMASK_BOT_TOKEN=",
    "systemctl",
    "daemon-reload",
    "enable --now",
    "journalctl",
    "EnvironmentFile",
    "/healthz",
    "nginx",
    "certbot",
    "smoke.sh",
    "protection.yaml",
    "CHANGELOG.md",
    "429",
    "503",
    "Retry-After",
    "rpc_pool.yaml",
    "UNMASK_RPC_URL_ALT",
    "18080",
    "Demo instance",
    "added by the operator",
    "systemd (`EnvironmentFile=`) and bash",
    "unquoted `&`",
    "resets all counters",
    "--standalone",
    "--pre-hook",
    "%{http_code}",
    "smoke.sh --local",
    "Restart=always",
    "proxy_read_timeout 660s",
    "code 2",
]

# Файли, на які розділ має посилатися і які мають існувати в репозиторії.
REFERENCED_FILES = [
    "deploy/unmask.service",
    "deploy/smoke.sh",
    "deploy/nginx-unmask.conf.example",
    "config/protection.yaml",
    "config/rpc_pool.yaml",
    "config/CHANGELOG.md",
]

# Схожі на секрети рядки: ключ API, токен Telegram-бота, реальна адреса оператора.
API_KEY_RE = re.compile(r"(?i)api[-_]?key=")
BOT_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
OPERATOR_IP = "195.201.60.40"
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
LOOPBACK_IPS = {"127.0.0.1", "0.0.0.0"}

LIMIT_KEYS = [
    "api.requests_per_window",
    "api.window_seconds",
    "heavy.max_concurrent",
    "heavy.busy_retry_after_seconds",
    "bot.checks_per_window",
    "bot.window_seconds",
]


def _read_readme() -> str:
    assert README.is_file(), "README.md відсутній"
    return README.read_text(encoding="utf-8")


def _deploy_section(text: str) -> str:
    """Текст від `## Deploy` до наступного заголовка другого рівня."""
    m = re.search(r"^## Deploy[ \t]*$", text, re.MULTILINE)
    assert m, "розділ `## Deploy` відсутній у README.md"
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _limits_section(deploy: str) -> str:
    """Підрозділ `### Limits` усередині розділу Deploy."""
    m = re.search(r"^### Limits[ \t]*$", deploy, re.MULTILINE)
    assert m, "у розділі Deploy немає підрозділу `### Limits`"
    rest = deploy[m.end():]
    nxt = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _yaml_limits() -> dict[str, int]:
    data = yaml.safe_load(PROTECTION.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for dotted in LIMIT_KEYS:
        node = data
        for part in dotted.split("."):
            node = node[part]
        out[dotted] = node
    return out


def test_readme_has_deploy_section_with_required_steps():
    deploy = _deploy_section(_read_readme())
    missing = [term for term in REQUIRED_TERMS if term not in deploy]
    assert not missing, f"у розділі Deploy немає: {missing}"


def test_readme_deploy_has_no_secrets():
    text = _read_readme()
    assert not API_KEY_RE.search(text), "README містить рядок, схожий на api-key="
    assert not BOT_TOKEN_RE.search(text), "README містить рядок, схожий на токен бота"
    assert OPERATOR_IP not in text, "README містить реальну адресу оператора"
    for ip in IPV4_RE.findall(text):
        assert ip in LOOPBACK_IPS, f"README містить IPv4-адресу, що не є loopback: {ip}"


def test_readme_limits_match_protection_yaml():
    limits = _limits_section(_deploy_section(_read_readme()))
    values = _yaml_limits()
    for dotted, value in values.items():
        pattern = rf"`{re.escape(dotted)}`[^\n]*(?<![\d.]){value}(?![\d.])"
        assert re.search(pattern, limits), f"у Limits немає {dotted} = {value} з config/protection.yaml"
    # Ключові числа v1 присутні в розділі лімітів (30 запитів / 60 с, 3 одночасні аналізи).
    for number in (30, 60, 3):
        assert re.search(rf"(?<![\d.]){number}(?![\d.])", limits), f"у Limits немає числа {number}"


def test_readme_references_existing_deploy_files():
    text = _read_readme()
    deploy = _deploy_section(text)
    for rel in REFERENCED_FILES:
        assert (ROOT / rel).is_file(), f"файл {rel} відсутній у репозиторії"
        assert rel in deploy, f"розділ Deploy не посилається на {rel}"
    for ref in re.findall(r"`((?:deploy|config|scripts|tests)/[^`\s]+)`", deploy):
        assert (ROOT / ref).exists(), f"розділ Deploy посилається на неіснуючий файл: {ref}"


def test_readme_deploy_urls_use_placeholder_hosts_only():
    deploy = _deploy_section(_read_readme())
    for url in re.findall(r"https?://[^\s`)]+", deploy):
        host = url.split("://", 1)[1].split("/", 1)[0]
        ok = "<" in host or host == "example.com" or host.split(":", 1)[0] == "127.0.0.1"
        assert ok, f"розділ Deploy містить не плейсхолдер-URL: {url}"


def test_smoke_burst_example_exceeds_per_ip_limit():
    """BURST у прикладі має перевищувати api.requests_per_window, інакше 429 не буде (FAIL)."""
    deploy = _deploy_section(_read_readme())
    bursts = [int(n) for n in re.findall(r"deploy/smoke\.sh --local\s+\S+\s+(\d+)", deploy)]
    assert bursts, "у розділі немає прикладу `smoke.sh --local <URL> <BURST>`"
    limit = _yaml_limits()["api.requests_per_window"]
    for burst in bursts:
        assert burst > limit, f"BURST={burst} не перевищує api.requests_per_window={limit}: 429 не буде"


def test_readme_matches_deploy_artifacts():
    """Значення, що повторюються в README і в deploy/, мають збігатися."""
    deploy = _deploy_section(_read_readme())
    unit = UNIT.read_text(encoding="utf-8")
    nginx = NGINX.read_text(encoding="utf-8")
    smoke = SMOKE.read_text(encoding="utf-8")

    assert re.search(r"^Restart=always$", unit, re.MULTILINE), "юніт не має Restart=always"
    assert "Restart=always" in deploy

    m = re.search(r"--port (\d+)", unit)
    assert m, "у юніті немає --port"
    port = m.group(1)
    assert f"127.0.0.1:{port}" in nginx, "nginx проксіює не на порт юніта"
    assert f"127.0.0.1:{port}" in deploy, "README згадує інший порт"
    assert f"--port {port}" in deploy, "README згадує інший порт запуску"

    m = re.search(r"proxy_read_timeout\s+(\d+)s", nginx)
    assert m, "у прикладі nginx немає proxy_read_timeout"
    assert f"proxy_read_timeout {m.group(1)}s" in deploy, "README має інший proxy_read_timeout"

    assert "--local" in smoke, "smoke.sh не має режиму --local"
    assert "smoke.sh --local" in deploy

    trusted = yaml.safe_load(PROTECTION.read_text(encoding="utf-8"))["proxy"]["trusted"]
    for addr in trusted:
        assert f"`{addr}`" in deploy, f"README не згадує довірений проксі {addr}"


def test_readme_rpc_pool_matches_config():
    deploy = _deploy_section(_read_readme())
    pool = yaml.safe_load(RPC_POOL.read_text(encoding="utf-8"))
    for provider in pool["providers"]:
        assert provider["url_env"] in deploy, f"README не називає змінну {provider['url_env']}"
    rpcfast = next(p for p in pool["providers"] if p["name"] == "rpcfast")
    assert rpcfast["url_env"] == "UNMASK_RPC_URL"
    for method in rpcfast["skip_methods"]:
        assert method in deploy, f"README не пояснює, що rpcfast не обслуговує {method}"
    assert "code 2" in deploy
    assert "код 2" in RPC_QUICKSTART.read_text(encoding="utf-8"), "джерело для коду 2 змінилось"
