# Implementation Plan: Граф фінансування та відсікання хабів

**Branch**: `002-funding-graph-hub-pruning` (робота в `main`, окрема гілка не створюється) | **Date**: 2026-10-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-funding-graph-hub-pruning/spec.md`

## Summary

З `IngestResult` фічі 001 побудувати орієнтований зважений граф фінансування (вершини — гаманці з ролями й глибиною, ребра — агреговані за `(вид, відправник, отримувач, актив)` з сумою, кількістю, часами й первинними посиланнями), окремим модулем відсікти хаби за чотирма незалежними критеріями (відомі списки/PDA, ступінь, частка одноразових відправників, позначка збору `high_degree`) з пояснювальним записом на кожне відсікання, ніколи не відсікаючи покупців, і видати звіт ефекту (компоненти й частка покупців у найбільшій компоненті до/після з попередженнями). Додатково розширити результат 001 зворотно сумісним полем `delegated` (swap-and-send: «делегована купівля» платник→отримувач і кандидати без пари), яке граф перетворює на окремий вид ребра.

Підхід: два пакети — `unmask.graph` (модель, побудова, виміри, компоненти, сервіс, серіалізація) і `unmask.hubs` (конфіг з двох версіонованих YAML, критерії, відсікання, звіт) — принцип VI буквально: `graph` не знає про хаби, `hubs` працює з готовим графом через його модель. Усі результато-впливові числа й адреси — у `config/hubs.yaml` і `config/hub_addresses.yaml` з версіями й журналом, захищеним дайджестом (SC-008). Результат детермінований побайтово (без годинника), серіалізується за власним контрактом `contracts/graph-result.schema.json` (версія `002.1`). Рішення й їх обґрунтування — [research.md](research.md) R-1…R-21.

## Technical Context

**Language/Version**: Python 3.12 (як у 001)

**Primary Dependencies**: `pyyaml` (конфіг), `solders` (`Pubkey.is_on_curve` для вершин-не-покупців); dev — `pytest`, `jsonschema`. **Нових залежностей немає** (`networkx` відкинуто — research R-17)

**Storage**: N/A — чисті функції над об'єктами в пам'яті; жодного стану між викликами

**Testing**: `pytest` через `uv run pytest`; гард мережі з `tests/conftest.py` діє на всі тести; вхід тестів — серіалізовані `IngestResult` (`tests/fixtures/graph/*/ingest.json`) з незалежними еталонами `expected.json` від окремого генератора; для swap-and-send — новий RPC-сценарій `swapsend` у генераторі 001

**Target Platform**: Linux-сервер (той самий процес, що й 001; згодом HTTP-API/бот)

**Project Type**: бібліотека (`src/unmask/graph`, `src/unmask/hubs`, розширення `src/unmask/ingest`), без CLI і HTTP

**Performance Goals**: `GraphService.analyze` + `to_json` для 300 покупців типового результату (~2·10⁴ переказів, хаб із 200 відправниками) < 2 с (SC-007); складність O((V+E)·α) + O(E log E) на сортування

**Constraints**: без мережі; без волатильних полів у результаті (SC-004 — побайтово); усі пороги/списки — YAML з версією й дайджестом у CHANGELOG; покупці ніколи не відсікаються (SC-003); жодна зміна `expected.json` фічі 001 (SC-006); `spec.md` 001 і 002 не змінюються

**Scale/Scope**: ≤ 500 покупців, ≤ 3 рівні, ~10⁴ вершин, ~3·10⁴ ребер; 21 FR, 3 user stories, 26 задач (T-023…T-048)

## Constitution Check

*GATE: перевірено до Phase 0 і повторно після Phase 1 — порушень немає.*

| Принцип | Як виконується | Статус |
|---|---|---|
| I. Трасування | кожна задача `tasks.md` несе `[FR-002-NN]`; розкладка `impl:`/`verifies:` — розділ «Маркери трасування»; `__init__.py` і генератор фікстур — `trace: ignore-file`; файли 001, що змінюються, додають FR-002-ID у шапку | PASS |
| II. Test-First | у кожній задачі названо тест(и), що пишуться першими й червоніють з очікуваної причини; фікстури й генератор — у фазах 1–2; еталони незалежні від коду, що тестується (research R-18); мережі немає | PASS |
| III. Версіонування | пороги й перемикачі — `config/hubs.yaml` (`version`), списки — `config/hub_addresses.yaml` (`version`); обидві версії й знімок порогів — у `metadata` результату; `CHANGELOG.md` із sha256, зміна без запису → червоний тест (SC-008) | PASS |
| IV. Межі модулів | `graph` ↔ `hubs` через модель; `hubs.criteria` / `prune` / `report` / `config` — окремі модулі з окремими тестами; залежність від 001 лише через `IngestResult` (тест забороняє імпорт внутрішніх модулів 001) | PASS |
| V. Доказ і чесна неповнота | кожне ребро — `refs[]` з підписом і слотом; кожне відсікання — критерії з виміряними значеннями й порогами (SC-002); статус повноти графа похідний і не може бути `complete` над неповним збором; `lists_applied=false` → обов'язкове попередження; `delegated` має явну повноту, умовчання — `not_analyzed`, не «порожньо» | PASS |
| VI. Хаби окремо | пакет `unmask.hubs` зі своїм інтерфейсом (`evaluate`, `prune_hubs`, `effect_report`, `load_hub_config`) і тестами; `graph.build` не містить жодної гілки про хаби; у результаті видно, що відсічено й чому (`pruned[]`, `buyer_flags[]`) | PASS |
| VII. Лише Solana, на запит | жодної мережі, сховища графа, ML, моніторингу | PASS |
| Ролі | `critical:` → `implementer-senior`; решта → `implementer` (розділ у `tasks.md`) | PASS |

Re-check після Phase 1: data-model і контракти не додали зовнішніх залежностей, сховищ чи обходу інтерфейсу. Складність, що потребує обґрунтування: немає.

## Project Structure

### Documentation (this feature)

```text
specs/002-funding-graph-hub-pruning/
├── spec.md                           # FR-002-01..21 (не змінюється)
├── plan.md                           # цей файл
├── research.md                       # R-1…R-21
├── data-model.md                     # сутності графа, відсікання, звіту, результату; розширення 001
├── quickstart.md                     # як переконатися, що фіча працює
├── contracts/
│   ├── graph-service.md              # публічний інтерфейс: GraphService, build_graph, measures, components, criteria, prune, report, serialize
│   ├── config-hubs.md                # config/hubs.yaml + config/hub_addresses.yaml + формат CHANGELOG із sha256
│   ├── graph-result.schema.json      # JSON Schema 2020-12 результату графа (schema 002.1), інваріанти в if/then
│   └── ingest-delegated-extension.md # єдина зміна контракту 001: schema 1.1, $defs/delegated, гарантії SC-006
├── checklists/requirements.md
└── tasks.md                          # T-023…T-048
```

### Source Code (repository root)

```text
config/
├── ingest.yaml                       # 001, без змін
├── hubs.yaml                         # НОВЕ: пороги й перемикачі, version=1
├── hub_addresses.yaml                # НОВЕ: списки адрес за категоріями, version=1
└── CHANGELOG.md                      # реструктуровано: розділ на файл; записи 002 з sha256
src/unmask/
├── ingest/                           # 001 — зміни лише для swap-and-send (research R-3)
│   ├── model.py                      # + DelegatedLink, UnpairedCandidate, DelegatedAnalysis; IngestResult.delegated
│   ├── delegated.py                  # НОВЕ: detect_delegated(parsed, mint) — правило R-2
│   ├── buyers.py                     # + виклик detect_delegated у _record_first; скидання стану
│   ├── collector.py                  # + CollectionState.delegated_by_signature; DelegatedAnalysis.derive у collect
│   ├── cache.py                      # + рядок _FIELD_COPY
│   ├── service.py                    # + delegated у _unverified
│   └── serialize.py                  # + ключ "delegated"
├── graph/
│   ├── __init__.py                   # trace: ignore-file
│   ├── model.py                      # Node, Edge, EdgeRef, FundingGraph, NodeMeasures, UnexpandedMark, GraphCompleteness, GraphMetadata, GraphResult, enum'и, ключі порядку, GraphInputError
│   ├── build.py                      # build_graph(IngestResult) -> FundingGraph (вершини, ребра обох видів, глибина, off-curve)
│   ├── measures.py                   # compute(nodes, edges) -> {address: NodeMeasures}
│   ├── components.py                 # union-find; Components
│   ├── service.py                    # GraphService.analyze
│   └── serialize.py                  # to_dict / to_json за graph-result.schema.json
└── hubs/
    ├── __init__.py                   # trace: ignore-file
    ├── config.py                     # load_hub_config, HubThresholds, AddressLists, HubConfig, content_digest, changelog_entries, ConfigError
    ├── criteria.py                   # CriterionHit; evaluate(node, config, ingest_counterparty_threshold)
    ├── prune.py                      # PruneRecord, BuyerFlag, PruneOutcome; prune_hubs
    └── report.py                     # EffectSnapshot, EffectReport, GraphWarning; effect_report

tests/
├── fixtures/
│   ├── build_fixtures.py             # 001; + сценарій swapsend (Launch.schedule_buy_for), expected.delegated
│   ├── scenarios/swapsend/{rpc.json, expected.json}        # НОВЕ (001-форма)
│   ├── build_graph_fixtures.py       # НОВЕ, trace: ignore-file; не імпортує unmask; еталони-оракули
│   └── graph/<scenario>/{ingest.json, expected.json}      # g_basic, g_hub, g_known, g_buyer_hub, g_incomplete, g_empty, g_all_hubs, g_unexpanded, g_delegated
├── test_hubs_config.py               # verifies: FR-002-08, FR-002-13, FR-002-14
├── test_hubs_changelog_guard.py      # verifies: FR-002-08
├── test_graph_model.py               # verifies: FR-002-02, FR-002-03, FR-002-05, FR-002-06, FR-002-18
├── test_graph_fixture_builder.py     # verifies: FR-002-20
├── test_graph_components.py          # verifies: FR-002-11
├── test_graph_build.py               # verifies: FR-002-01, FR-002-02, FR-002-18
├── test_graph_build_nodes.py         # verifies: FR-002-03
├── test_graph_completeness.py        # verifies: FR-002-05, FR-002-06
├── test_graph_determinism.py         # verifies: FR-002-04
├── test_hubs_measures.py             # verifies: FR-002-07
├── test_hubs_threshold_rule.py       # verifies: FR-002-07, FR-002-14
├── test_hubs_criteria_lists.py       # verifies: FR-002-07
├── test_hubs_prune.py                # verifies: FR-002-09, FR-002-10
├── test_hubs_report.py               # verifies: FR-002-11
├── test_hubs_lists_unavailable.py    # verifies: FR-002-12
├── test_hubs_edge_cases.py           # verifies: FR-002-09, FR-002-11
├── test_graph_service.py             # verifies: FR-002-13, FR-002-20
├── test_graph_serialize_contract.py  # verifies: FR-002-04, FR-002-21
├── test_graph_integration_001.py     # verifies: FR-002-20
├── test_graph_performance.py         # verifies: FR-002-11
├── test_delegated_fixtures.py        # verifies: FR-002-15, FR-002-16
├── test_ingest_delegated_model.py    # verifies: FR-002-19, FR-002-21
├── test_delegated_rule.py            # verifies: FR-002-15, FR-002-16
├── test_delegated_wiring.py          # verifies: FR-002-17, FR-002-19
├── test_graph_delegated_edges.py     # verifies: FR-002-18
└── test_graph_delegated_contract.py  # verifies: FR-002-19, FR-002-21
```

**Structure Decision**: src-layout, два нові пакети в спільному корені `unmask`. Поділ збігається з межами тестування: кожен файл у `src/unmask/graph/` і `src/unmask/hubs/` має щонайменше один тестовий файл-власник. `hubs` імпортує лише `graph.model`; `graph.build`/`measures`/`components` не імпортують `hubs`; `graph.service` — єдиний, що знає про обидва.

## Модулі: що робить · як користуватись · від чого залежить

| Модуль | Що робить | Інтерфейс | Залежить від |
|---|---|---|---|
| `graph.model` | типи графа й результату з інваріантами; похідна повнота | дата-класи; `GraphCompleteness.derive(result)`; `FundingGraph.without(…)` | `ingest.model` (типи 001) |
| `graph.build` | з `IngestResult` — вершини й ребра обох видів, глибина, off-curve | `build_graph(result) -> FundingGraph` | `graph.model`, `graph.measures`, `solders` |
| `graph.measures` | ступінь, відправники, одноразові відправники | `compute(nodes, edges) -> dict[str, NodeMeasures]` | `graph.model` |
| `graph.components` | слабка зв'язність, union-find | `components(graph) -> Components` | `graph.model` |
| `hubs.config` | два YAML, валідація, дайджести, журнал | `load_hub_config(thresholds_path, lists_path)`, `content_digest`, `changelog_entries` | `pyyaml` |
| `hubs.criteria` | чотири критерії, правило порогу | `evaluate(node, config, *, ingest_counterparty_threshold) -> tuple[CriterionHit]` | `graph.model`, `hubs.config` |
| `hubs.prune` | записи відсікання, захист покупців, граф без хабів | `prune_hubs(graph, config, *, ingest_counterparty_threshold) -> PruneOutcome` | `hubs.criteria`, `graph.model` |
| `hubs.report` | знімки до/після, попередження | `effect_report(before, after, config, *, lists_applied, delegated_complete) -> EffectReport` | `graph.components`, `graph.model` |
| `graph.service` | публічний вхід | `GraphService(config).analyze(result) -> GraphResult` | усе вище |
| `graph.serialize` | JSON за контрактом | `to_dict(result)`, `to_json(result)` | `graph.model` |
| `ingest.delegated` (001) | правило делегованої купівлі | `detect_delegated(parsed, mint) -> list[DelegatedLink \| UnpairedCandidate]` | `ingest.parse`, `ingest.model`, `ingest.purchases.spent_sol` |

Правило залежностей: стрілки лише вниз таблицею; `graph.*` (крім `service`) не імпортує `hubs.*`; жоден модуль 002 не імпортує `ingest.rpc`, `ingest.collector`, `ingest.buyers`, `ingest.funding`, `ingest.cache`, `ingest.service` (тест T-039).

## Маркери трасування

| Файл | Шапка |
|---|---|
| `src/unmask/graph/__init__.py`, `src/unmask/hubs/__init__.py`, `tests/fixtures/build_graph_fixtures.py` | `# trace: ignore-file` |
| `graph/model.py` | `# impl: FR-002-02, FR-002-03, FR-002-05, FR-002-06, FR-002-18` |
| `graph/build.py` | `# impl: FR-002-01, FR-002-02, FR-002-03, FR-002-04, FR-002-05, FR-002-06, FR-002-18` |
| `graph/measures.py` | `# impl: FR-002-07` |
| `graph/components.py` | `# impl: FR-002-11` |
| `graph/service.py` | `# impl: FR-002-04, FR-002-13, FR-002-19, FR-002-20, FR-002-21` |
| `graph/serialize.py` | `# impl: FR-002-04, FR-002-13, FR-002-21` |
| `hubs/config.py` | `# impl: FR-002-08, FR-002-12, FR-002-13, FR-002-14` |
| `hubs/criteria.py` | `# impl: FR-002-07, FR-002-14` |
| `hubs/prune.py` | `# impl: FR-002-07, FR-002-09, FR-002-10, FR-002-12` |
| `hubs/report.py` | `# impl: FR-002-11, FR-002-12` |
| `ingest/delegated.py` | `# impl: FR-002-15, FR-002-16, FR-002-19` |
| `ingest/model.py` | до наявного списку **додати** `FR-002-19, FR-002-21` |
| `ingest/serialize.py` | додати `FR-002-20` (T-026, `from_dict`) і `FR-002-21` (T-044) |
| `ingest/buyers.py` | додати `FR-002-17` |
| `ingest/collector.py` | додати `FR-002-19` |
| `ingest/cache.py` | додати `FR-002-17` |
| `ingest/service.py` | додати `FR-002-19` |
| кожен `tests/test_*.py` | `# verifies: FR-002-NN[, …]` — за таблицею в «Source Code» |

`config/*.yaml`, `*.json` маркерів не потребують (`config/` не в `IMPL_ROOTS`; JSON не сканується).

## Межі фічі й що відкладено

- **Входить**: FR-002-01…21; два YAML з журналом; фікстури 002 і сценарій `swapsend` 001; зворотно сумісне розширення контракту 001 (schema 1.1).
- **Не входить**: кластеризація (union-find за спільним джерелом, Louvain — фіча 003), поведінкові сигнали, оцінка ризику, API, бот, рендер графа, калібрування порогів і заповнення списків бірж на реальних даних (потребує RPC-ключа; процедура — research R-12), зміна правила купівлі 001 для swap-and-send-отримувачів (їх **не** додають у перші N — FR-002-17).
- Жодна задача не тягне в «Поза межами» PRD.

## Відкриті питання (не BLOCKED — архітектура однакова за будь-якої відповіді; змінюється одна задача)

| # | Питання | Обрано в плані | Якщо відповідь інша |
|---|---|---|---|
| Q1 | SC-006 «жоден байт результату 001»: еталони `expected.json` незмінні і `to_dict` набуває одного нового ключа `delegated` (research R-3) — чи буквально `to_json` побайтово (емітувати `delegated` лише коли непорожнє/неповне)? | завжди емітувати (принцип V: «не аналізовано» ≠ «порожньо») | змінити `_delegated()` у `serialize.py` і тест T-044; схема без змін |
| Q2 | Покупець-PDA (пул/крива, що «купив» при продажу користувача, known-issues 001 §2): FR-002-10 буквально — лишається з позначкою `buyer_flags` | не відсікати, позначати | дозволити відсікання покупців з `address_type=off_curve` — зміна в `prune_hubs` (T-035) і SC-003 потребує уточнення у spec власником |
| Q3 | «Частка унікальних контрагентів» = частка одноразових **відправників** серед унікальних відправників із передумовою ≥ 10 (research R-8) | так | інша формула — лише `graph/measures.py` + `hubs/criteria.py` (T-032, T-033) і еталон генератора |
| Q4 | Вікно swap-and-send = транзакції mint, розібрані перелічувачем покупців (до слота N-го покупця включно), без додаткових звернень (research R-1) | так | окреме перегортання — мережа, нова FR; поза цією фічею |
| Q5 | `depth` вершин лише з делегованих ребер: отримувач 0, платник 1 (research R-6) | так | `null` — зміна `graph/model.py`/`build.py` (T-029, T-047) і схеми |
| Q6 | Два YAML (пороги / списки) з окремими версіями (research R-13) | так | один файл з двома полями версій — лише `hubs/config.py` (T-023) |

## Complexity Tracking

Порушень Constitution Check немає — таблиця не заповнюється.
