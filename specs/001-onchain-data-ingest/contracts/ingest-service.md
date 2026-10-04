# Contract: `IngestService` — публічний вхід фічі 001

Споживач — наступні фічі (граф, хаби, кластеризація) і згодом HTTP-API/бот. Усе, що вони знають про збір даних, — цей контракт і `ingest-result.schema.json`.

Файл: `src/unmask/ingest/service.py`.

## Конструювання

```python
IngestService(config: IngestConfig, source: RpcSource, clock: Clock = SystemClock(), cache: ResultCache | None = None)
```

Конфіг завантажується викликачем (`config.load_config("config/ingest.yaml")`) і передається явно — сервіс не читає файлів і змінних середовища сам. Один екземпляр сервісу = один кеш; кеш живе, поки живе процес.

## Метод

```python
def collect(self, mint: str) -> IngestOutcome   # IngestResult | Rejection
```

Поведінка, у порядку перевірки:

1. **Некоректна адреса** (не base58, не 32 байти, порожній рядок) → `Rejection(kind="invalid_address")`. Жодного звернення до `source` (FR-001-11).
2. **Кеш повних результатів** містить `mint` → копія результату з `metadata.served_from_cache = true`, решта полів ідентична першому поверненню; жодного звернення до `source` (FR-001-12).
3. **Партиційний стан** для `mint` існує й має поточний `config.version` → `resume`: звернення лише за недоотриманим (FR-001-13). Інакше партиційний стан відкидається.
4. **Існування токена**: `get_account_info(mint)`; відсутній або не mint → `Rejection(kind="token_not_found", detail="account_missing" | "not_a_mint")` (FR-001-11). Відмова не кешується.
5. **Збір** під `Deadline(config.time_budget_seconds)`: перші N покупців → BFS фінансування до `funding_depth` → `Completeness.derive`.
6. `status == complete` → `cache.put_complete`; `incomplete` → `cache.put_partial(state)`; результат повертається в обох випадках із чесним статусом (FR-001-09, FR-001-10, FR-001-16).

Гарантії:

- `collect` **ніколи не піднімає** виняток через дані (адреса, відсутній токен, збій джерела, пошкоджені записи, вичерпаний бюджет) — усе це відображається у `Rejection` або у `completeness`. Винятки можливі лише через дефект програми (`CacheInvariantError`, `ConfigError`, а також будь-який виняток джерела поза ієрархією `RpcError` — дефект адаптера) — вони мають падати гучно й не маскуватися ні під `Rejection`, ні під `incomplete`.
- Два виклики з однаковими `config`, `source`-даними й `clock` дають результати, що відрізняються лише `metadata.analyzed_at`, `elapsed_seconds`, `rpc_calls`, `served_from_cache`, `resumed` (детермінізм, FR-001-02).
- Виклик `collect` блокує потік не довше `time_budget_seconds` + один `rpc.request_timeout_seconds` (хвіст поточного запиту).

## Серіалізація

```python
serialize.to_dict(outcome: IngestOutcome) -> dict   # валідний проти contracts/ingest-result.schema.json
serialize.to_json(outcome) -> str                   # sort_keys=True, ensure_ascii=False, без зайвих пробілів
```

Для `Rejection` — об'єкт `{"kind": …, "mint": …, "detail": …}` (гілка `rejection` у схемі).

## Що сервіс не робить (межі фічі)

Не фільтрує хаби, не будує граф, не оцінює ризик, не ходить у мережу сам (лише через `source`), не читає конфіг і секрети, не пише на диск.
