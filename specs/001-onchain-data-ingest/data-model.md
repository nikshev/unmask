# Data Model: Збір ончейн-даних по токену (001)

Усі сутності — незмінні Python-дата-класи (`frozen=True`) у `src/unmask/ingest/model.py`, крім `CollectionState` (змінний, живе лише всередині збору й у партиційному сховищі кешу). Суми — цілі в базових одиницях (lamports; базові одиниці SPL з окремим полем `decimals`), жодних float. Адреси — base58-рядки. Час — Unix-секунди (`block_time`) з RPC; порядок і межі визначаються слотом, не часом (див. research R-1, R-6).

## Перелічення

| Тип | Значення | Зміст |
|---|---|---|
| `Asset` | `sol` \| `spl:<mint>` | актив переказу; WSOL лишається `spl:So111…112` |
| `AddressType` | `wallet` \| `off_curve` | ключ на кривій ed25519 / поза нею (PDA) |
| `CompletenessStatus` | `complete` \| `incomplete` | статус результату |
| `MissingReason` | `rate_limited` \| `timeout` \| `unavailable` \| `corrupt_data` \| `budget_exhausted` | чому історію не отримано |
| `UnexpandedReason` | `high_degree` \| `signature_cap` | чому розгортання через вершину обмежено |
| `RejectKind` | `invalid_address` \| `token_not_found` | явна відмова без збою |

## Сутності

### `IngestConfig` — конфігурація збору (принцип III)

Завантажується з `config/ingest.yaml` (`config.py::load_config(path) -> IngestConfig`). Повна схема й правила версіонування — `contracts/config-ingest.md`.

| Поле | Тип | Обмеження | Типове |
|---|---|---|---|
| `version` | int | ≥ 1; підіймається з записом у `config/CHANGELOG.md` | 1 |
| `first_buyers_n` | int | 1 ≤ N ≤ 500; у продакшн-конфігу 200–500 (spec) | 300 |
| `funding_depth` | int | 1 ≤ d ≤ 3 | 2 |
| `counterparty_threshold` | int | ≥ 1 | 200 |
| `max_signatures_per_wallet` | int | ≥ 1 | 300 |
| `collect_spl_inbound` | bool | — | true |
| `time_budget_seconds` | float | > 0 | 40 |
| `commitment` | str | `finalized` \| `confirmed` | finalized |
| `rpc.page_size` | int | 1 ≤ p ≤ 1000 | 1000 |
| `rpc.request_timeout_seconds` | float | > 0 | 10 |
| `rpc.max_retries` | int | ≥ 0 | 2 |
| `rpc.retry_backoff_seconds` | float | ≥ 0 | 0.5 |
| `rpc.max_concurrency` | int | ≥ 1 | 8 |

Порушення обмеження або відсутнє поле → `ConfigError` при завантаженні (гучно, не тихо).

### `Buyer` — покупець

| Поле | Тип | Правило |
|---|---|---|
| `wallet` | str | власник токен-рахунку, що отримав mint |
| `rank` | int | 1-базована позиція у впорядкованій вибірці |
| `first_buy_signature` | str | підпис транзакції першої купівлі |
| `first_buy_slot` | int | слот першої купівлі |
| `first_buy_time` | int \| None | `blockTime`; `None`, якщо RPC не повернув |
| `received_amount` | int | Δ mint у базових одиницях, > 0 |
| `spent` | tuple[`Spend`, …] | що віддано: `(asset, amount)`; непорожній |
| `programs` | tuple[str, …] | programId інструкцій верхнього рівня, у порядку транзакції |
| `address_type` | `AddressType` | research R-5 |

Унікальність — за `wallet`. Порядок у результаті — `(first_buy_slot, first_buy_signature, wallet)`.

Правило визнання купівлі (`purchases.py::detect_purchases(tx, mint) -> list[Purchase]`) — research R-2; `Purchase` — проміжний запис `(wallet, signature, slot, block_time, received_amount, spent, programs)`, із якого `buyers.py` будує `Buyer` після відбору перших N.

### `Transfer` — вхідний переказ (FR-001-06)

| Поле | Тип | Правило |
|---|---|---|
| `signature` | str | первинне посилання (принцип V) |
| `slot` | int | |
| `block_time` | int \| None | |
| `instruction_path` | str | `"i"` або `"i.j"` (research R-9) |
| `sender` | str | для SPL — власник токен-рахунку-джерела |
| `receiver` | str | для SPL — власник токен-рахунку-призначення; гаманець, що розгортався |
| `asset` | `Asset` | |
| `amount` | int | > 0, базові одиниці |
| `decimals` | int \| None | для `spl:*` з `uiTokenAmount.decimals`; для `sol` — `None` (9 мається на увазі) |
| `depth` | int | 1 — безпосередньо в покупця, …, ≤ `funding_depth`; мінімальна з усіх шляхів |

Ключ унікальності — `(signature, instruction_path)`. Правила включення: `sender != receiver`; транзакція без `meta.err`; транзакція **строго раніше** за транзакцію-межу вершини-отримувача (research R-1; сама транзакція-межа, зокрема доставка токена в купівельній транзакції, не включається). Порядок у результаті — `(slot, signature, instruction_path)`.

Джерела розпізнавання (`parse.py`): System `transfer`, `transferWithSeed`, `createAccount`, `createAccountWithSeed` (lamports) → `sol`; spl-token / spl-token-2022 `transfer`, `transferChecked` → `spl:<mint>`, власники через `pre/postTokenBalances[accountIndex].owner`. Верхній рівень і `meta.innerInstructions`.

### `UnexpandedNode` — нерозгорнута вершина (FR-001-08)

| Поле | Тип | Правило |
|---|---|---|
| `wallet` | str | |
| `depth` | int | глибина вершини (0 — покупець) |
| `reason` | `UnexpandedReason` | `high_degree` має пріоритет над `signature_cap` |
| `counterparties_seen` | int | унікальних відправників на момент зупинки |
| `signatures_seen` | int | переглянуто підписів |
| `signatures_truncated` | bool | історія довша за `max_signatures_per_wallet` |

Семантика: `high_degree` — відправники цієї вершини далі не розгортаються, вхідні перекази понад поріг не збираються, зібрані до порога лишаються; `signature_cap` — переглянуто лише найновіші до межі `max_signatures_per_wallet` підписів, знайдені відправники розгортаються нормально. Порядок — `(depth, wallet)`.

### `MissingHistory` — недоотримана історія (FR-001-09)

| Поле | Тип | Правило |
|---|---|---|
| `wallet` | str | |
| `depth` | int | |
| `reason` | `MissingReason` | |
| `detail` | str | підпис/метод/повідомлення — що саме не вдалося |

Один запис на пару `(wallet, reason)`; наявність хоча б одного → статус `incomplete`. Порядок — `(depth, wallet)`.

### `BuyersCompleteness`

| Поле | Тип | Правило |
|---|---|---|
| `complete` | bool | перелічення історії mint дійшло до найстарішого запису й N (або всіх) покупців відібрано |
| `reason` | `MissingReason` \| None | якщо `complete == false` |
| `detail` | str | курсор/повідомлення |

### `Completeness` (FR-001-09, FR-001-10)

| Поле | Тип | Правило |
|---|---|---|
| `status` | `CompletenessStatus` | **похідне**: `complete` тоді й лише тоді, коли `missing` порожній **і** `buyers.complete == true`; інакше `incomplete`. Конструюється лише через `Completeness.derive(missing, buyers)`; прямий виклик з суперечливими полями неможливий |
| `missing` | tuple[`MissingHistory`, …] | |
| `buyers` | `BuyersCompleteness` | |

`unexpanded[]` **не** робить результат неповним: обмеження розгортання — свідоме й видиме рішення конфігурації, а не збій.

### `RunMetadata` (FR-001-14)

| Поле | Тип |
|---|---|
| `mint` | str |
| `analyzed_at` | int (Unix-секунди, з `Clock.wall()`) |
| `wallets_analyzed` | int — кількість покупців у вибірці |
| `config_version` | int |
| `first_buyers_n`, `funding_depth`, `counterparty_threshold`, `max_signatures_per_wallet` | int — використані значення |
| `collect_spl_inbound` | bool |
| `time_budget_seconds`, `elapsed_seconds` | float |
| `rpc_calls` | int — звернень до джерела за цей прогін |
| `transactions_scanned` | int — транзакцій mint, розібраних правилом купівлі |
| `source` | str — `fixture:<scenario>` \| `http` |
| `resumed` | bool — чи продовжено з партиційного стану |
| `served_from_cache` | bool |

### `IngestResult` — результат збору

```
IngestResult(
  metadata: RunMetadata,
  completeness: Completeness,
  buyers: tuple[Buyer, …],              # рівно min(N, наявних), упорядковано
  transfers: tuple[Transfer, …],        # унікальні, упорядковано
  unexpanded: tuple[UnexpandedNode, …],
)
```

Серіалізація (`serialize.py::to_dict`) — за `contracts/ingest-result.schema.json`.

### `Rejection` — явна відмова (FR-001-11)

`Rejection(kind: RejectKind, mint: str, detail: str)`; `detail` для `token_not_found`: `account_missing` | `not_a_mint`. Повертається значенням, не винятком.

`IngestOutcome = IngestResult | Rejection`.

## Стан збору і кеш

### `CollectionState` (змінний; `collector.py`)

Поля: `mint`, `config_version`, `signature_cursor` (останній підпис перегортання історії mint або `None`), `mint_signatures` (список `(signature, slot, block_time, err)`), `mint_history_exhausted: bool`, `purchases_by_wallet`, `buyers` (після відбору), `frontier_by_depth: dict[int, dict[wallet, cutoff_signature]]`, `expanded: set[wallet]`, `transfers: dict[(signature, instruction_path), Transfer]`, `unexpanded`, `missing: dict[(wallet, reason), MissingHistory]`, `tx_cache: dict[signature, ParsedTx]`, `rpc_calls`, `transactions_scanned`.

Переходи:

```
new ──enumerate buyers──▶ buyers_selected ──BFS depth 1..d──▶ done(complete)
  │                           │                                  ▲
  │ бюджет/збій               │ бюджет/збій                      │
  ▼                           ▼                                  │
partial(buyers.complete=false)   partial(missing≠∅) ──resume: лише missing/курсор──┘
```

`resume` повторює лише: перегортання з `signature_cursor` (якщо `mint_history_exhausted == false`), гаманці з `missing` (видаляючи їх запис при успіху), нерозгорнуті рівні BFS. Гаманці з `expanded` не запитуються повторно (перевіряється журналом викликів фікстурного джерела). Якщо `config_version` партиційного стану ≠ поточному — стан відкидається, збір починається заново.

### `ResultCache` (`cache.py`)

```
complete: dict[mint, IngestResult]       # лише status == complete (FR-001-12)
partial:  dict[mint, CollectionState]    # ніколи не віддається як результат (FR-001-13)
```

`put_complete` відхиляє `IngestResult` зі статусом `incomplete` винятком `CacheInvariantError` — захист від регресії, а не очікуваний шлях. Повторний запит: `complete` → віддати копію з `served_from_cache=true`, 0 звернень до джерела; `partial` → `resume`; інакше — новий збір.

## Зв'язки

```
IngestConfig ──читається──▶ Collector ──використовує──▶ RpcSource (Protocol)
                                 │                           ▲
                                 │ створює                   │ реалізують
                                 ▼                 FixtureRpcSource · HttpRpcSource
                            IngestResult ──кешується (complete)──▶ ResultCache
                                 │                           ▲
                                 │ серіалізується             │ зберігає partial
                                 ▼                           │
                     contracts/ingest-result.schema.json   CollectionState
```

Buyer 1 — * Transfer (через `receiver` на глибині 1 і далі транзитивно через `sender`); Buyer/вершина 0..1 UnexpandedNode; вершина 0..* MissingHistory.
