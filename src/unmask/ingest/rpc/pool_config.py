# impl: FR-007-03, FR-007-09, FR-007-10, FR-007-11, FR-007-12
"""Завантаження й валідація `config/rpc_pool.yaml` (принцип III; specs/007-rpc-provider-pool/contracts/rpc-pool-config.md).

Перелік провайдерів, профілі швидкості, архівність і глибина історії визначають, чи дістане пул історію токена,
тобто що побачить аналіз. Тому вони живуть у версіонованому YAML із журналом `config/CHANGELOG.md` і sha256-захистом
(перевірка — тест через `unmask.hubs.config.check_changelog`; цей модуль журнал НЕ читає й `hubs` не імпортує).

У файлі лише ІМЕНА змінних середовища (`url_env`), жодних URL і ключів. Тихих умовчань немає: відсутнє чи невідоме
поле та значення поза межами дають `ConfigError` зі шляхом поля й правилом. Повідомлення НІКОЛИ не відлунюють
значень (лише шлях, правило й, для невідомого ключа, його ім'я), а розбір YAML не пропускає причину в ланцюг винятків.

Розбір обмежений: файл ≤ 64 КіБ, лише прості мапи, списки й скаляри — анкери, аліаси й дублі ключів відхиляються
(інакше друге значення мовчки перебило б перше й дайджест журналу описував би не те, що завантажено).

Фабрика `build_rpc_source` (T-138, contracts/rpc-pool-config.md «Фабрика», кроки 1–5) збирає з конфігу й середовища
`PooledRpcSource`: кожен провайдер — власний `HttpRpcSource` (власний профіль і кошик лімітера, FR-007-03), а зі змінної
середовища береться лише URL. Без файла — один провайдер `UNMASK_RPC_URL`, як до цієї фічі (FR-007-10). Незадана змінна —
пропуск провайдера з попередженням (лише імена, FR-007-11); невалідний URL — гучна `ConfigError` з іменем змінної.
URL і ключі не потрапляють у логи, тексти помилок і `repr`, а помилки піднімаються поза `except` (порожні
`__cause__`/`__context__`): у середовищі живуть секрети (FR-007-12).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

import yaml

from unmask.ingest.budget import Clock, SystemClock
from unmask.ingest.config import ConfigError, IngestConfig
from unmask.ingest.rpc.http import DEFAULT_PROFILE, ENV_URL, RPC_PROFILES, HttpRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, Runner

if TYPE_CHECKING:  # лише для підказки типу: `httpx` імпортує тільки адаптер
    import httpx

__all__ = ["ProviderSpec", "RpcPoolConfig", "load_rpc_pool_config", "build_rpc_source", "RPC_METHODS"]

logger = logging.getLogger(__name__)

# Методи, які пул розподіляє між провайдерами (data-model.md).
RPC_METHODS: tuple[str, ...] = ("getAccountInfo", "getSignaturesForAddress", "getTransaction",
                                "getTokenAccountsByOwner")

_MAX_FILE_BYTES = 64 * 1024
_NAME_RE = re.compile(r"[a-z0-9_]{1,32}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")  # як hubs.config._IDENT_RE (hubs тут не імпортується)
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
    # Усі `raise` — поза блоками `except`: `from None` лише ховає контекст у показі, а сам об'єкт (`__context__`)
    # лишився б досяжним, і розбірник YAML тримає у ньому уривок файла (з помилково вставленим URL чи ключем).
    failure: str | None = None
    raw = b""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        failure = f"{path}: cannot read config ({type(exc).__name__})"
    if failure is not None:
        raise ConfigError(failure)
    if len(raw) > _MAX_FILE_BYTES:
        raise ConfigError(f"{path}: config is larger than {_MAX_FILE_BYTES} bytes")
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failure = f"{path}: config is not valid UTF-8"
    if failure is not None:
        raise ConfigError(failure)
    data: Any = None
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                failure = f"{path}: anchors and aliases are not allowed"
                break
        else:
            data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader-нащадок
    except Exception as exc:  # noqa: BLE001 — межа: недовірений текст, контракт — лише ConfigError
        failure = f"{path}: invalid YAML ({type(exc).__name__})"
    if failure is not None:
        raise ConfigError(failure)
    return data


def _key_name(key: Any) -> str:
    """Безпечне ім'я невідомого ключа для повідомлення: ідентифікатор як є, інше — лише тип (`<str key>`).

    Ключ мапи — недовірений текст: помилково вставлений `https://…?api-key=…` не має потрапити в повідомлення.
    """
    if isinstance(key, str) and _IDENT_RE.fullmatch(key):
        return key
    return f"<{type(key).__name__} key>"


def _check_keys(data: Any, expected: tuple[str, ...], where: str) -> dict:
    prefix = f"{where}." if where else ""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected a mapping")
    for key in expected:
        if key not in data:
            raise ConfigError(f"{prefix}{key}: missing required field")
    for key in data:
        if not (isinstance(key, str) and key in expected):
            raise ConfigError(f"{prefix}{_key_name(key)}: unknown field")
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


# ------------------------------------------------------------------------------------------------------ фабрика джерела

# Пауза після 429 і розмір кешу віку для режиму «без файла»: на результат не впливають (contracts «Фабрика», крок 1).
_DEFAULT_COOLDOWN_SECONDS = 5.0
_DEFAULT_AGE_CACHE_ENTRIES = 1
_DEFAULT_PROVIDER_NAME = "default"
_UNLIMITED_WEIGHT = 1e6  # data-model: `rate_per_second = inf` профілю -> скінченна вага


def _weight(profile: str) -> float:
    rate = float(RPC_PROFILES[profile]["rate_per_second"])
    return rate if math.isfinite(rate) else _UNLIMITED_WEIGHT


def _single_default_spec(fallback_profile: str) -> ProviderSpec:
    """Крок 1: без файла — один архівний провайдер із `UNMASK_RPC_URL` (довіра, як до цієї фічі)."""
    return ProviderSpec(name=_DEFAULT_PROVIDER_NAME, url_env=ENV_URL, profile=fallback_profile, archival=True,
                        retention_seconds=None, skip_methods=())


def _describe(spec: ProviderSpec) -> str:
    return f"{spec.name}({spec.profile}, {'archival' if spec.archival else 'non-archival'})"


def build_rpc_source(
    config_path: Path,
    ingest_cfg: IngestConfig,
    environ: Mapping[str, str],
    *,
    fallback_profile: str = DEFAULT_PROFILE,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
    transport_factory: Callable[[str], httpx.BaseTransport] | None = None,
    runner: Runner | None = None,
) -> PooledRpcSource:
    """Зібрати `PooledRpcSource` за `config/rpc_pool.yaml` і середовищем (кроки 1–5 контракту).

    `environ` читається лише як переданий мапінг (`os.environ` не чіпається). `transport_factory(name)` — шов для
    тестів: транспорт кожного провайдера за його іменем з конфігу. Усі помилки — `ConfigError` з іменем змінної чи
    правилом, без значень; клієнти вже збудованих провайдерів при цьому закриваються.
    """
    config_path = Path(config_path)
    if config_path.exists():
        cfg = load_rpc_pool_config(config_path)  # `ConfigError` піднімається звідси ж: нічого ще не збудовано
        specs = cfg.providers
        cooldown, cache_entries, version = cfg.rate_limit_cooldown_seconds, cfg.age_cache_entries, cfg.version
        single = False
    else:
        specs = (_single_default_spec(fallback_profile),)
        cooldown, cache_entries, version = _DEFAULT_COOLDOWN_SECONDS, _DEFAULT_AGE_CACHE_ENTRIES, 0
        single = True
    clock = clock if clock is not None else SystemClock()

    built: list[tuple[ProviderSpec, HttpRpcSource]] = []
    try:
        for spec in specs:
            if not single and not environ.get(spec.url_env):
                # лише імена: значення змінної (воно і є секретом) у повідомлення не потрапляє
                logger.warning("rpc pool: provider %s skipped: %s not set", spec.name, spec.url_env)
                continue
            # Режим «без файла»: відсутня/невалідна змінна дає ТУ САМУ `ConfigError`, що й `HttpRpcSource.from_env`.
            transport = transport_factory(spec.name) if transport_factory is not None else None
            try:
                source = HttpRpcSource.from_env(
                    ingest_cfg.rpc,
                    commitment=ingest_cfg.commitment,
                    environ=environ,
                    url_var=spec.url_env,
                    profile=spec.profile,
                    clock=clock,
                    sleep=sleep,
                    transport=transport,
                )
            except BaseException:
                if transport is not None:  # URL не прийнято: клієнта немає, транспорт не лишаємо відкритим
                    transport.close()
                raise
            built.append((spec, source))
        if not built:
            raise ConfigError(
                "rpc_pool.yaml: no RPC provider available (set one of: "
                + ", ".join(spec.url_env for spec in specs)
                + ")"
            )
        for method in RPC_METHODS:
            if all(method in spec.skip_methods for spec, _ in built):
                raise ConfigError(f"rpc_pool.yaml: no available provider serves {method}")
        providers = [
            Provider(
                name=spec.name,
                source=source,
                weight=_weight(spec.profile),
                archival=spec.archival,
                retention_seconds=spec.retention_seconds,
                skip_methods=frozenset(spec.skip_methods),
            )
            for spec, source in built
        ]
        pool = PooledRpcSource(
            providers,
            clock=clock,
            rate_limit_cooldown_seconds=cooldown,
            age_cache_entries=cache_entries,
            runner=runner,
        )
    except BaseException:
        for _, source in built:  # не лишати відкритих клієнтів, коли збірка не вдалась
            source.close()
        raise
    if not single:
        logger.info("rpc pool v%d: %s", version, ", ".join(_describe(spec) for spec, _ in built))
    return pool
