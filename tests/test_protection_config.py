# verifies: FR-008-08
"""Версіонований `config/protection.yaml` v1 (T-161): значення, журнал, завантажувач `load_protection_config`.

Принцип III: межі захисту API (запити на вікно, довжина вікна, одночасні аналізи, `Retry-After`, довірені
адреси проксі, ліміти бота) живуть у YAML під git; будь-яка зміна значення без запису в `config/CHANGELOG.md`
червонить тест. Мережі немає: усе — на поставленому файлі й tmp-копіях.

Golden-значення (`GOLDEN`) і дайджест (`PROTECTION_V1_SHA`) зафіксовано в тесті окремо від файла; дайджест
незалежно обчислюється з `yaml.safe_load` → канонічний JSON → sha256 (правило research R-14 002).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from unmask.delivery.config import load_protection_config
from unmask.hubs.config import ConfigError, changelog_entries, check_changelog, content_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "config" / "CHANGELOG.md"
PROTECTION_YAML = REPO_ROOT / "config" / "protection.yaml"
DELIVERY_YAML = REPO_ROOT / "config" / "delivery.yaml"

# Золоте значення v1 (дослівно з research R-7 і data-model `ProtectionConfig`).
GOLDEN = {
    "version": 1,
    "api": {"requests_per_window": 30, "window_seconds": 60},
    "heavy": {"max_concurrent": 3, "busy_retry_after_seconds": 60},
    "proxy": {"trusted": ["127.0.0.1", "::1"]},
    "bot": {"checks_per_window": 5, "window_seconds": 600},
}

# Дайджест v1: змінити його можна лише новою версією файла і записом у журналі.
PROTECTION_V1_SHA = "13212e00027fb65d16abdf5ab8fe67e57d5042104176c68a7c90b2bb6d12ece2"

DELIVERY_V1_SHA = "74dae8a76af17497df61b0f029e2afd27bdf4298ad8da8e6d6be7e1fc095e778"

# Листові поля: шлях у YAML (ключі, а для списку — індекс).
LEAVES = [
    pytest.param(("version",), id="version"),
    pytest.param(("api", "requests_per_window"), id="api.requests_per_window"),
    pytest.param(("api", "window_seconds"), id="api.window_seconds"),
    pytest.param(("heavy", "max_concurrent"), id="heavy.max_concurrent"),
    pytest.param(("heavy", "busy_retry_after_seconds"), id="heavy.busy_retry_after_seconds"),
    pytest.param(("proxy", "trusted", 0), id="proxy.trusted[0]"),
    pytest.param(("proxy", "trusted", 1), id="proxy.trusted[1]"),
    pytest.param(("bot", "checks_per_window"), id="bot.checks_per_window"),
    pytest.param(("bot", "window_seconds"), id="bot.window_seconds"),
]


def _canonical_sha(data) -> str:
    """Незалежний еталон дайджесту (без `hubs.config`): канонічний JSON → sha256."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_yaml(tmp_path: Path, data, name: str = "protection.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _golden_copy() -> dict:
    return yaml.safe_load(yaml.safe_dump(GOLDEN))


def _set_leaf(data: dict, path: tuple, value) -> dict:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


def _get_leaf(data: dict, path: tuple):
    node = data
    for key in path:
        node = node[key]
    return node


# --- поставний файл і журнал ----------------------------------------------------------------------


def test_shipped_protection_yaml_passes_check_changelog() -> None:
    check_changelog(PROTECTION_YAML, CHANGELOG)


def test_entry_sha_equals_content_digest() -> None:
    entries = changelog_entries(CHANGELOG, "protection.yaml")
    assert entries == {1: content_digest(PROTECTION_YAML)}
    # незалежне обчислення з golden-значень (ті самі значення, без файла) дає той самий дайджест
    assert _canonical_sha(GOLDEN) == PROTECTION_V1_SHA
    assert entries == {1: PROTECTION_V1_SHA}


def test_protection_section_before_ingest_and_ingest_is_last() -> None:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    headings = [line for line in lines if line.startswith("# ")]
    assert "# config/protection.yaml" in headings
    assert headings.index("# config/protection.yaml") < headings.index("# config/ingest.yaml")
    assert headings[-1] == "# config/ingest.yaml"


def test_delivery_yaml_digest_unchanged() -> None:
    assert changelog_entries(CHANGELOG, "delivery.yaml") == {1: DELIVERY_V1_SHA}
    assert content_digest(DELIVERY_YAML) == DELIVERY_V1_SHA


# --- значення й завантажувач ----------------------------------------------------------------------


def test_shipped_values_load() -> None:
    cfg = load_protection_config(PROTECTION_YAML)

    assert cfg.version == 1 and type(cfg.version) is int
    assert cfg.api_requests_per_window == 30 and type(cfg.api_requests_per_window) is int
    assert cfg.api_window_seconds == 60 and type(cfg.api_window_seconds) is int
    assert cfg.max_concurrent_analyses == 3 and type(cfg.max_concurrent_analyses) is int
    assert cfg.busy_retry_after_seconds == 60 and type(cfg.busy_retry_after_seconds) is int
    assert tuple(cfg.trusted_proxies) == ("127.0.0.1", "::1")
    assert cfg.bot_checks_per_window == 5 and type(cfg.bot_checks_per_window) is int
    assert cfg.bot_window_seconds == 600 and type(cfg.bot_window_seconds) is int
    assert cfg.digest == content_digest(PROTECTION_YAML) == PROTECTION_V1_SHA


@pytest.mark.parametrize("path", LEAVES)
def test_any_value_change_without_entry_is_detected(tmp_path: Path, path: tuple) -> None:
    data = _golden_copy()
    old = _get_leaf(data, path)
    if isinstance(old, str):
        new = "10.0.0.1" if old == "127.0.0.1" else "10.0.0.2"
    else:
        new = old + 1
    _set_leaf(data, path, new)
    tmp = _write_yaml(tmp_path, data)
    assert tmp.read_bytes() != PROTECTION_YAML.read_bytes()
    with pytest.raises(ConfigError):
        check_changelog(tmp, CHANGELOG)


@pytest.mark.parametrize(
    "mutate, field",
    [
        pytest.param(lambda d: d.update(extra=1), "extra", id="top-unknown"),
        pytest.param(lambda d: d.pop("heavy"), "heavy", id="top-missing"),
        pytest.param(lambda d: d["api"].update(token="x"), "api.token", id="nested-unknown"),
        pytest.param(lambda d: d["bot"].pop("window_seconds"), "bot.window_seconds", id="nested-missing"),
    ],
)
def test_unknown_or_missing_field_rejected(tmp_path: Path, mutate, field: str) -> None:
    data = _golden_copy()
    mutate(data)
    path = _write_yaml(tmp_path, data)
    with pytest.raises(ConfigError, match=re.escape(field)):
        load_protection_config(path)


@pytest.mark.parametrize(
    "path_, value, field",
    [
        pytest.param(("version",), 0, "version", id="version-0"),
        pytest.param(("version",), True, "version", id="version-bool"),
        pytest.param(("api", "requests_per_window"), 0, "api.requests_per_window", id="rpw-0"),
        pytest.param(("api", "requests_per_window"), True, "api.requests_per_window", id="rpw-bool"),
        pytest.param(("api", "window_seconds"), -1, "api.window_seconds", id="window-negative"),
        pytest.param(("heavy", "max_concurrent"), "3", "heavy.max_concurrent", id="max-concurrent-str"),
        pytest.param(("heavy", "busy_retry_after_seconds"), 1.5, "heavy.busy_retry_after_seconds",
                     id="busy-float"),
        pytest.param(("bot", "checks_per_window"), float("nan"), "bot.checks_per_window", id="bot-nan"),
        pytest.param(("bot", "window_seconds"), 0, "bot.window_seconds", id="bot-window-0"),
        pytest.param(("proxy", "trusted"), ["10.0.0.300"], "proxy.trusted[0]", id="proxy-bad-ip"),
        pytest.param(("proxy", "trusted"), "127.0.0.1", "proxy.trusted", id="proxy-not-list"),
        pytest.param(("proxy", "trusted"), [None], "proxy.trusted[0]", id="proxy-not-str"),
    ],
)
def test_bad_values_rejected(tmp_path: Path, path_: tuple, value, field: str) -> None:
    data = _golden_copy()
    _set_leaf(data, path_, value)
    path = _write_yaml(tmp_path, data)
    with pytest.raises(ConfigError) as info:
        load_protection_config(path)
    message = str(info.value)
    assert field in message
    # повідомлення не відлунює значення (лише поле і правило)
    assert "10.0.0.300" not in message
