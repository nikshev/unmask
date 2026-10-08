# Contract: `PooledRpcSource` — пул провайдерів за протоколом `RpcSource`

Файл: `src/unmask/ingest/rpc/pool.py`. Імпортує лише stdlib, `unmask.ingest.rpc.protocol`,
`unmask.ingest.budget` (`Clock`). НЕ імпортує `httpx`, `rpc.http`, `rpc.fixture`, `graph`, `hubs`,
`clusters`, `delivery`, `yaml`, `os` (AST-тест T-142).

## Інтерфейс

```python
@dataclass(frozen=True)
class Provider:
    name: str
    source: RpcSource
    weight: float                      # > 0, скінченне
    archival: bool
    retention_seconds: float | None    # None ⇔ archival
    skip_methods: frozenset[str] = frozenset()
    # __repr__ -> f"Provider(name={name!r})"  (source у repr не потрапляє)

Outcome = tuple[Any, BaseException | None]           # (результат, виняток) однієї задачі
Runner = Callable[[Sequence[Callable[[], Any]]], list[Outcome]]   # outcomes у ПОРЯДКУ ЗАДАЧ

def apportion(n: int, weights: Sequence[float]) -> list[int]: ...
    # метод найбільших залишків; sum == n; нічия залишків — менший індекс; n == 0 -> нулі

class ThreadRunner:                      # runner за замовчуванням
    def __init__(self, max_workers: int) -> None: ...
    def __call__(self, tasks) -> list[Outcome]: ...   # ловить BaseException задачі в Outcome
    def close(self) -> None: ...

class PooledRpcSource:                   # реалізує RpcSource
    def __init__(self, providers: Sequence[Provider], *, clock: Clock,
                 rate_limit_cooldown_seconds: float, age_cache_entries: int,
                 runner: Runner | None = None) -> None: ...
    # RpcSource: get_account_info, get_signatures_for_address, get_transactions,
    #            get_token_accounts_by_owner, name
    def stats(self) -> Mapping[str, ProviderStats]: ...   # знімок, ключі в порядку конфігу
    def stats_line(self) -> str: ...
    def close(self) -> None: ...        # ідемпотентно: runner.close, source.close (якщо є), лог stats_line
    def __enter__/__exit__
    def __repr__(self) -> str: ...      # "PooledRpcSource(providers=('rpcfast', 'helius_1'))"
```

Конструктор (`ValueError` з назвою поля, без значень): ≥ 1 провайдер; унікальні `name`; `weight` > 0
скінченне; `archival` ⇔ `retention_seconds is None`; `retention_seconds` > 0; `skip_methods` ⊆ чотирьох
імен JSON-RPC методів; кожен із чотирьох методів обслуговує ≥ 1 провайдер.

`name`: якщо `source.name` усіх провайдерів однакове — воно; інакше `"pool"`.

## `get_transactions(signatures, *, deadline)` — раунди

```
якщо signatures порожній -> [] (нуль звернень)
pending = усі позиції; confirm = ∅; excluded = ∅; last_error = {}
поки pending:
  якщо deadline.expired(): raise RpcBudgetTimeout()          # нуль звернень у цьому раунді
  для кожної позиції i ∈ pending: E(i) = придатні провайдери (data-model «Придатність»),
      без excluded; якщо i ∈ confirm — лише архівні; фільтр паузи (якщо лишається хоч один не на паузі)
  якщо ∃ i з E(i) = ∅: для найменшої такої i -> raise last_error[i] якщо є,
      інакше RpcUnavailable("history beyond provider retention")          # до будь-якого звернення
  групи = позиції з однаковим E (у порядку першої позиції); кожну групу apportion() за вагами членів
      E у порядку конфігу; частка провайдера = зростаючі позиції з усіх груп
  задачі (порядок конфігу): lambda: with lock[p]: p.source.get_transactions([sig...], deadline=deadline)
  outcomes = runner(задачі)
  обробка outcomes у порядку конфігу (головний потік, під _state_lock для лічильників/пауз):
    не-RpcError -> запамʼятати перший такий; після циклу raise (дефект, не маскується)
    RpcError    -> excluded += p; last_error[i] = exc для позицій частки; лічильник класу;
                   RpcRateLimited -> пауза p до now + max(cooldown, retry_after or 0)
    len(result) != len(частки) -> як RpcUnavailable("unexpected response shape: pool share length")
    інакше для кожної позиції: tx не None -> остаточно; None і p.archival -> остаточно None;
                   None і не архівний -> confirm += i, лишається в pending, escalations += 1
return results у порядку позицій
```

Гарантії:
- **Порядок**: елемент i відповідає `signatures[i]` незалежно від того, хто і коли його обслужив.
- **Детермінізм** (FR-007-04): склад часток і обробка залежать лише від вхідних даних, конфігу, стану
  пауз і результатів (не від порядку завершення задач). Результат виклику залежить лише від даних:
  транзакція з даними від будь-кого, `None` лише від архівного.
- **«Все або нічого»**: виклик або повертає повний список, або піднімає виняток; часткового результату
  немає (як в адаптера).
- **Винятки**: піднімається сам об'єкт винятку адаптера (клас і фіксований текст збережені), поза блоком
  `except` — `__cause__`/`__context__` порожні. Пул не додає тексту ззовні; власні тексти — константи:
  `history beyond provider retention`, `unexpected response shape: pool share length`,
  `no provider available`.
- **Потоки**: кожен провайдер викликається лише під своїм локом; `_state_lock` ніколи не береться разом із
  локом провайдера. Пул безпечний для одночасних викликів з кількох потоків.

## Одиночні методи

`get_account_info`, `get_token_accounts_by_owner`, `get_signatures_for_address` — один провайдер на
спробу, у викликаючому потоці, під локом провайдера:

```
excluded = ∅; last = None
цикл:
  якщо deadline.expired(): raise RpcBudgetTimeout()
  cands = провайдери ∉ excluded, метод ∉ skip_methods
  для getSignaturesForAddress: якщо в cands є архівні -> лише вони
  фільтр паузи; якщо cands порожні -> raise last або RpcUnavailable("no provider available")
  p = SWRR(cands)           # згладжений зважений round-robin, нічия — порядок конфігу
  виклик; RpcError -> excluded += p, last = exc, лічильник, пауза при 429; далі цикл (raise поза except)
  getSignaturesForAddress: blockTime кожного запису (справжній int) -> кеш віку;
      p не архівний і len(page) < limit -> escalations += 1; raise RpcUnavailable("history beyond provider retention")
  повернути результат
```

## Лічильники (FR-007-13)

`ProviderStats(requests, units, failures, not_found_escalations, cooldowns)` (data-model). Класи
`failures`: `budget` (`RpcBudgetTimeout`, перевіряється першим), `timeout`, `rate_limited`, `unavailable`,
`other` (інший `RpcError`). `stats_line()`:
`rpc_pool rpcfast requests=3 units=40 failures=0 escalations=1 cooldowns=0; helius_1 requests=…`
— лише імена з конфігу й цілі числа. `close()` пише цей рядок у логер `unmask.ingest.rpc.pool` (INFO).

## Тестові дублі (`tests/rpc_pool_doubles.py`, `# impl: FR-007-15`)

```python
class ScriptedProvider:                      # реалізує RpcSource поверх ВЛАСНОГО FixtureRpcSource
    def __init__(self, inner: RpcSource, *, horizon_block_time: int | None = None,
                 failures: Sequence[ScriptedFailure] = (), barrier: threading.Barrier | None = None): ...
    log: list[tuple[str, tuple[str, ...]]]   # (метод, ключі); getTransaction -> кортеж підписів частки
    name -> inner.name
@dataclass class ScriptedFailure: method: str; calls: frozenset[int] | None; exc: Exception
    # calls — номери викликів цього методу на цьому провайдері (з 1); None — кожен виклик
class SequentialRunner:                      # Runner без потоків
    def __init__(self, order: str | Sequence[int] = "forward"): ...   # "forward" | "reverse" | перестановка
```

`horizon_block_time` емулює неархівний вузол: транзакція з `blockTime < horizon` → `None`; записи
сторінки підписів з `blockTime < horizon` відкидаються (сторінка коротшає). `barrier` — `wait()` перед
обслуговуванням `get_transactions`. Один `FixtureRpcSource` не ділиться між дублями (не потокобезпечний).
