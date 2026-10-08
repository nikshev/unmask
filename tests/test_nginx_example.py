# verifies: FR-008-12
"""Статичні перевірки прикладу зворотного проксі `deploy/nginx-unmask.conf.example` (T-171).

Мережі немає: лише читання файла з репозиторію. Перевіряємо, що приклад:
- обслуговує HTTPS (`listen 443 ssl`) з плейсхолдерами домену й сертифікатів;
- блок порту 80 лише перенаправляє на HTTPS, без `proxy_pass`;
- перезаписує `X-Forwarded-For` адресою з'єднання (`$remote_addr`), а не доклеює;
- має `proxy_read_timeout 660s` (холодний аналіз до 600 с);
- проксіює на loopback-порт юніта (18080);
- не містить секретів і реальних адрес оператора.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "deploy" / "nginx-unmask.conf.example"
INGEST_YAML = ROOT / "config" / "ingest.yaml"
# Запас понад бюджет аналізу: таймаут nginx має перевищувати time_budget_seconds.
TIMEOUT_MARGIN_SECONDS = 30

UNIT_PORT = 18080
PLACEHOLDER_DOMAIN = "example.com"

# Адреси, дозволені в прикладі: loopback і wildcard-прив'язка з коментаря (0.0.0.0 — не адреса
# оператора). Будь-яка інша IPv4-літерал вважається реальною адресою.
ALLOWED_IPV4 = {"127.0.0.1", "0.0.0.0"}

SECRET_PATTERNS = [
    re.compile(r"api[-_]?key\s*=", re.IGNORECASE),
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}"),  # токен Telegram-бота
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
]
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _read() -> str:
    assert CONF.is_file(), f"немає прикладу конфігу: {CONF.relative_to(ROOT)}"
    return CONF.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """Текст без коментарів (усе, що після `#` до кінця рядка)."""
    return "\n".join(re.sub(r"#.*", "", line) for line in text.splitlines())


def _server_blocks(text: str) -> list[str]:
    """Тіла всіх `server { ... }` з урахуванням вкладених дужок."""
    blocks: list[str] = []
    code = _code(text)
    for m in re.finditer(r"\bserver\s*\{", code):
        depth = 0
        for i in range(m.end() - 1, len(code)):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(code[m.end():i])
                    break
    return blocks


def _tls_block() -> str:
    blocks = [b for b in _server_blocks(_read()) if re.search(r"\blisten\s+443\b", b)]
    assert len(blocks) == 1, "очікується рівно один server-блок з listen 443"
    return blocks[0]


def _redirect_block() -> str:
    blocks = [b for b in _server_blocks(_read()) if re.search(r"\blisten\s+80\b", b)]
    assert len(blocks) == 1, "очікується рівно один server-блок з listen 80"
    return blocks[0]


def test_tls_listen_443_ssl_with_placeholder_certificates() -> None:
    tls = _tls_block()
    # `listen 443 ssl http2;` працює на nginx 1.24; окрема `http2 on;` є лише з 1.25.1.
    assert re.search(r"\blisten\s+443\s+ssl\s+http2;", tls), (
        "блок 443 має бути `listen 443 ssl http2;`"
    )
    assert not re.search(r"\bhttp2\s+on;", tls), "`http2 on;` ламає nginx 1.24"
    certs = re.findall(r"\bssl_certificate(?:_key)?\s+(\S+?);", tls)
    assert len(certs) == 2, "очікуються ssl_certificate і ssl_certificate_key"
    for path in certs:
        assert path.startswith(f"/etc/letsencrypt/live/{PLACEHOLDER_DOMAIN}/"), (
            f"сертифікат має бути плейсхолдером example.com: {path}"
        )


def test_port80_only_redirects_to_https() -> None:
    redirect = _redirect_block()
    assert "return 301 https://$host$request_uri;" in redirect, (
        "блок 80 має перенаправляти: `return 301 https://$host$request_uri;`"
    )
    assert "proxy_pass" not in redirect, "блок 80 не має проксіювати незашифровано"


def test_overwrites_xff_with_remote_addr() -> None:
    tls = _tls_block()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in tls, (
        "X-Forwarded-For має перезаписуватися через $remote_addr"
    )
    assert "$proxy_add_x_forwarded_for" not in _code(_read()), (
        "$proxy_add_x_forwarded_for доклеює підроблений клієнтом ланцюг — заборонено"
    )


def _time_budget_seconds() -> int:
    data = yaml.safe_load(INGEST_YAML.read_text(encoding="utf-8"))
    return int(data["time_budget_seconds"])


def test_read_timeout_exceeds_analysis_budget() -> None:
    tls = _tls_block()
    budget = _time_budget_seconds()
    for directive in ("proxy_read_timeout", "proxy_send_timeout"):
        m = re.search(rf"\b{directive}\s+(\d+)s;", tls)
        assert m, f"немає директиви {directive} з таймаутом у секундах"
        seconds = int(m.group(1))
        assert seconds >= budget + TIMEOUT_MARGIN_SECONDS, (
            f"{directive} {seconds}s має бути ≥ time_budget_seconds ({budget}) + "
            f"{TIMEOUT_MARGIN_SECONDS}s, інакше холодний аналіз отримає 504"
        )


def test_proxies_to_loopback_unit_port() -> None:
    tls = _tls_block()
    m = re.search(r"\bproxy_pass\s+http://127\.0\.0\.1:(\d+);", tls)
    assert m, "proxy_pass має вказувати на http://127.0.0.1:<порт>"
    assert int(m.group(1)) == UNIT_PORT, f"порт має збігатися з юнітом: {UNIT_PORT}"


def test_no_secrets_and_no_operator_addresses() -> None:
    text = _read()
    for pat in SECRET_PATTERNS:
        assert not pat.search(text), f"схоже на секрет: {pat.pattern}"
    addrs = set(IPV4_RE.findall(text))
    assert addrs <= ALLOWED_IPV4, f"реальні IP у прикладі заборонено: {sorted(addrs - ALLOWED_IPV4)}"


def test_only_placeholder_domain_is_used() -> None:
    names = set(re.findall(r"\bserver_name\s+([^;]+);", _code(_read())))
    assert names, "server_name не знайдено"
    assert names == {PLACEHOLDER_DOMAIN}, f"лише плейсхолдер домену: {names}"


def test_comments_explain_trusted_proxy_and_loopback_bind() -> None:
    comments = "\n".join(
        line for line in _read().splitlines() if line.lstrip().startswith("#")
    )
    assert "config/protection.yaml" in comments, "коментар має посилатися на довірені адреси"
    assert "127.0.0.1" in comments and "::1" in comments, "коментар має назвати довірені адреси проксі"
    lowered = comments.lower()
    assert "18080" in comments and "--host 127.0.0.1" in comments, (
        "коментар має пояснити, що юніт слухає 18080 лише на loopback через --host 127.0.0.1"
    )
    assert "фаєрвол" in lowered or "ufw" in lowered, (
        "коментар має пояснити, як закрити порт 18080 при запуску вручну без --host"
    )
    for stale in ("Поки прапорця", "Після появи", "задача T-167"):
        assert stale not in comments, f"застарілий коментар про відсутній --host: {stale}"


def test_comment_host_matches_unit_execstart() -> None:
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "unmask.service").read_text(encoding="utf-8")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--host 127.0.0.1" in exec_line
    assert "--host 127.0.0.1" in _read(), "коментар прикладу має називати той самий --host, що й юніт"
