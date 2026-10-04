# Contract: публічний інтерфейс фічі 002 — `GraphService` і модулі

Споживачі — кластеризація (фіча 003), згодом HTTP-API/бот. Усе, що вони знають про граф і відсікання, — цей контракт, `data-model.md` і `graph-result.schema.json`.

Залежність від фічі 001 — **лише** через `unmask.ingest.model.IngestResult` (об'єкт, отриманий від `IngestService.collect`) і, для фікстур, через серіалізований JSON за `specs/001-onchain-data-ingest/contracts/ingest-result.schema.json` (1.1). Жоден модуль 002 не імпортує `unmask.ingest.rpc`, `collector`, `buyers`, `funding`, `cache`, `service` (перевіряється тестом).

## 1. `unmask.hubs.config` — конфігурація (принцип III)

```python
load_hub_config(thresholds_path: Path, lists_path: Path | None) -> HubConfig
content_digest(path: Path) -> str                       # sha256 канонічного вмісту YAML (research R-14)
changelog_entries(changelog: Path, file_name: str) -> dict[int, str]   # {version: sha256} із розділу "# config/<file_name>"
```

- `thresholds_path` обов'язковий: відсутній файл, невідоме/відсутнє поле, значення поза межами → `ConfigError(field)`.
- `lists_path=None`, відсутній або нечитабельний файл → `HubConfig.lists = None` (списки «не застосовано», FR-002-12). Файл присутній, але невалідний (не base58, дубль у двох категоріях, невідома категорія, відсутній `version`) → `ConfigError`.
- Нічого не кешується; функція чиста відносно файлової системи.

## 2. `unmask.graph.build` — побудова графа (US1)

```python
build_graph(result: IngestResult) -> FundingGraph
```

- Вершини: покупці ∪ відправники переказів ∪ платники/отримувачі `result.delegated.links`. Ролі, глибина, `buyer_rank`, `address_type`, `unexpanded` — за `data-model.md` (research R-6).
- Ребра: агрегація за `(kind, sender, receiver, asset)` (research R-5); `delegated_buy` — з `result.delegated.links`, окремий вид.
- `measures` для кожної вершини обчислює `graph.measures.compute(nodes, edges) -> dict[address, NodeMeasures]`, який `build_graph` викликає наприкінці. Визначення вимірів (R-7, R-8) належать графу — це властивості структури; пороги й рішення «хаб/не хаб» — пакету `hubs`. Так `graph` не залежить від `hubs`, а `hubs.criteria` працює лише з готовими числами вершини.
- Детермінізм: результат — функція лише вмісту `result`, не порядку його кортежів (FR-002-04).
- Виняток `GraphInputError` лише на порушення контракту 001 (самопереказ, дубль `(signature, instruction_path)`, покупець без `rank`, розбіжність `decimals` одного активу) — дефект, не дані.
- Порожній `result` (0 покупців) → `FundingGraph((), ())`.

## 2a. `unmask.graph.measures` — виміри вершин (research R-7, R-8)

```python
compute(nodes: Iterable[Node], edges: Iterable[Edge]) -> dict[str, NodeMeasures]
```

`degree` — унікальні контрагенти (вхідні ∪ вихідні, всі `kind` і активи); `unique_senders` — унікальні відправники вхідних ребер `kind=transfer`; `one_off_senders` — з них із сумарним `count == 1` по всіх активах; `one_off_share = one_off_senders / unique_senders` або `None`. Чиста функція; порядок входу не впливає.

Виміри пилового роздавання (research R-22, FR-002-22): `buyer_fanout` — кількість різних вершин з роллю `buyer`, до яких є ребро `(transfer, v, b, sol)` (SPL і `delegated_buy` не рахуються); `median_to_buyers` — верхня медіана `Edge.amount` цих ребер (одне значення на покупця): `sorted(amounts)[len(amounts) // 2]`, ціле в лампортах; `None` ⇔ `buyer_fanout == 0`. Роль `buyer` береться з вершин, переданих у `compute` (тому `build_graph` формує вершини з ролями до виклику `compute`).

## 3. `unmask.graph.components` — компоненти

```python
components(graph: FundingGraph) -> Components
Components.count -> int
Components.of(address) -> str            # ідентифікатор компоненти = мінімальна адреса в ній
Components.members(component_id) -> tuple[str, …]
Components.buyers_by_component() -> dict[str, int]
```

Слабка зв'язність по всіх ребрах (обох видів). Union-find; порядок обходу — за адресою; результат не залежить від порядку ребер.

## 4. `unmask.hubs.criteria` — критерії (принцип VI; окремий модуль з окремими тестами)

```python
evaluate(node: Node, config: HubConfig, *, ingest_counterparty_threshold: int) -> tuple[CriterionHit, …]
```

Повертає усі спрацьовані критерії для вершини (порожній кортеж — не хаб), незалежно від того, чи вершина покупець (рішення «не відсікати покупця» — у `prune`). Правила:

| Критерій | Умова спрацювання | `measured` / `threshold` / `detail` |
|---|---|---|
| `known_list` (список) | `config.lists` не `None` і `node.address ∈ lists.index` | `None` / `None` / `list:<category>`, `lists_version` |
| `known_list` (PDA) | `config.thresholds.prune_off_curve` і `node.address_type == off_curve` | `None` / `None` / `address_type:off_curve` |
| `degree` | `node.measures.degree > degree_threshold` | `degree` / `degree_threshold` / `measured` |
| `one_off_senders` | `unique_senders >= one_off_min_senders` **і** `one_off_share > one_off_senders_share` | `one_off_share` / `one_off_senders_share` / `measured` |
| `ingest_high_degree` | `prune_ingest_high_degree` і `node.unexpanded.reason == high_degree` | `counterparties_seen` / `ingest_counterparty_threshold` / `unexpanded:high_degree` |
| `dust_fanout` (FR-002-22, R-22) | `buyer_fanout >= dust_min_fanout` **і** `median_to_buyers < dust_amount_lamports` | `median_to_buyers` (int, лампорти) / `dust_amount_lamports` / `measured`; передумова видима через `measures.buyer_fanout` запису |

Правило порогу (FR-002-14): **рівно поріг ніколи не спрацьовує, нерівність строга**. Напрямок — властивість критерію: `degree`, `one_off_senders` — строго більше (багато — хаб); `dust_fanout` — строго менше (мало — пил). `one_off_min_senders` і `dust_min_fanout` — передумови (включно). `signature_cap` критерієм не є. Хіти впорядковані за рядком `criterion`: `degree < dust_fanout < ingest_high_degree < known_list < one_off_senders`; вершина може мати 1…5 хітів.

## 5. `unmask.hubs.prune` — відсікання (FR-002-09, FR-002-10)

```python
prune_hubs(graph: FundingGraph, config: HubConfig, *, ingest_counterparty_threshold: int) -> PruneOutcome
```

- Для кожної вершини — `criteria.evaluate`. Не покупець із ≥ 1 спрацюванням → `PruneRecord` (адреса, критерії, інцидентні ребра, виміри, версії); покупець із ≥ 1 спрацюванням → `BuyerFlag`, вершина лишається.
- `outcome.graph = graph.without({адреси records})` — без хабів і всіх інцидентних ребер; усі покупці присутні (SC-003).
- `lists_applied = config.lists is not None`; `lists_version = config.lists.version` або `None`.
- Чиста функція: вхідний граф не змінюється; той самий вхід → побайтово той самий вихід.

## 6. `unmask.hubs.report` — звіт ефекту (FR-002-11, FR-002-12)

```python
effect_report(before: FundingGraph, after: FundingGraph, config: HubConfig, *, lists_applied: bool,
              delegated_complete: bool) -> EffectReport
```

Знімки до/після (`components`), попередження: `giant_component` (частка після > `giant_component_warn_share`), `empty_graph` (0 покупців), `all_sources_pruned` (у `after` немає вершин без ролі `buyer`, а в `before` були), `address_lists_not_applied` (`lists_applied == False`), `delegated_incomplete`. Жодного поля «чисто».

## 7. `unmask.graph.service` — публічний вхід

```python
GraphService(config: HubConfig)
GraphService.analyze(result: IngestResult) -> GraphResult
```

Кроки: `build_graph` → `prune_hubs` → `effect_report(before=повний, after=outcome.graph)` → `GraphCompleteness.derive(result)` → `GraphMetadata` (версії обох YAML, знімок усіх восьми порогів `hubs.yaml` включно з `dust_amount_lamports`/`dust_min_fanout`, `ingest_*` з метаданих 001) → `GraphResult`.

Гарантії:
- Не піднімає винятків через дані: будь-який валідний `IngestResult` (повний, неповний, порожній, з `NOT_ANALYZED`) дає `GraphResult`. Винятки — лише `GraphInputError` (порушення контракту 001) і `ConfigError` (при завантаженні, до створення сервісу).
- Без годинника, мережі, файлів, стану між викликами: `analyze(r)` двічі → `to_json` побайтово однаковий (SC-004).
- `Rejection` на вхід не приймається (`TypeError`): граф будується лише з результату збору; відмову споживач обробляє сам.

## 8. `unmask.graph.serialize`

```python
to_dict(result: GraphResult) -> dict     # валідний проти contracts/graph-result.schema.json
to_json(result: GraphResult) -> str      # sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
```

Відображення явне, поле за полем (не `asdict`): `frozenset` ролей → відсортований список; `StrEnum` → рядок; `Asset` → `str`; `None` → `null` лише там, де схема дозволяє; порядок елементів — канонічний порядок моделі. Невідомий тип → `TypeError`.

## 8a. `unmask.ingest.serialize.from_dict` (додається цією фічею, 001)

```python
from_dict(data: dict) -> IngestOutcome      # обернене до to_dict: from_dict(to_dict(o)) == o
```

Читання контракту 001 (schema 1.1) назад у типи: документ без ключа `delegated` → `IngestResult.delegated = DelegatedAnalysis.NOT_ANALYZED` (чесно: аналізу не було). Невалідна форма → `ValueError`/`TypeError` гучно (валідність проти схеми перевіряють тести, не ця функція). Це єдиний шлях, яким фікстури 002 (`tests/fixtures/graph/*/ingest.json`) стають `IngestResult`.

## 9. Що фіча не робить

Не кластеризує, не оцінює ризик, не рендерить, не читає конфіг і фікстури сама (отримує `HubConfig` і `IngestResult`), не ходить у мережу, не зберігає нічого між викликами, не змінює `IngestResult`.
