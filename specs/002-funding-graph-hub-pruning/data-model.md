# Data Model: Граф фінансування та відсікання хабів (002)

Усі сутності — незмінні Python-дата-класи (`frozen=True`), що перевіряють себе при побудові (некоректний стан — `TypeError`/`ValueError` гучно, як у `ingest/model.py`). Суми — цілі в базових одиницях (жодних float, крім часток `*_share`, що є похідними відношеннями двох цілих і також зберігають чисельник/знаменник поруч). Адреси — base58-рядки. Порядок кожного кортежу визначений ключем лише з даних (детермінізм, FR-002-04).

Розташування: `src/unmask/graph/model.py` (граф, звіт, результат), `src/unmask/hubs/config.py` (конфігурація), `src/unmask/ingest/model.py` (розширення 001 для swap-and-send).

Доповнення за калібруванням (`calibration.md`, research R-22, FR-002-22): критерій `dust_fanout`, два виміри `NodeMeasures`, два пороги `HubThresholds`/`ThresholdsSnapshot` — позначені в таблицях нижче з посиланням на задачі T-054/T-055, бо `model.py` і `config.py` уже написані (T-023…T-025) і змінюються цими задачами.

## Перелічення

| Тип | Значення | Зміст |
|---|---|---|
| `NodeRole` | `buyer` \| `funder` \| `delegated_payer` \| `delegated_receiver` | ролі вершини; множина, непорожня |
| `EdgeKind` | `transfer` \| `delegated_buy` | вид ребра (FR-002-18: не зливаються) |
| `HubCriterion` | `known_list` \| `degree` \| `one_off_senders` \| `ingest_high_degree` \| `dust_fanout` | критерії FR-002-07 (а)…(г) і FR-002-22 (пилове роздавання, research R-22); порядок хітів — за рядковим значенням: `degree < dust_fanout < ingest_high_degree < known_list < one_off_senders` |
| `CriterionSource` (у `detail`) | `list:<category>` \| `address_type:off_curve` \| `unexpanded:high_degree` \| `measured` | звідки спрацювання |
| `GraphWarning` | `address_lists_not_applied` \| `giant_component` \| `empty_graph` \| `all_sources_pruned` \| `delegated_incomplete` | попередження звіту (R-16) |
| `GraphCompletenessStatus` | `complete` \| `incomplete` | похідний статус (FR-002-05) |
| `DelegatedSide` | `payer` \| `receiver` | сторона кандидата без пари (001-розширення) |

## Розширення фічі 001 (`src/unmask/ingest/model.py`; research R-2…R-4)

### `DelegatedLink` — зв'язок «делегована купівля»

| Поле | Тип | Правило |
|---|---|---|
| `signature` | str | первинне посилання (принцип V) |
| `slot` | int ≥ 0 | |
| `block_time` | int \| None | |
| `payer` | str | платник: `token_delta(M) == 0`, витратив кошти |
| `receiver` | str | отримувач: `token_delta(M) > 0`, нічого не витратив; `receiver != payer` |

Ключ унікальності — `signature` (один зв'язок на транзакцію за правилом 1:1). Порядок — `(slot, signature, payer, receiver)`.

### `UnpairedCandidate` — кандидат без пари (FR-002-16)

| Поле | Тип | Правило |
|---|---|---|
| `signature`, `slot`, `block_time` | як вище | |
| `wallet` | str | |
| `side` | `DelegatedSide` | |
| `detail` | str | `"payers=<p> receivers=<r>"` — чому не зіставлено |

Ключ — `(signature, wallet, side)`. Порядок — `(slot, signature, wallet, side)`.

### `DelegatedAnalysis` (FR-002-19)

| Поле | Тип | Правило |
|---|---|---|
| `links` | tuple[`DelegatedLink`, …] | упорядковано, без дублів |
| `unpaired` | tuple[`UnpairedCandidate`, …] | упорядковано, без дублів |
| `complete` | bool | **похідне**: `== buyers_completeness.complete` (те саме вікно й той самий розбір) |
| `reason` | str \| None | `MissingReason.value` при неповноті або `"not_analyzed"`; `None` при `complete` |
| `detail` | str | з `BuyersCompleteness.detail` |

Будується лише через `DelegatedAnalysis.derive(links, unpaired, buyers: BuyersCompleteness)`; константа `DelegatedAnalysis.NOT_ANALYZED` (`links=(), unpaired=(), complete=False, reason="not_analyzed"`) — чесне умовчання для `IngestResult`, яке колектор і сервіс **ніколи** не емітують (тест T-044). `complete=True` з `reason != None` або навпаки → `ValueError`.

### Зміни наявних сутностей 001

- `IngestResult.delegated: DelegatedAnalysis = DelegatedAnalysis.NOT_ANALYZED` — нове останнє поле; решта полів і їхній порядок без змін.
- `CollectionState.delegated_by_signature: dict[str, tuple[DelegatedLink | UnpairedCandidate, ...]]` — перевиводиться на кожному `enumerate_buyers` (R-4); рядок у `cache._FIELD_COPY`: `dict`.
- Контракт `contracts/ingest-result.schema.json` → 1.1: необов'язкове `result.delegated` (`$defs/delegated`), `additionalProperties:false` збережено.

## Конфігурація відсікання (`src/unmask/hubs/config.py`; принцип III)

### `HubThresholds` — з `config/hubs.yaml`

| Поле | Тип | Обмеження | v1 | v2 (T-054, research R-22) |
|---|---|---|---|---|
| `version` | int | ≥ 1; підіймається із записом у `config/CHANGELOG.md` (розділ `# config/hubs.yaml`) + `sha256` | 1 | 2 |
| `degree_threshold` | int | ≥ 1; хаб iff `degree > degree_threshold` | 100 | 100 (свідомо без змін — `calibration.md`) |
| `one_off_senders_share` | float | 0 ≤ x ≤ 1; хаб iff `one_off_share > x` | 0.8 | 0.8 |
| `one_off_min_senders` | int | ≥ 2; критерій застосовний iff `unique_senders >= one_off_min_senders` | 10 | 10 |
| `giant_component_warn_share` | float | 0 < x ≤ 1; попередження iff `share_after > x` | 0.5 | 0.5 |
| `prune_off_curve` | bool | | true | true |
| `prune_ingest_high_degree` | bool | | true | true |
| `dust_amount_lamports` | int | ≥ 1; хаб iff `median_to_buyers < dust_amount_lamports` (строго **менше** — пил; `1` вимикає критерій, бо суми ребер ≥ 1) | — | 1 000 000 (0,001 SOL) |
| `dust_min_fanout` | int | ≥ 2; критерій застосовний iff `buyer_fanout >= dust_min_fanout` (передумова, включно) | — | 5 |

Поля без умовчань: у файлі v2 обов'язкові всі дев'ять ключів; файл v1 (без `dust_*`) → `ConfigError(dust_amount_lamports: missing required field)`.

### `AddressLists` — з `config/hub_addresses.yaml`

| Поле | Тип | Обмеження |
|---|---|---|
| `version` | int ≥ 1 | журнал — розділ `# config/hub_addresses.yaml` |
| `categories` | dict[str, tuple[str, …]] | ключі — рівно відомі категорії: `system_programs`, `token_programs`, `dex_routers`, `amm_programs`, `launchpads`, `exchanges`, `market_makers`; порожні списки дозволені |
| `index` | dict[address, category] | похідне; адреса в двох категоріях → `ConfigError`; адреса не base58/не 32 байти → `ConfigError` |

### `HubConfig`

```
HubConfig(thresholds: HubThresholds, lists: AddressLists | None, thresholds_digest: str, lists_digest: str | None)
```

`lists is None` ⇔ файл списків відсутній або нечитабельний (R-13) → `lists_applied = false`. Невідоме/відсутнє поле чи значення поза межами в будь-якому з файлів → `ConfigError` з назвою поля (тихих умовчань у коді немає).

## Граф (`src/unmask/graph/model.py`)

### `NodeMeasures` — виміри для критеріїв хаба (research R-7, R-8, R-22)

| Поле | Тип | Правило |
|---|---|---|
| `degree` | int ≥ 0 | унікальні контрагенти, вхідні ∪ вихідні, всі види ребер і активи |
| `unique_senders` | int ≥ 0 | унікальні відправники вхідних ребер `transfer` |
| `one_off_senders` | int ≥ 0 | з них із сумарним `count == 1`; `≤ unique_senders` |
| `one_off_share` | float \| None | `one_off_senders / unique_senders`; `None`, якщо `unique_senders == 0` |
| `buyer_fanout` | int ≥ 0 | кількість різних вершин із роллю `buyer`, до яких є вихідне ребро `(transfer, v, b, sol)`; SPL і `delegated_buy` не рахуються (T-055 додає поле; T-032 обчислює) |
| `median_to_buyers` | int \| None | верхня медіана `Edge.amount` тих самих ребер (одне значення на покупця): `sorted(amounts)[len(amounts) // 2]`, у лампортах; `None` ⇔ `buyer_fanout == 0`; інакше `≥ 1` |

Усі шість полів без умовчань (конструктор вимагає кожне — як решта моделі). `median_to_buyers` — ціле з даних, не похідний float: конструктор перевіряє лише `None`-інваріант і `≥ 1`, бо відтворити медіану без списку сум неможливо (на відміну від `one_off_share`).

### `UnexpandedMark` — атрибут нерозгорнутості (FR-002-06)

`(reason: high_degree | signature_cap, counterparties_seen: int, signatures_seen: int, signatures_truncated: bool)` — копія `UnexpandedNode` без `depth`/`wallet`.

### `Node` — вершина (FR-002-03)

| Поле | Тип | Правило |
|---|---|---|
| `address` | str | ключ унікальності |
| `roles` | frozenset[`NodeRole`] | непорожня; `buyer` ⇔ `buyer_rank is not None` |
| `depth` | int ≥ 0 | мінімальна глибина від покупців (R-6); `buyer` ⇒ `depth == 0` |
| `buyer_rank` | int ≥ 1 \| None | `Buyer.rank` |
| `address_type` | `wallet` \| `off_curve` | для покупців — з 001; інакше обчислено (R-6) |
| `unexpanded` | `UnexpandedMark` \| None | |
| `measures` | `NodeMeasures` | з `graph/measures.py::compute` на **повному** графі (до відсікання); у відсіченому графі лишаються як були — це значення, за якими приймалось рішення |

Порядок — за `address`.

### `EdgeRef` — первинне посилання (FR-002-02)

`(signature: str, slot: int ≥ 0, instruction_path: str | None)` — `instruction_path` обов'язковий для `transfer` (`"i"`/`"i.j"`), `None` для `delegated_buy`. Порядок — `(slot, signature, path_key(instruction_path))`.

### `Edge` — ребро (FR-002-01, FR-002-02, FR-002-18)

| Поле | Тип | `transfer` | `delegated_buy` |
|---|---|---|---|
| `kind` | `EdgeKind` | | |
| `sender` | str | відправник | платник |
| `receiver` | str | отримувач; `!= sender` | отримувач; `!= sender` |
| `asset` | `Asset` \| None | `sol` / `spl:<mint>` | `None` |
| `amount` | int ≥ 1 \| None | сума в базових одиницях активу | `None` |
| `decimals` | int \| None | з переказів (однакові) або `None` для `sol` | `None` |
| `count` | int ≥ 1 | кількість переказів `== len(refs)` | кількість транзакцій `== len(refs)` |
| `first_slot`, `last_slot` | int | `first ≤ last`; з `refs` | те саме |
| `first_time`, `last_time` | int \| None | `block_time` першого/останнього за слотом | те саме |
| `refs` | tuple[`EdgeRef`, …] | непорожній, упорядкований, без дублів `(signature, instruction_path)` | без дублів `signature` |

Ключ унікальності — `(kind, sender, receiver, asset)`. Порядок — `(kind, sender, receiver, asset or "")`.

### `FundingGraph`

```
FundingGraph(nodes: tuple[Node, …], edges: tuple[Edge, …])
```

Інваріанти (`__post_init__`): адреси вершин унікальні; кожен кінець ребра — вершина; ключі ребер унікальні; кортежі впорядковані канонічно (конструктор сортує сам). Методи: `node(address)`, `without(addresses: frozenset) -> FundingGraph` (новий граф без вершин і інцидентних ребер; `measures` вершин не перераховуються), `incident(address) -> tuple[Edge]`, `buyers() -> tuple[Node]`.

Порожній граф (0 вершин) — валідний.

## Відсікання (`src/unmask/hubs/`)

### `CriterionHit` — одне спрацювання

| Поле | Тип | Правило |
|---|---|---|
| `criterion` | `HubCriterion` | |
| `measured` | int \| float \| None | `degree`: int; `one_off_senders`: float (частка); `ingest_high_degree`: `counterparties_seen`; `dust_fanout`: int — `median_to_buyers` у лампортах; `known_list`: `None` |
| `threshold` | int \| float \| None | з конфігу; для `ingest_high_degree` — `metadata.counterparty_threshold` 001; для `dust_fanout` — `dust_amount_lamports`; для `known_list` — `None` |
| `detail` | str | `list:<category>` \| `address_type:off_curve` \| `unexpanded:high_degree` \| `measured` |
| `lists_version` | int \| None | лише для `detail = list:*` |

Інваріант (правило R-9 закодоване в типі: хіт на порозі не конструюється): `criterion in {degree, one_off_senders, ingest_high_degree}` ⇒ `measured > threshold`; `criterion == dust_fanout` ⇒ `measured < threshold` (напрямок — властивість критерію, строгість і «рівно поріг — не хаб» — спільні, FR-002-14). Передумова `dust_fanout` (`buyer_fanout >= dust_min_fanout`) у хіті окремого поля не має — вона читається з `measures.buyer_fanout` запису/позначки, так само як `unique_senders` для `one_off_senders`.

### `PruneRecord` — запис відсікання (FR-002-09)

| Поле | Тип | Правило |
|---|---|---|
| `address` | str | не покупець |
| `criteria` | tuple[`CriterionHit`, …] | **непорожній** (SC-002), упорядкований за `(criterion, detail)`; до 6 хітів (два незалежні джерела `known_list`) |
| `incident_edges` | tuple[`Edge`, …] | усі ребра графа з цією вершиною на будь-якому кінці |
| `measures` | `NodeMeasures` | виміри вершини на момент рішення |
| `config_version` | int | `HubThresholds.version` |
| `lists_version` | int \| None | |

Порядок — за `address`.

### `BuyerFlag` — пояснювальна позначка покупця (FR-002-10)

`(address: str, buyer_rank: int, criteria: tuple[CriterionHit, …] непорожній, measures: NodeMeasures)`. Порядок — за `buyer_rank`.

### `PruneOutcome` — результат `prune_hubs`

```
PruneOutcome(graph: FundingGraph, records: tuple[PruneRecord], buyer_flags: tuple[BuyerFlag],
             lists_applied: bool, config_version: int, lists_version: int | None)
```

Інваріанти: `{r.address for r in records} ∩ {адреси покупців} = ∅`; `graph.nodes` = вхідні вершини мінус `records`; усі покупці входу присутні в `graph` (SC-003); `lists_applied == False` ⇒ жоден `CriterionHit` з `detail` виду `list:*`.

## Звіт ефекту (`src/unmask/hubs/report.py`; FR-002-11)

### `EffectSnapshot`

| Поле | Тип | Правило |
|---|---|---|
| `nodes`, `edges`, `components` | int ≥ 0 | компоненти — слабка зв'язність (R-10); ізольована вершина — компонента |
| `buyers_total` | int ≥ 0 | `wallets_analyzed` |
| `buyers_in_largest_component` | int ≥ 0 | у компоненті з найбільшою кількістю покупців |
| `largest_component_buyer_share` | float | `buyers_in_largest_component / buyers_total`; `0.0` при `buyers_total == 0` |
| `isolated_buyers` | int ≥ 0 | покупці без жодного ребра |

### `EffectReport`

```
EffectReport(before: EffectSnapshot, after: EffectSnapshot, pruned_nodes: int, pruned_edges: int,
             warn_share: float, warnings: tuple[GraphWarning, …])
```

Інваріанти: `after.largest_component_buyer_share > warn_share` ⇔ `giant_component ∈ warnings`; `before.buyers_total == 0` ⇔ `empty_graph ∈ warnings`; `pruned_nodes == before.nodes − after.nodes`. Поля «ok/clean» немає.

## Результат (`src/unmask/graph/model.py`)

### `GraphCompleteness` (FR-002-05, FR-002-19)

| Поле | Тип | Правило |
|---|---|---|
| `status` | властивість | `complete` ⇔ `ingest_status == complete` **і** `delegated_complete`; інакше `incomplete` |
| `ingest_status` | `complete` \| `incomplete` | `IngestResult.completeness.status` |
| `missing` | tuple[`MissingRef`, …] | копії `(wallet, depth, reason, detail)` з 001, у порядку 001 |
| `buyers_complete` | bool | |
| `buyers_reason` | str \| None | |
| `delegated_complete` | bool | з `IngestResult.delegated` |
| `delegated_reason` | str \| None | |

Будується лише через `GraphCompleteness.derive(ingest: IngestResult)` (той самий прийом із токеном, що й у 001).

### `GraphMetadata` (FR-002-13)

| Поле | Тип |
|---|---|
| `mint` | str |
| `schema_version` | str — константа `"002.1"` (контракт `graph-result.schema.json`) |
| `ingest_analyzed_at` | int — з 001 (єдине «часове» поле; не volatile, бо з входу) |
| `ingest_config_version` | int |
| `ingest_source` | str |
| `wallets_analyzed` | int |
| `hub_config_version` | int |
| `address_lists_version` | int \| None |
| `lists_applied` | bool — `address_lists_version is None` ⇔ `lists_applied == False` |
| `thresholds` | `ThresholdsSnapshot` — знімок усіх порогів `hubs.yaml`, крім `version`: `degree_threshold`, `one_off_senders_share`, `one_off_min_senders`, `giant_component_warn_share`, `prune_off_curve`, `prune_ingest_high_degree`, `dust_amount_lamports` (≥ 1), `dust_min_fanout` (≥ 2) — вісім полів з тими самими межами, що й `HubThresholds` (T-055) |
| `nodes_total`, `edges_total` | int — повний граф до відсікання |

### `GraphResult`

```
GraphResult(
  metadata: GraphMetadata,
  completeness: GraphCompleteness,
  graph: FundingGraph,                 # для кластеризації: без хабів, з усіма покупцями
  pruned: tuple[PruneRecord, …],       # хаби з інцидентними ребрами — `graph ∪ pruned` відтворює повну множину адрес, ребер і вимірів (ролі, depth, address_type і unexpanded видаленої вершини в запису не зберігаються)
  buyer_flags: tuple[BuyerFlag, …],
  report: EffectReport,
)
```

Інваріанти: `len(graph.buyers()) == metadata.wallets_analyzed` (SC-003); адреси `pruned` не перетинаються з `graph.nodes`; `metadata.nodes_total == len(graph.nodes) + len(pruned)`; `metadata.lists_applied == False` ⇒ `address_lists_not_applied ∈ report.warnings`.

Серіалізація — `graph/serialize.py::to_dict/to_json` за `contracts/graph-result.schema.json`.

## Зв'язки

```
config/hubs.yaml ─┐
config/hub_addresses.yaml ─┴─ load_hub_config ──▶ HubConfig ──▶ GraphService
                                                                   │
IngestResult (001, з .delegated) ──▶ build_graph ──▶ FundingGraph ─┤ measure (hubs.criteria) ──▶ Node.measures
                                                                   ├ prune_hubs ──▶ PruneOutcome(graph′, records, flags)
                                                                   ├ components(FundingGraph) ──▶ effect_report(before, after)
                                                                   └──▶ GraphResult ──▶ to_dict/to_json ──▶ graph-result.schema.json
```

Node 1 — * Edge (як `sender` або `receiver`); PruneRecord 1 — 1 Node (відсічена); PruneRecord 1 — * Edge (інцидентні); BuyerFlag 1 — 1 Node(buyer); DelegatedLink 1 — 1 Edge(kind=delegated_buy) після агрегації за парою (кілька зв'язків однієї пари — одне ребро з `count > 1`).
