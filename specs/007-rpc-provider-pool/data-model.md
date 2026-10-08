# Data Model: Пул RPC-провайдерів (007)

Усі сутності — у пам'яті процесу; на диск пише лише оператор (`config/rpc_pool.yaml`). Результат збору
(`IngestResult`) і документ відповіді 004 не змінюються.

## Конфіг (`pool_config.py`)

```text
ProviderSpec (frozen)
  name: str                 # [a-z0-9_]{1,32}, унікальне; єдиний ідентифікатор у логах і лічильниках
  url_env: str              # ім'я змінної середовища, [A-Za-z_][A-Za-z0-9_]*; НЕ URL (немає "://")
  profile: str              # ключ RPC_PROFILES ("rpcfast_start" | "helius_free")
  archival: bool            # справжній bool
  retention_seconds: int | None   # None ⇔ archival; для неархівного — ціле ≥ 1
  skip_methods: tuple[str, ...]   # підмножина {getAccountInfo, getSignaturesForAddress,
                                  #   getTransaction, getTokenAccountsByOwner}; без дублів

RpcPoolConfig (frozen)
  version: int ≥ 1
  rate_limit_cooldown_seconds: float  # > 0, скінченне
  age_cache_entries: int              # ≥ 1
  providers: tuple[ProviderSpec, ...] # ≥ 1; унікальні name і url_env
```

Інваріанти перевіряє завантажувач (`ConfigError` з назвою поля, без значень): хоча б один провайдер не
пропускає `getSignaturesForAddress`; провайдер не може пропускати всі методи.

## Рантайм (`pool.py`)

```text
Provider (frozen; repr лише name)
  name: str
  source: Any               # RpcSource за протоколом: HttpRpcSource у роботі, дубль у тестах
  weight: float             # rate_per_second профілю; > 0, скінченне (math.inf профілю → 1e6)
  archival: bool
  retention_seconds: float | None
  skip_methods: frozenset[str]

ProviderStats (frozen знімок)
  requests: int             # звернень до провайдера (будь-який метод; частка = 1)
  units: int                # одиниць: частка get_transactions = к-сть підписів; інші = 1
  failures: Mapping[str, int]   # незмінний; завжди всі п'ять: budget | timeout | rate_limited | unavailable | other
  not_found_escalations: int    # позицій, справді переданих архівному після None (на початку раунду),
                                #   плюс відкинуті короткі сторінки підписів неархівного
  cooldowns: int

PooledRpcSource — внутрішній стан
  _providers: tuple[Provider, ...]          # порядок конфігу = порядок нічиїх; індекс = ключ усього стану нижче
  _locks: tuple[Lock, ...]                  # по одному на провайдера
  _state_lock: Lock                          # кеш віку, паузи, SWRR, лічильники; не разом із _locks
  _age: OrderedDict[str, int]                # signature -> blockTime, FIFO ≤ age_cache_entries
  _cooldown_until: dict[int, float]          # індекс провайдера -> clock.monotonic() кінця паузи
  _swrr_current: list[Fraction]              # стан згладженого round-robin за індексом провайдера (точна арифметика)
  _requests, _units, _not_found_escalations, _cooldown_count: list[int]   # лічильники за індексом
  _failures: list[dict[str, int]]            # за індексом; ключі FAILURE_CLASSES
  _closed: bool                              # close() ідемпотентний
```

## Похідні правила

- **Придатність провайдера p для підпису s** (`get_transactions`): `getTransaction ∉ p.skip_methods` і
  (`p.archival` або вік(s) невідомий, або `wall − blockTime(s) ≤ p.retention_seconds`); у раунді
  підтвердження — лише архівні.
- **Придатність для одиночних методів**: метод ∉ `skip_methods`, провайдер не виключений у цьому виклику;
  `getSignaturesForAddress` — спершу архівні, неархівні лише якщо архівних не лишилось.
- **Пауза**: провайдер на паузі (`now < cooldown_until`) непридатний, якщо є придатні не на паузі.
- **Остаточність**: `RawTransaction` від будь-кого — остаточна; `None` — остаточне лише від архівного;
  сторінка підписів — остаточна від архівного або повна (`len >= limit`) від неархівного; коротку/не-список
  від неархівного відкинуто (вік із неї не кешується).
- **Ім'я пулу**: спільне `source.name` усіх провайдерів, інакше `"pool"`.
