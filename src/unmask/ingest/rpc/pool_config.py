# impl: FR-007-09
"""Завантаження й валідація `config/rpc_pool.yaml` (принцип III; specs/007-rpc-provider-pool/contracts/rpc-pool-config.md).

Перелік провайдерів, профілі швидкості, архівність і глибина історії визначають, чи дістане пул історію токена,
тобто що побачить аналіз. Тому вони живуть у версіонованому YAML із журналом `config/CHANGELOG.md` і sha256-захистом
(перевірка — тест через `unmask.hubs.config.check_changelog`; цей модуль журнал НЕ читає й `hubs` не імпортує).

У файлі лише ІМЕНА змінних середовища (`url_env`), жодних URL і ключів. Тихих умовчань немає: відсутнє чи невідоме
поле та значення поза межами дають `ConfigError` зі шляхом поля й правилом. Повідомлення НІКОЛИ не відлунюють
значень (лише шлях, правило й, для невідомого ключа, його ім'я), а розбір YAML не пропускає причину в ланцюг винятків.

Розбір обмежений: файл ≤ 64 КіБ, лише прості мапи, списки й скаляри — анкери, аліаси й дублі ключів відхиляються
(інакше друге значення мовчки перебило б перше й дайджест журналу описував би не те, що завантажено).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from unmask.ingest.config import ConfigError
from unmask.ingest.rpc.http import RPC_PROFILES

__all__ = ["ProviderSpec", "RpcPoolConfig", "load_rpc_pool_config", "RPC_METHODS"]

# Методи, які пул розподіляє між провайдерами (data-model.md).
RPC_METHODS: tuple[str, ...] = ("getAccountInfo", "getSignaturesForAddress", "getTransaction",
                                "getTokenAccountsByOwner")

_MAX_FILE_BYTES = 64 * 1024
_NAME_RE = re.compile(r"[a-z0-9_]{1,32}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_URL_ENV_ERROR = "must be an environment variable name"
_PROFILE_ERROR = "unknown provider profile (expected one of: " + ", ".join(RPC_PROFILES) + ")"
_TOP_KEYS = ("version", "rate_limit_cooldown_seconds", "age_cache_entries", "providers")
_PROVIDER_KEYS = ("name", "url_env", "profile", "archival", "retention_seconds", "skip_methods")


@dataclass(frozen=True)
class ProviderSpec:
    """Один провайдер пулу (лише імена змінних середовища, не URL)."""

    name: str
    url_env: str
    profile: str
    archival: bool
    retention_seconds: int | None
    skip_methods: tuple[str, ...]


@dataclass(frozen=True)
class RpcPoolConfig:
    """Перевірений `config/rpc_pool.yaml`."""

    version: int
    rate_limit_cooldown_seconds: float
    age_cache_entries: int
    providers: tuple[ProviderSpec, ...]


class _StrictLoader(yaml.SafeLoader):
    """`SafeLoader`, що відхиляє дублі ключів мап."""

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        seen: set = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.YAMLError("duplicate key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path) -> Any:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config ({type(exc).__name__})") from None
    if len(raw) > _MAX_FILE_BYTES:
        raise ConfigError(f"{path}: config is larger than {_MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: config is not valid UTF-8") from None
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                raise ConfigError(f"{path}: anchors and aliases are not allowed")
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader-нащадок
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — межа: недовірений текст, контракт — лише ConfigError
        raise ConfigError(f"{path}: invalid YAML ({type(exc).__name__})") from None


def _check_keys(data: Any, expected: tuple[str, ...], where: str) -> dict:
    prefix = f"{where}." if where else ""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected a mapping")
    for key in expected:
        if key not in data:
            raise ConfigError(f"{prefix}{key}: missing required field")
    for key in data:
        if not (isinstance(key, str) and key in expected):
            label = key if isinstance(key, str) and len(key) <= 64 else f"<{type(key).__name__} key>"
            raise ConfigError(f"{prefix}{label}: unknown field")
    return data


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(value: Any, where: str, lo: int) -> int:
    if not _is_int(value):
        raise ConfigError(f"{where}: expected int")
    if value < lo:
        raise ConfigError(f"{where}: must be >= {lo}")
    return value


def _parse_provider(raw: Any, index: int, names: set[str], envs: set[str]) -> ProviderSpec:
    where = f"providers[{index}]"
    data = _check_keys(raw, _PROVIDER_KEYS, where)

    name = data["name"]
    if not isinstance(name, str):
        raise ConfigError(f"{where}.name: expected a string")
    if _NAME_RE.fullmatch(name) is None:
        raise ConfigError(f"{where}.name: must match [a-z0-9_]{{1,32}}")
    if name in names:
        raise ConfigError(f"{where}.name: duplicate")

    url_env = data["url_env"]
    if not isinstance(url_env, str) or _ENV_NAME_RE.fullmatch(url_env) is None:
        # рядок із "://" (URL чи ключ) — теж сюди: значення в повідомлення не потрапляє
        raise ConfigError(f"{where}.url_env: {_URL_ENV_ERROR}")
    if url_env in envs:
        raise ConfigError(f"{where}.url_env: duplicate")

    profile = data["profile"]
    if not isinstance(profile, str) or profile not in RPC_PROFILES:
        raise ConfigError(f"{where}.profile: {_PROFILE_ERROR}")

    archival = data["archival"]
    if not isinstance(archival, bool):
        raise ConfigError(f"{where}.archival: expected bool")

    retention = data["retention_seconds"]
    if archival:
        if retention is not None:
            raise ConfigError(f"{where}.retention_seconds: must be null when archival is true")
    else:
        if retention is None:
            raise ConfigError(f"{where}.retention_seconds: must be an int >= 1 when archival is false")
        retention = _int(retention, f"{where}.retention_seconds", 1)

    skip = data["skip_methods"]
    if not isinstance(skip, list):
        raise ConfigError(f"{where}.skip_methods: expected a list")
    for method in skip:
        if not isinstance(method, str) or method not in RPC_METHODS:
            raise ConfigError(f"{where}.skip_methods: items must be among " + ", ".join(RPC_METHODS))
    if len(set(skip)) != len(skip):
        raise ConfigError(f"{where}.skip_methods: duplicate method")
    if set(skip) >= set(RPC_METHODS):
        raise ConfigError(f"{where}.skip_methods: provider must serve at least one method")

    names.add(name)
    envs.add(url_env)
    return ProviderSpec(name=name, url_env=url_env, profile=profile, archival=archival,
                        retention_seconds=retention, skip_methods=tuple(skip))


def load_rpc_pool_config(path: Path) -> RpcPoolConfig:
    """Прочитати `config/rpc_pool.yaml`; некоректне → `ConfigError`. Журнал не читається."""
    path = Path(path)
    data = _check_keys(_read_yaml(path), _TOP_KEYS, "")

    version = _int(data["version"], "version", 1)

    cooldown = data["rate_limit_cooldown_seconds"]
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
        raise ConfigError("rate_limit_cooldown_seconds: expected number")
    cooldown = float(cooldown)
    if not math.isfinite(cooldown) or cooldown <= 0:
        raise ConfigError("rate_limit_cooldown_seconds: must be a finite number > 0")

    age_cache_entries = _int(data["age_cache_entries"], "age_cache_entries", 1)

    raw_providers = data["providers"]
    if not isinstance(raw_providers, list):
        raise ConfigError("providers: expected a list")
    if not raw_providers:
        raise ConfigError("providers: must not be empty")
    names: set[str] = set()
    envs: set[str] = set()
    providers = tuple(_parse_provider(raw, i, names, envs) for i, raw in enumerate(raw_providers))

    for method in RPC_METHODS:
        if all(method in p.skip_methods for p in providers):
            raise ConfigError(f"providers: no provider serves {method}")

    return RpcPoolConfig(version=version, rate_limit_cooldown_seconds=cooldown,
                         age_cache_entries=age_cache_entries, providers=providers)
