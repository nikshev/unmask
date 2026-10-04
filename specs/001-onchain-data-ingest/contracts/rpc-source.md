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

### `get_transactions(signatures)` ↔ `getTransaction(sig, {encoding: "jsonParsed", maxSupportedTransactionVersion: 0, commitment})`

Пакетний виклик: елемент i відповідає `signatures[i]`; `None` — транзакцію не знайдено (обрізана історія). Форма елемента:

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

Адаптер піднімає **тільки** ці винятки (`rpc/protocol.py`); усе інше з транспорту — дефект адаптера:

| Виняток | Коли | Мапиться у `MissingReason` |
|---|---|---|
| `RpcRateLimited(retry_after: float \| None)` | HTTP 429 або JSON-RPC помилка з кодом ліміту | `rate_limited` |
| `RpcTimeout` | таймаут запиту або `deadline` вичерпано до відправки | `timeout` (адаптер) / `budget_exhausted` (ядро, якщо винен дедлайн) |
| `RpcUnavailable(detail)` | HTTP 5xx, мережевий збій, JSON-RPC `error`, невалідний JSON | `unavailable` |

Захисний шар ядра (рішення власника процесу за ревʼю T-016): будь-який інший `RpcError` (базовий клас або невідомий підклас) ядро мапить у `unavailable` з класом винятку в `detail` — «не вдалось дізнатись», а не збій `collect`. Підклас відомого винятку зберігає його причину (`isinstance`). Винятки поза ієрархією `RpcError` (ValueError, KeyError тощо) — дефект адаптера й виходять з `collect` назовні; ніколи не маскуються під «чисто» (з `IngestService.collect` теж виходять назовні як дефект програми — див. `ingest-service.md`, «Гарантії»: ні `Rejection`, ні `incomplete` не можуть чесно описати дефект адаптера, тож єдина безпечна поведінка — гучне падіння).

Повторні спроби (`rpc.max_retries`, `rpc.retry_backoff_seconds`) — відповідальність адаптера, у межах `deadline`. Ядро отримує або результат, або один із трьох винятків.

## `Deadline`

`budget.py::Deadline(clock: Clock, seconds: float)` — `remaining() -> float`, `expired() -> bool`, `request_timeout(cap: float) -> float` (мінімум із залишку й `rpc.request_timeout_seconds`). Адаптер перед кожним запитом: `if deadline.expired(): raise RpcTimeout("budget")`.

## Вимоги до реалізацій

**`FixtureRpcSource(scenario_dir, *, failures=None, clock=None)`**
- Читає `rpc.json` сценарію: `{"getAccountInfo": {addr: value|null}, "getSignaturesForAddress": {addr: [SignatureInfo…]}, "getTransaction": {sig: RawTransaction|null}, "getTokenAccountsByOwner": {owner: [TokenAccountInfo…]}}`.
- Веде журнал `calls: list[(method, params)]` — тести доводять «0 звернень» (FR-001-12) і «лише недоотримане» (FR-001-13) саме за ним.
- `failures` — політика: `FailAfter(n_calls, exc)`, `FailFor(address|signature, exc, times)`; `clock` — `FakeClock`, який просувається на `advance_per_call` на кожному виклику.
- Адреса, відсутня в `rpc.json`, → порожня історія (не помилка): це покриває edge case «гаманець без вхідних переказів».

**`HttpRpcSource(url, config.rpc, transport=None, *, commitment, clock=None, sleep=None)`** (`commitment` — keyword-only, береться з `IngestConfig`, умовчання немає: принцип III; `from_env(...)` — зручність для `UNMASK_RPC_URL`)
- JSON-RPC 2.0 через `httpx.Client`; `transport` підмінний (`httpx.MockTransport` у тестах).
- `get_transactions` — один JSON-RPC batch на кожні `rpc.page_size` підписів (реалізовано batch; `rpc.max_concurrency` адаптер не використовує); порядок результату береться за JSON-RPC `id` (позицією підпису), а не за порядком відповіді. Помилка одного елемента batch → помилка всього виклику (`None` лише для `result: null`): інакше збій став би «відсутністю даних» (принцип V).
- Секрет (URL з ключем) не потрапляє в `repr`/`str`, у виключення (включно з `__cause__`/`__context__`), `detail` і логи. `detail` і тексти винятків адаптера **ніколи не містять вільного тексту ззовні** (ні `error.message`/`error.data`, ні тіла, ні заголовків, ні назви класу винятку транспорту): лише категорію з фіксованої таблиці адаптера — за HTTP-статусом (`http 401 unauthorized (check API key)`, `http 5xx server error`, …), за кодом JSON-RPC (`jsonrpc error code=<int> (<мітка>)`; код — лише справжній `int` у межах int32, невідомий — без мітки, інший тип — `jsonrpc error (malformed code)`), за класом мережевої помилки (`network error: ConnectError`), `invalid JSON in response`, `unexpected response shape: …`. Причина: «очищення» довільного тексту від ключа принципово ненадійне (кожне оборотне кодування його обходить). Редиректи не виконуються (3xx → `RpcUnavailable`).
- Конструктор повністю валідує URL (схема http/https, хост, порт 1..65535); будь-яка відмова → одне `ValueError("invalid RPC URL")` без URL і без ланцюга винятків; `from_env` → `ConfigError` без URL.
- Усі записи логерів `httpx`/`httpcore*` відкидаються (у них URL, хост або сирі заголовки відповіді); транспортна діагностика цих бібліотек недоступна.
- `before=` передається як є; наявність підпису в історії адреси адаптер не перевіряє (на живому RPC поведінка за слотом/позицією).
- Ключ API — лише зі змінної середовища (`UNMASK_RPC_URL` із вбудованим ключем); у YAML і в git не потрапляє.
