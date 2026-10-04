# Contract: `RpcSource` — джерело ончейн-даних (FR-001-15)

Єдина точка доступу до зовнішнього світу. Ядро збору (`buyers.py`, `funding.py`, `collector.py`, `service.py`) викликає **лише** цей інтерфейс; імпорт `httpx` або будь-якого мережевого модуля поза `rpc/http.py` — дефект ревʼю (принцип IV).

Файл: `src/unmask/ingest/rpc/protocol.py`. Реалізації: `rpc/fixture.py::FixtureRpcSource`, `rpc/http.py::HttpRpcSource`.

## Інтерфейс

```python
class RpcSource(Protocol):
    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None: ...
    def get_signatures_for_address(
        self, address: str, *, before: str | None, until: str | None,
        limit: int, deadline: Deadline,
    ) -> list[SignatureInfo]: ...
    def get_transactions(self, signatures: Sequence[str], *, deadline: Deadline) -> list[RawTransaction | None]: ...
    def get_token_accounts_by_owner(self, owner: str, *, deadline: Deadline) -> list[TokenAccountInfo]: ...
    @property
    def name(self) -> str: ...          # "fixture:<scenario>" | "http"
```

Типи-значення — `TypedDict`/`Mapping`, що **дослівно** повторюють поле `result` відповідних методів Solana JSON-RPC. Адаптери не перекладають структуру — вони лише транспортують і мапують помилки. Так фікстури, записані з живого RPC, підходять без перетворень.

### `get_account_info(address)` ↔ `getAccountInfo(address, {encoding: "jsonParsed", commitment})`

Повертає `result.value` або `None`, якщо рахунку немає. Для mint очікується:

```json
{"lamports": 1461600, "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "executable": false, "rentEpoch": 0, "space": 82,
 "data": {"program": "spl-token", "space": 82,
          "parsed": {"type": "mint", "info": {"decimals": 6, "supply": "1000000000000000", "isInitialized": true,
                                               "mintAuthority": null, "freezeAuthority": null}}}}
```

`owner` ∈ {`TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`, `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`} і `data.parsed.type == "mint"` — критерій «токен існує». Інакше `token_not_found` з `detail=not_a_mint`; `None` → `detail=account_missing`.

### `get_signatures_for_address(address, before, until, limit)` ↔ `getSignaturesForAddress`

Повертає список **від найновішого до найстарішого**, довжиною ≤ `limit` (≤ 1000). Кожен елемент:

```json
{"signature": "5h…", "slot": 312000451, "err": null, "memo": null, "blockTime": 1759400000, "confirmationStatus": "finalized"}
```

Семантика курсорів (обов'язкова для обох реалізацій):

- `before=S` — лише записи, що в історії адреси **строго раніші** за S (S має належати історії цієї адреси; ядро гарантує це: S — підпис ребра, яке посилається на адресу).
- `until=S` — лише записи, **новіші** за S.
- Порожній список означає кінець історії.

`FixtureRpcSource` реалізує це поверх повного збереженого списку. Записи з `err != null` ядро пропускає.

### `get_transactions(signatures)` ↔ `getTransaction(sig, {encoding: "jsonParsed", maxSupportedTransactionVersion: <max_tx_version>, commitment})`

Пакетний виклик: елемент i відповідає `signatures[i]`; `None` — транзакцію не знайдено (обрізана історія). Форма елемента:

`max_tx_version` — параметр адаптера (за замовчуванням `1`): мережа містить транзакції `version: 1`; на запит із меншим значенням вузол відповідає JSON-RPC `-32015`. Поле `version` елемента повертається як є (`"legacy"`, `0`, `1`, …).

```json
{"slot": 312000451, "blockTime": 1759400000, "version": 0,
 "transaction": {"signatures": ["5h…"],
   "message": {"recentBlockhash": "…",
     "accountKeys": [{"pubkey": "…", "signer": true, "writable": true, "source": "transaction"}],
     "instructions": [
       {"program": "system", "programId": "11111111111111111111111111111111",
        "parsed": {"type": "transfer", "info": {"source": "…", "destination": "…", "lamports": 1500000000}}},
       {"programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "accounts": ["…"], "data": "…"}
     ]}},
 "meta": {"err": null, "fee": 5000,
   "preBalances": [0, 0], "postBalances": [0, 0],
   "preTokenBalances":  [{"accountIndex": 3, "mint": "…", "owner": "…", "programId": "Tokenkeg…",
                          "uiTokenAmount": {"amount": "0", "decimals": 6, "uiAmount": null, "uiAmountString": "0"}}],
   "postTokenBalances": [],
   "innerInstructions": [{"index": 1, "instructions": [
       {"program": "spl-token", "programId": "Tokenkeg…",
        "parsed": {"type": "transferChecked", "info": {"source": "…", "destination": "…", "authority": "…", "mint": "…",
                   "tokenAmount": {"amount": "250000", "decimals": 6, "uiAmount": 0.25, "uiAmountString": "0.25"}}}}]}],
   "logMessages": [], "loadedAddresses": {"readonly": [], "writable": []}}}
```

Інструкції, які адаптер не може розібрати в `parsed`, лишаються сирими (`programId`, `accounts`, `data`); ядро їх ігнорує як перекази, але враховує `programId` у `programs[]` покупця.

### `get_token_accounts_by_owner(owner)` ↔ `getTokenAccountsByOwner(owner, {programId: Token}, {encoding: "jsonParsed"})`

Повертає `result.value` — список `{"pubkey": "…", "account": {"data": {"parsed": {"info": {"mint": "…", "owner": "…", "tokenAmount": {…}}}}}}`. Ядро використовує лише `pubkey` і `info.mint`. Викликається для spl-token і spl-token-2022 (адаптер об'єднує два запити в один результат).

## Помилки

Адаптер піднімає **тільки** ці винятки (`rpc/protocol.py`) — разом із підкласом `RpcBudgetTimeout` (T-051); усе інше з транспорту — дефект адаптера:

| Виняток | Коли | Мапиться у `MissingReason` |
|---|---|---|
| `RpcRateLimited(retry_after: float \| None)` | HTTP 429 або JSON-RPC помилка з кодом ліміту | `rate_limited` |
| `RpcTimeout` | таймаут запиту (httpx або загальна тривалість запиту понад `request_timeout`) | `timeout`; `budget_exhausted` (ядро), лише якщо на момент обробки `deadline.expired()` |
| `RpcBudgetTimeout` (підклас `RpcTimeout`, текст фіксований — `budget`) | адаптер сам вирішив, що звернення не вміщається в бюджет: `deadline.expired()` перед запитом; `request_timeout() <= 0`/NaN; очікування глобального лімітера ≥ `deadline.remaining()` | `budget_exhausted` (ядро, `budget.deadline_timeouts`) — **безумовно**, навіть якщо `expired()` ще хибне |
| `RpcUnavailable(detail)` | HTTP 5xx, мережевий збій, JSON-RPC `error`, невалідний JSON | `unavailable` |

Захисний шар ядра (рішення власника процесу за ревʼю T-016): будь-який інший `RpcError` (базовий клас або невідомий підклас) ядро мапить у `unavailable` з класом винятку в `detail` — «не вдалось дізнатись», а не збій `collect`. Підклас відомого винятку зберігає його причину (`isinstance`). Винятки поза ієрархією `RpcError` (ValueError, KeyError тощо) — дефект адаптера й виходять з `collect` назовні; ніколи не маскуються під «чисто» (з `IngestService.collect` теж виходять назовні як дефект програми — див. `ingest-service.md`, «Гарантії»: ні `Rejection`, ні `incomplete` не можуть чесно описати дефект адаптера, тож єдина безпечна поведінка — гучне падіння).

Повторні спроби (`rpc.max_retries`, `rpc.retry_backoff_seconds`) — відповідальність адаптера, у межах `deadline`. Ядро отримує або результат, або один із трьох винятків (`RpcTimeout` — можливо, його підклас `RpcBudgetTimeout`).

## `Deadline`

`budget.py::Deadline(clock: Clock, seconds: float)` — `remaining() -> float`, `expired() -> bool`, `request_timeout(cap: float) -> float` (мінімум із залишку й `rpc.request_timeout_seconds`). Адаптер перед кожним запитом: `if deadline.expired(): raise RpcBudgetTimeout()`; `request_timeout() <= 0` (або NaN) — теж `RpcBudgetTimeout()`. Ядро обгортає кожне звернення в `budget.deadline_timeouts`: `RpcBudgetTimeout` → `BudgetExhausted` безумовно (рішення адаптера «винен бюджет», T-051: пейсер піднімає його, коли `expired()` ще хибне), звичайний `RpcTimeout` → `BudgetExhausted` лише якщо `deadline.expired()`, інакше лишається `timeout`. `detail` у `missing`/`buyers` — `<метод і адреса/підпис>: budget`.

## Вимоги до реалізацій

**`FixtureRpcSource(scenario_dir, *, failures=None, clock=None)`**
- Читає `rpc.json` сценарію: `{"getAccountInfo": {addr: value|null}, "getSignaturesForAddress": {addr: [SignatureInfo…]}, "getTransaction": {sig: RawTransaction|null}, "getTokenAccountsByOwner": {owner: [TokenAccountInfo…]}}`.
- Веде журнал `calls: list[(method, params)]` — тести доводять «0 звернень» (FR-001-12) і «лише недоотримане» (FR-001-13) саме за ним.
- `failures` — політика: `FailAfter(n_calls, exc)`, `FailFor(address|signature, exc, times)`; `clock` — `FakeClock`, який просувається на `advance_per_call` на кожному виклику.
- Адреса, відсутня в `rpc.json`, → порожня історія (не помилка): це покриває edge case «гаманець без вхідних переказів».

**`HttpRpcSource(url, config.rpc, transport=None, *, commitment, clock=None, sleep=None, profile="rpcfast_start", max_tx_version=<профіль>, max_batch=<профіль>, rate_per_second=<профіль>, burst=<профіль>)`** (`commitment` — keyword-only, береться з `IngestConfig`, умовчання немає: принцип III; `profile`, `max_tx_version`, `max_batch`, `rate_per_second`, `burst` — keyword-only константи протоколу/провайдера, не результат-впливаючі (за невдалого значення збір чесно стає неповним, а не іншим); не передані параметри беруться з профілю, явно передані перекривають профіль поодинці; `from_env(..., url_var="UNMASK_RPC_URL", profile=..., ...)` — зручність: URL зі змінної середовища `url_var`, приймає ті самі параметри)
- JSON-RPC 2.0 через `httpx.Client`; `transport` підмінний (`httpx.MockTransport` у тестах).
- `get_transactions` — послідовні JSON-RPC batch, кожен не більше `max_batch` підписів (за замовчуванням 25; batch більший за ємність кошика провайдера не пройде ніколи); `rpc.page_size` — лише розмір сторінки `get_signatures_for_address` (≤ 1000); пачку `get_transactions` у ядрі задає `rpc.tx_batch_size` (за замовчуванням 25 = `max_batch`: «все або нічого» викидає виклик, що не вмістився в бюджет, тож пачка має бути не більшою за те, що встигає виконатись); `rpc.max_concurrency` адаптер не використовує. JSON-RPC `id` = позиція підпису в усьому виклику (не в під-batch); порядок результату береться за `id`, а не за порядком відповіді; результат не залежить від `max_batch`. «Все або нічого»: помилка будь-якого під-batch (після повторів) чи елемента → виняток на весь виклик, наступні під-batch не відправляються, часткового результату немає (`None` лише для `result: null`): інакше збій став би «відсутністю даних» (принцип V). Перед кожним під-batch — повна перевірка дедлайну (`expired()` / `request_timeout() <= 0` → `RpcBudgetTimeout`, нуль подальших запитів). Повтори — на рівні під-batch (повторюється лише той, що впав), лише для збоїв рівня HTTP/транспорту.
- Глобальний лімітер (кошик «одиниць запиту», T-053; узагальнює пейсер T-049). Ліміт провайдера — ШВИДКІСТЬ, а не розмір batch, і в Helius free — ОДИН на всі методи, тому кошик спільний для ВСІХ методів адаптера. Ціна в одиницях: `get_account_info` = 1; `get_signatures_for_address` = 1; `get_token_accounts_by_owner` = 1 на кожну програму (Token, Token-2022 — два запити, кожен окремо проходить кошик); `get_transactions` = кількість елементів кожного під-batch. Кошик: `burst` токенів на старті, поповнення `rate_per_second` за `clock.monotonic()`, не більше `burst`. Перед кожною спробою запиту на k одиниць (також повтором): поповнити; бракує — чекати `(k − tokens) / rate_per_second` через `sleep`; якщо очікування ≥ `deadline.remaining()` — `RpcBudgetTimeout` без запиту, без сну й без зміни стану кошика (у цей момент `deadline.expired()` ще хибне; ядро мапить саме цей підклас у `budget_exhausted` безумовно — T-051; resume продовжить; це стосується й випадку після 429 — тоді назовні `RpcBudgetTimeout`, а не `RpcRateLimited`); інакше після паузи списати k і відправити (перевірка `expired()` / `request_timeout()` — після паузи). HTTP 429 на будь-якому запиті → локальні токени = 0, далі звичайний повтор (backoff/`Retry-After` у межах дедлайну), повтор знову проходить через лімітер; 5xx і мережеві збої кошик не обнуляють; JSON-RPC помилка ліміту в тілі HTTP 200 не повторюється і кошик не обнуляє. `rate_per_second = math.inf` вимикає лімітер; скінченна швидкість ≥ 0.1/с, тож одна пауза ≤ `burst / 0.1` с навіть за безкінечного дедлайну. Адаптер однопотоковий, як і ядро: стан кошика не синхронізується. Старі імена T-049 (`tx_rate_per_second`, `tx_burst`) не приймаються (`TypeError`).
- Профілі провайдерів — константна незмінна таблиця `RPC_PROFILES` у `rpc/http.py` (не секрети, на результат не впливають); за замовчуванням `rpcfast_start`; невідомий профіль (також не-рядок) → `ValueError` з переліком відомих профілів, без значення й без URL:

  | Профіль | `rate_per_second` | `burst` | `max_batch` | `max_tx_version` | Виміряно |
  |---|---|---|---|---|---|
  | `rpcfast_start` | 12 | 30 | 25 | 1 | RPC Fast «Start» (ревʼю T-049, 16 живих прогонів): кошик ≈ 40 (±3) getTransaction-елементів, поповнення ≈ 15–16/с; batch 40 проходить, 50 → HTTP 429 з тілом `{"error":{"code":-32005}}` без `Retry-After`; 20/с з кошиком 40 давали 429 у кожному прогоні; запас ≈ 20–25% |
  | `helius_free` | 4.5 | 20 | 10 | 1 | Helius free (known-issues §8): один кошик на всі методи (`getSignaturesForAddress`, `getTokenAccountsByOwner`, `getAccountInfo`, `getTransaction`), кожен елемент batch = одиниця; виміряно: ємність ≈ 30 (batch 30 проходить, 35 — ні), поповнення ≈ 5 одиниць/с (10/12/16 запитів/с протягом 6 с → прийнято 59–60 = 30 + 5·6; сталі 5/с — без 429; «8/с без 429» спостерігалось лише на коротких тестах до вичерпання кошика). Профіль навмисно нижче: запас ≈ 10% за швидкістю, ≈ 33% за ємністю; перевірено живо: 0×429 за 80 с |
- Наслідок для бюджету: пропускна здатність ≈ `burst + rate_per_second × бюджет` одиниць запиту на холодний запит, і це одиниці ВСІХ методів, а не лише getTransaction: `rpcfast_start` ≈ 30 + 12·40 ≈ 510, `helius_free` ≈ 20 + 4.5·40 ≈ 200 (hub-сценарій фікстур на `helius_free` ≈ (Σ − 20) / 4.5 с лише на паузи лімітера). Більше — `budget_exhausted` / `incomplete` і продовження через resume.
- `profile`: один із ключів `RPC_PROFILES`; `max_tx_version`: ціле ≥ 0; `max_batch`: ціле ≥ 1 і ≤ `burst` (перевіряється над підсумковими значеннями після накладання профілю); `rate_per_second`: число ≥ 0.1 (скінченне) або `math.inf`; NaN — ні; `burst`: ціле ≥ 1 (`bool` ніде не приймається); інакше `ValueError` з назвою параметра, без значення й без URL. `from_env`: `url_var` — ім'я змінної середовища (`[A-Za-z_][A-Za-z0-9_]*`), інакше `ValueError` без значення; змінна відсутня/порожня/з невалідним URL → `ConfigError`, у тексті лише ім'я змінної, ніколи її значення.
- JSON-RPC `-32015` (непідтримана версія транзакції, напр. майбутня v2) → `RpcUnavailable` з фіксованою міткою `jsonrpc error code=-32015 (unsupported transaction version)` (для елемента batch — з префіксом `batch item <позиція>: `); ніколи `None` і ніколи `RpcRateLimited`.
- Секрет (URL з ключем) не потрапляє в `repr`/`str`, у виключення (включно з `__cause__`/`__context__`), `detail` і логи. `detail` і тексти винятків адаптера **ніколи не містять вільного тексту ззовні** (ні `error.message`/`error.data`, ні тіла, ні заголовків, ні назви класу винятку транспорту): лише категорію з фіксованої таблиці адаптера — за HTTP-статусом (`http 401 unauthorized (check API key)`, `http 5xx server error`, …), за кодом JSON-RPC (`jsonrpc error code=<int> (<мітка>)`; код — лише справжній `int` у межах int32, невідомий — без мітки, інший тип — `jsonrpc error (malformed code)`), за класом мережевої помилки (`network error: ConnectError`), `invalid JSON in response`, `unexpected response shape: …`. Причина: «очищення» довільного тексту від ключа принципово ненадійне (кожне оборотне кодування його обходить). Редиректи не виконуються (3xx → `RpcUnavailable`).
- Конструктор повністю валідує URL (схема http/https, хост, порт 1..65535); будь-яка відмова → одне `ValueError("invalid RPC URL")` без URL і без ланцюга винятків; `from_env` → `ConfigError` без URL.
- Усі записи логерів `httpx`/`httpcore*` відкидаються (у них URL, хост або сирі заголовки відповіді); транспортна діагностика цих бібліотек недоступна.
- `before=` передається як є; наявність підпису в історії адреси адаптер не перевіряє (на живому RPC поведінка за слотом/позицією).
- Ключ API — лише зі змінної середовища (за замовчуванням `UNMASK_RPC_URL`, ім'я задається `url_var`, напр. `UNMASK_RPC_URL_ALT` для другого провайдера); у YAML і в git не потрапляє.
