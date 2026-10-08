# Contract: `PooledRpcSource` — пул провайдерів за протоколом `RpcSource`

Файл: `src/unmask/ingest/rpc/pool.py`.

| Імпортує | Що саме |
|---|---|
| stdlib | `logging`, `math`, `threading`, `collections` (`OrderedDict`), `concurrent.futures` (`ThreadPoolExecutor`), `dataclasses`, `fractions` (`Fraction`), `types` (`MappingProxyType`), `typing` |
| `unmask.ingest.budget` | `Clock` |
| `unmask.ingest.rpc.protocol` | `AccountInfo`, `Deadline`, `RawTransaction`, `SignatureInfo`, `TokenAccountInfo`, `RpcError` і підкласи (`RpcBudgetTimeout`, `RpcRateLimited`, `RpcTimeout`, `RpcUnavailable`) |

НЕ імпортує `httpx`, `rpc.http`, `rpc.fixture`, `graph`, `hubs`, `clusters`, `delivery`, `yaml`, `os`
(AST-тест T-142).

## Інтерфейс

```python
@dataclass(frozen=True)
class Provider:
    name: str
    source: Any                        # RpcSource за протоколом (типізовано Any: пул не імпортує адаптер)
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
    def close(self) -> None: ...        # ідемпотентно; див. «Закриття»
    def __enter__/__exit__              # __exit__ викликає close() і не ковтає виняток блоку (повертає None)
    def __repr__(self) -> str: ...      # "PooledRpcSource(providers=('rpcfast', 'helius_1'))"
```

Конструктор (`ValueError` з назвою поля, без значень): ≥ 1 провайдер; унікальні `name`; `weight` > 0
скінченне; `archival` ⇔ `retention_seconds is None`; `retention_seconds` > 0; `skip_methods` ⊆ чотирьох
імен JSON-RPC методів; кожен із чотирьох методів обслуговує ≥ 1 провайдер.

`name`: якщо `source.name` усіх провайдерів однакове — воно; інакше `"pool"`.

## `get_transactions(signatures, *, deadline)` — раунди

```
якщо signatures порожній -> [] (нуль звернень, дедлайн не перевіряється)
pending = усі позиції; confirm = ∅; excluded = ∅; last_error = {}
said_none = {}            # позиція -> неархівний, що відповів None (ще не передана архівному)
excluded_errors = {}      # провайдер -> помилка, що його виключила (порядок виключення)
поки pending:
  rounds += 1; якщо rounds > 2n + 2: raise RuntimeError("pool: failover round limit exceeded")   # запобіжник, дефект
  якщо deadline.expired(): raise RpcBudgetTimeout()          # свіжий об'єкт, нуль звернень у цьому раунді
  для кожної позиції i ∈ pending: E(i) = придатні провайдери (data-model «Придатність»),
      без excluded; якщо i ∈ confirm — лише архівні; фільтр паузи (якщо лишається хоч один не на паузі)
  якщо ∃ i з E(i) = ∅, для найменшої такої i (до будь-якого звернення цього раунду):
      last_error[i] є                           -> raise last_error[i]
      інакше (позиція сама не падала, напр. чекає підтвердження None):
        S = придатні для i без урахування excluded і паузи
        ∃ p ∈ S в excluded_errors              -> raise помилку НАЙПІЗНІШЕ виключеного такого p
        інакше                                  -> raise RpcUnavailable("history beyond provider retention")
  для кожної i ∈ pending з i ∈ said_none: escalations[said_none.pop(i)] += 1
      # лічильник — у момент, коли позицію справді передано архівному (початок раунду), а не коли отримано None
  групи = позиції з однаковим E (у порядку першої позиції); кожну групу apportion() за вагами членів
      E у порядку конфігу; частка провайдера = зростаючі позиції з усіх груп
  задачі (порядок конфігу): lambda: with lock[p]: p.source.get_transactions([sig...], deadline=deadline)
  outcomes = runner(задачі)
  len(outcomes) != len(задачі) -> raise RuntimeError("runner: must return one outcome per task")   # дефект runner
  requests[p] += 1, units[p] += len(частки) для кожної частки
  обробка outcomes у порядку конфігу (головний потік, під _state_lock для лічильників/пауз):
    результат без винятку, але не list або len != len(частки)
                -> як RpcUnavailable("unexpected response shape: pool share length")
    не-RpcError -> запамʼятати перший такий; після циклу raise (дефект, не маскується, не перебирається)
    RpcError (зокрема RpcBudgetTimeout частки)
                -> excluded += p; excluded_errors[p] = exc; last_error[i] = exc для позицій частки;
                   лічильник класу; RpcRateLimited -> пауза p до now + max(cooldown, retry_after or 0)
    інакше для кожної позиції: tx не None -> остаточно; None і p.archival -> остаточно None;
                   None і не архівний -> confirm += i, said_none[i] = p, лишається в pending
  pending = (позиції часток, що впали) ∪ (позиції з None неархівного), зростаюче
return results у порядку позицій
```

`RpcBudgetTimeout` частки — стан кошика КОНКРЕТНОГО ключа (пейсер адаптера не вмістив запит у дедлайн), а не
виклику: інший ключ може мати повний кошик. Тому він перебирається як інші `RpcError`, без паузи. Вичерпаний
дедлайн самого виклику ловиться перевіркою `deadline.expired()` перед раундом — тоді свіжий `RpcBudgetTimeout()`
без звернень.

Запобіжник `2n + 2` (n — кількість провайдерів) недосяжний у коректному коді: кожен раунд із відмовою виключає
≥ 1 провайдера, раунд без відмов або завершує все, або лишає лише позиції підтвердження (найгірше — n + 2
раунди). Його `RuntimeError` — дефект, а не `RpcError`.

Гарантії:
- **Порядок**: елемент i відповідає `signatures[i]` незалежно від того, хто і коли його обслужив.
- **Детермінізм** (FR-007-04): склад часток і обробка залежать лише від вхідних даних, конфігу, стану
  пауз і результатів (не від порядку завершення задач). Результат виклику залежить лише від даних:
  транзакція з даними від будь-кого, `None` лише від архівного.
- **«Все або нічого»**: виклик або повертає повний список, або піднімає виняток; часткового результату
  немає (як в адаптера).
- **Винятки**: піднімається сам об'єкт винятку адаптера (клас і фіксований текст збережені), поза блоком
  `except` — `__cause__`/`__context__` порожні. Пул не додає тексту ззовні; власні тексти `RpcError` — константи:
  `history beyond provider retention`, `unexpected response shape: pool share length`,
  `no provider available`.
- **Внутрішні дефекти** (`RuntimeError`, не `RpcError`, не перебираються й не рахуються як відмови провайдера):
  `pool: failover round limit exceeded` (запобіжник раундів `get_transactions`, 2n + 2),
  `pool: failover attempt limit exceeded` (запобіжник спроб одиночного методу, n + 1),
  `runner: must return one outcome per task` (runner повернув не стільки outcomes, скільки задач).
- **Потоки**: кожен провайдер викликається лише під своїм локом; `_state_lock` ніколи не береться разом із
  локом провайдера. Пул безпечний для одночасних викликів з кількох потоків.

## Одиночні методи

`get_account_info`, `get_token_accounts_by_owner`, `get_signatures_for_address` — один провайдер на
спробу, у викликаючому потоці, під локом провайдера:

```
excluded = ∅; last = None
не більше n + 1 спроб:
  якщо deadline.expired(): raise RpcBudgetTimeout()      # свіжий об'єкт, нуль звернень; ротацію не зсуваємо
  cands = провайдери ∉ excluded, метод ∉ skip_methods
  для getSignaturesForAddress: якщо в cands є архівні -> лише вони
  фільтр паузи; якщо cands порожні -> raise last або RpcUnavailable("no provider available")
  p = SWRR(cands)           # згладжений зважений round-robin, нічия — порядок конфігу
  requests[p] += 1; units[p] += 1                        # до виклику: невдале звернення теж рахується
  виклик під lock[p]; RpcError (зокрема RpcBudgetTimeout ключа) -> excluded += p, last = exc, лічильник класу,
      пауза лише при RpcRateLimited; далі цикл (raise поза except); не-RpcError — летить одразу (дефект)
  повернути (p, результат)
вичерпано n + 1 спроб -> raise RuntimeError("pool: failover attempt limit exceeded")   # недосяжно, дефект

get_signatures_for_address після успішного _single:
  p не архівний і (page не list/tuple або len(page) < limit):
      # коротку/порожню сторінку неархівного ВІДКИНУТО: вік із неї НЕ кешується
      escalations[p] += 1; raise RpcUnavailable("history beyond provider retention")
  blockTime кожного запису (str підпис, справжній int) -> кеш віку    # лише ПІСЛЯ перевірки короткої сторінки
  повернути page            # сторінка архівного — остаточна, яка б не була (розбір записів — справа ядра)
```

«Повна» сторінка неархівного — `len(page) >= limit`. Відмова `RpcUnavailable` від запасного неархівного не
перебирається далі всередині цього виклику (`_single` уже повернув успіх) — це чесна неповнота для ядра.

## Лічильники (FR-007-13)

`ProviderStats(requests, units, failures, not_found_escalations, cooldowns)` (data-model). Класи
`failures`: `budget` (`RpcBudgetTimeout`, перевіряється першим), `timeout`, `rate_limited`, `unavailable`,
`other` (інший `RpcError`). `stats_line()`:
`rpc_pool rpcfast requests=3 units=40 failures=0 escalations=1 cooldowns=0; helius_1 requests=…`
— лише імена з конфігу й цілі числа. `close()` пише цей рядок у логер `unmask.ingest.rpc.pool` (INFO).

## Закриття

`close()` ідемпотентний (повторний виклик — нічого). Закриває `runner.close()` (якщо є) і `source.close()`
кожного провайдера (якщо є); одне джерело під кількома іменами (той самий об'єкт) закривається один раз.
Виняток (`Exception`) одного закриття не перериває решту: усі закриваються, потім рядок `stats_line()` пишеться
в лог, і лише після цього піднімається ПЕРШИЙ виняток — той самий об'єкт, поза `except`. `__exit__` викликає
`close()` і не ковтає виняток блоку `with`.

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
