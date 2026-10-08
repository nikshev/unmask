# Contract: `config/rpc_pool.yaml`, завантажувач і фабрика джерела

Файл коду: `src/unmask/ingest/rpc/pool_config.py`. Імпортує `yaml`, `logging`, `pathlib`,
`unmask.ingest.config` (`ConfigError`, `IngestConfig`), `unmask.ingest.rpc.http` (`HttpRpcSource`,
`RPC_PROFILES`, `DEFAULT_PROFILE`, `ENV_URL`), `unmask.ingest.rpc.pool`, `unmask.ingest.budget`. НЕ
імпортує `hubs` (журнал перевіряє тест).

## Файл v1 (поставляється)

```yaml
# Версіонований пул RPC-провайдерів (принцип III). Схема: specs/007-rpc-provider-pool/contracts/rpc-pool-config.md
# Лише ІМЕНА змінних середовища — жодних URL і ключів.
# Будь-яка зміна: підняти version і додати запис із sha256 у config/CHANGELOG.md (розділ "# config/rpc_pool.yaml").
version: 1
rate_limit_cooldown_seconds: 5
age_cache_entries: 200000
providers:
  - name: rpcfast
    url_env: UNMASK_RPC_URL
    profile: rpcfast_start
    archival: false
    retention_seconds: 72000          # виміряно ≈ 77400 (≈ 21,5 год); запас ≈ 7%
    skip_methods: [getTokenAccountsByOwner]   # заблоковано на тарифі Start
  - name: helius_1
    url_env: UNMASK_RPC_URL_ALT
    profile: helius_free
    archival: true
    retention_seconds: null
    skip_methods: []
  - name: helius_2                    # те саме, url_env: UNMASK_RPC_URL_ALT1
  - name: helius_3                    # те саме, url_env: UNMASK_RPC_URL_ALT2
```

## Схема (усі ключі обов'язкові; невідомий ключ — `ConfigError("<шлях>: unknown field")`)

| Ключ | Тип і межі |
|---|---|
| `version` | int ≥ 1 (не bool) |
| `rate_limit_cooldown_seconds` | число > 0, скінченне (не bool) |
| `age_cache_entries` | int ≥ 1 |
| `providers` | непорожній список мап |
| `providers[i].name` | `[a-z0-9_]{1,32}`, унікальне |
| `providers[i].url_env` | `[A-Za-z_][A-Za-z0-9_]*`, унікальне; рядок із `://` — `ConfigError("providers[i].url_env: must be an environment variable name")` без значення |
| `providers[i].profile` | ключ `RPC_PROFILES` |
| `providers[i].archival` | bool |
| `providers[i].retention_seconds` | `null` ⇔ `archival: true`; інакше int ≥ 1 |
| `providers[i].skip_methods` | список без дублів ⊆ {`getAccountInfo`, `getSignaturesForAddress`, `getTransaction`, `getTokenAccountsByOwner`} |

Плюс: кожен із чотирьох методів обслуговує ≥ 1 провайдер файла, інакше
`ConfigError("providers: no provider serves <method>")`. Тексти помилок — шлях поля і правило, ніколи значення.

Обмеження розбору (усе — `ConfigError`, піднята поза `except`, без уривка файла):

- файл ≤ 64 КіБ (`<path>: config is larger than 65536 bytes`); не UTF-8 → `<path>: config is not valid UTF-8`;
  збій читання → `<path>: cannot read config (<клас OSError>)`;
- якорі й аліаси відхиляються (`<path>: anchors and aliases are not allowed`); дубль ключа мапи, як і будь-яка
  інша помилка розбору, → `<path>: invalid YAML (<клас винятку>)`;
- невідомий ключ показується іменем лише якщо воно ідентифікатор `[A-Za-z_][A-Za-z0-9_]{0,63}`; інакше —
  лише тип: `<str key>` (для не-рядкового ключа — `<int key>` тощо). Тож помилково вставлений
  `https://…?api-key=…` як ключ у текст не потрапляє.

```python
def load_rpc_pool_config(path: Path) -> RpcPoolConfig: ...   # журнал не читає
```

## Журнал

Розділ `# config/rpc_pool.yaml` у `config/CHANGELOG.md` — **перед** `# config/ingest.yaml` (той лишається
останнім); запис `## 1 — 2026-10-0X`, останній рядок — `sha256: <content_digest>`. Перевірка —
`unmask.hubs.config.check_changelog(config/rpc_pool.yaml, config/CHANGELOG.md)` у тесті.

## Фабрика

```python
def build_rpc_source(config_path: Path, ingest_cfg: IngestConfig, environ: Mapping[str, str], *,
                     fallback_profile: str = DEFAULT_PROFILE,
                     clock: Clock | None = None, sleep: Callable[[float], None] | None = None,
                     transport_factory: Callable[[str], httpx.BaseTransport] | None = None,
                     runner: Runner | None = None) -> PooledRpcSource: ...
```

1. `config_path` не існує → один провайдер `default`: `url_env = UNMASK_RPC_URL`, `profile =
   fallback_profile`, `archival: true`, без `skip_methods`; змінна відсутня/невалідна → той самий
   `ConfigError`, що й `HttpRpcSource.from_env` (`UNMASK_RPC_URL: not set` / `…: invalid RPC URL`).
   Пауза й кеш віку для цього режиму — сталі модуля (`5.0`, `1`; на результат не впливають).
2. Файл є → `load_rpc_pool_config`; для кожного провайдера: змінна відсутня або порожня → пропуск +
   `logger.warning("rpc pool: provider %s skipped: %s not set", name, url_env)` (лише імена); інакше
   `HttpRpcSource.from_env(ingest_cfg.rpc, commitment=ingest_cfg.commitment, environ=environ,
   url_var=url_env, profile=profile, clock=clock, sleep=sleep, transport=transport_factory(name) або None)`
   — невалідний URL → `ConfigError` з ім'ям змінної (гучно); транспорт від `transport_factory`, якщо
   `from_env` його не прийняв, закривається.
3. Не лишилось жодного → `ConfigError("rpc_pool.yaml: no RPC provider available (set one of: UNMASK_RPC_URL,
   UNMASK_RPC_URL_ALT, …)")`; лишились, але якийсь метод ніхто не обслуговує →
   `ConfigError("rpc_pool.yaml: no available provider serves <method>")`.
4. `weight = RPC_PROFILES[profile]["rate_per_second"]`; `PooledRpcSource(..., clock=clock or SystemClock())`.
5. Лише коли файл є: `logger.info("rpc pool v%d: %s", version, "rpcfast(rpcfast_start, non-archival),
   helius_1(…)")` (перелічено лише збудованих). Режим без файла (крок 1) цього рядка не пише.

Будь-який збій кроків 2–4 (зокрема `ConfigError` кроку 3 чи `ValueError` конструктора пулу) закриває вже
збудовані `HttpRpcSource` (а з ними їхні клієнти й транспорти) і піднімає виняток далі; відкритих клієнтів
не лишається.

Логер: `unmask.ingest.rpc.pool_config`. URL і ключі не потрапляють у винятки, логи, `repr`.

## Споживачі

- `delivery/main.py`: `source = build_rpc_source(config_dir / "rpc_pool.yaml", ingest_cfg, dict(env))`
  замість `HttpRpcSource.from_env(...)`; `ConfigError` → `serve: <текст>` у stderr, код 2 (як зараз).
- `scripts/screen_worker.py`: `build_rpc_source(ROOT / "config" / "rpc_pool.yaml", cfg, os.environ,
  fallback_profile="helius_free")`; після кожного токена — `print(src.stats_line())`.
