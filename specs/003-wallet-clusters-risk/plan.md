# Implementation Plan: Кластери пов'язаних гаманців, докази й оцінка ризику токена

**Branch**: `003-wallet-clusters-risk` (робота в `main`, окрема гілка не створюється) | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-wallet-clusters-risk/spec.md`

## Summary

З `GraphResult` фічі 002 (граф без хабів, усі покупці, записи відсікання, позначки покупців, повнота) і `IngestResult` фічі 001 (суми купівель, `spent`, слоти) побудувати кластери ранніх покупців: прохід 1 — union-find за зв'язками «спільне джерело у вікні-ланцюгу», «прямий переказ», «делегована купівля» (і «відновлене ребро» US6); прохід 2 — непрямий зв'язок через спільне джерело на глибині 2 з повним переліком проміжних вершин; поведінкові докази «однакові суми» і «купівлі в одному слоті» лише підвищують впевненість і не утворюють кластера; впевненість — noisy-OR ваг доказів з YAML; `risk_score = ⌊100·Σ share·confidence + 0.5⌋` зі смугами PRD; смуга «чисто» неможлива на неповному вході. Усі пороги й ваги — `config/clusters.yaml` v1 із записом у `config/CHANGELOG.md` (розділ між `hub_addresses` і `ingest`, щоб `ingest` лишався останнім). Оцінювальна команда без мережі прогоняє 9 збережених результатів збору (комітуються як фікстури, 1,14 МБ) і звіряє таблицю з еталоном у репозиторії.

Ключові знахідки read-only аналізу збережених результатів (research R-3, R-4, R-12): (1) у кожному токені бондинг-крива самого токена є «покупцем» з позначкою 002, і платежі покупців у неї без правила «позначені покупці не з'єднують» склеюють чисті токени в «високу концентрацію»; (2) інсайдерські розподільники фінансують гаманці пачками з паузами до 1–2 год, тож вікно — ланцюг за проміжком, не «кілька слотів»; (3) за пропозицією v1 SC-001 досягається 3/5 і 3/4, але на межі (ins1 = 23, cln4 = 25), а смуга «чисто» на цих 9 файлах недосяжна, бо всі зібрані без аналізу делегованих купівель (Q1). Рішення — [research.md](research.md) R-1…R-19.

## Technical Context

**Language/Version**: Python 3.12 (як у 001/002)

**Primary Dependencies**: `pyyaml` (конфіг); dev — `pytest`, `jsonschema`, `solders` (лише в генераторі фікстур — справжні адреси/PDA). **Нових залежностей немає**: Louvain як бібліотека не додається (research R-6 — обрано детерміноване правило «спільне джерело на глибині 2»; `networkx`/`python-louvain` відкинуто: недетермінованість, непояснюваність, непотрібність на графах 30–300 покупців)

**Storage**: N/A — чисті функції над об'єктами в пам'яті; стану між викликами немає

**Testing**: `pytest` через `uv run pytest`; гард мережі `tests/conftest.py` діє на всі тести; входи — `tests/fixtures/clusters/<scenario>/{ingest.json, expected.json}` (власний генератор з незалежними оракулами, R-14), фікстури 002 `tests/fixtures/graph/*` (SC-005, US6), реальні результати `tests/fixtures/real/*.json` (FR-003-23); perf — маркер `perf` поза типовим прогоном (R-17)

**Target Platform**: Linux-сервер (той самий процес; згодом HTTP-API/бот)

**Project Type**: бібліотека `src/unmask/clusters` + оцінювальна команда `python -m unmask.clusters.evaluate` (тонка обгортка `scripts/evaluate_clusters.py`); без HTTP

**Performance Goals**: `ClusterService.analyze` + `to_json` для 300 покупців / ~15 000 ребер < 1 с (SC-006); складність O(E log E + E·degree_threshold) (R-17)

**Constraints**: без мережі; без годинника (час — лише з метаданих 001 через 002); усі результато-впливові числа — YAML з версією й sha256 у журналі (SC-007); модуль не змінює `GraphResult`/`IngestResult` і не імпортує внутрішності 001/002 (FR-003-21, R-16); `spec.md` 001/002/003 не змінюються; `hubs/config.py` не змінюється (R-11)

**Scale/Scope**: ≤ 500 покупців, ≤ 3·10⁴ ребер на вході; 23 FR, 6 user stories, 8 сутностей результату; орієнтовно 25–30 задач (нумерація з T-059 — наступний вільний номер проєкту після T-058 фічі 002)

## Constitution Check

*GATE: перевірено до Phase 0 і повторно після Phase 1 — порушень немає.*

| Принцип | Як виконується | Статус |
|---|---|---|
| I. Трасування | кожна задача `tasks.md` нестиме `[FR-003-NN]`; розкладка `impl:`/`verifies:` — розділ «Маркери трасування»; `__init__.py`, генератор фікстур, обгортка в `scripts/` — `trace: ignore-file`; FR-ID формату `FR-003-NN` сумісні з `scripts/trace.py` без змін (R-19) | PASS |
| II. Test-First | у кожній задачі буде названо тест, що червоніє з очікуваної причини; фікстури з незалежними оракулами (R-14) і реальні результати (R-13) — у фазах 1–2; мережі немає; perf окремо (R-17) | PASS |
| III. Версіонування | усі пороги, ваги, вікно, множники, межі смуг — `config/clusters.yaml` v1 з `version`, записом і sha256 у `config/CHANGELOG.md` (R-11); знімок усіх значень і версії 002/001 — у `metadata` результату (FR-003-18); еталон оцінювання прив'язаний до дайджесту конфігу (R-13); значення v1 — пропозиція, фіксується задачею калібрування із записом | PASS |
| IV. Межі модулів | `clusters` залежить лише від `graph.model`, `ingest.model` і чотирьох публічних функцій `hubs.config` (R-11, R-16); кожен модуль — одна мета, названий інтерфейс, окремий тест; оркестрація 001→002→003 лише в `evaluate.py`; AST-тест меж | PASS |
| V. Доказ і чесна неповнота | кластер без доказів не конструюється (тип і схема); кожен доказ — тип, учасники, `via`, підписи/слоти, вікно, внесок у впевненість (R-7); `risk_score` розкладний (R-9, R-10); неповний вхід → «чисто» неможливе, «підозріло/висока» — нижня оцінка; порожній вхід → `insufficient_data: empty_input`; попередження 002 успадковані; позначені покупці й поведінкові збіги без зв'язку — видимі діагностикою, не приховані (R-3, R-8) | PASS |
| VI. Хаби окремо | 003 не відсікає й не перебудовує граф: споживає `GraphResult`, не імпортує `hubs.criteria/prune/report`; відсічені вершини не з'єднують (крім US6 з окремим типом доказу); позначені покупці не з'єднують за перемикачем у YAML (R-3); правило артефакту FR-003-17 ловить залишки хабів | PASS |
| VII. Лише Solana, на запит | жодної мережі, сховища, ML, моніторингу; Louvain не додається; перезбір реальних токенів — поза фічею (Q1) | PASS |
| Стоп-гейти | контрольна точка 8 жовтня — розділ «Порядок реалізації і ризики»: вертикальний зріз дає осмислений результат на ins4/ins0/ins1 за збереженими даними; гейт критерію успіху — звіт оцінювання показує провали явно (R-12, R-13); пороги «під результат» без запису заборонені — еталон прив'язаний до дайджесту | PASS |
| Ролі | `critical:` → `implementer-senior`; решта → `implementer` (визначить `tasks.md`) | PASS |

Re-check після Phase 1: data-model і контракти не додали зовнішніх залежностей, сховищ чи обходу інтерфейсу. Складність, що потребує обґрунтування: немає (Complexity Tracking порожній).

## Project Structure

### Documentation (this feature)

```text
specs/003-wallet-clusters-risk/
├── spec.md                           # FR-003-01..23 (не змінюється під час імплементації)
├── plan.md                           # цей файл
├── research.md                       # R-1…R-19
├── data-model.md                     # сутності результату, конфіг, інваріанти
├── quickstart.md                     # перевірка без мережі
├── calibration.md                    # З'ЯВИТЬСЯ задачею калібрування: таблиці порогів на 9 результатах, рішення, журнал еталона
├── contracts/
│   ├── cluster-service.md            # ClusterService.analyze, модулі, винятки, оцінювальна команда
│   ├── config-clusters.md            # config/clusters.yaml v1 (дослівно, дайджест) + розділ CHANGELOG
│   └── cluster-result.schema.json    # JSON Schema 2020-12 результату (schema 003.1), парності в allOf
├── checklists/requirements.md
└── tasks.md                          # /speckit-tasks (окремий крок)
```

### Source Code (repository root)

```text
config/
├── clusters.yaml                     # НОВЕ: version=1; усі пороги/ваги/межі (contracts/config-clusters.md)
└── CHANGELOG.md                      # + розділ "# config/clusters.yaml" МІЖ hub_addresses і ingest (ingest лишається останнім)
src/unmask/clusters/
├── __init__.py                       # trace: ignore-file
├── config.py                         # load_cluster_config(path) -> ClusterConfig; імпортує з hubs.config лише ConfigError, content_digest, changelog_entries, check_changelog
├── model.py                          # EvidenceType, TimeBasis, RiskBand, ClusterWarning, DiagnosticKind, Window, Evidence, ClusterMember, Cluster, DiagnosticSignal, ClusterCompleteness, ThresholdsSnapshot, ClusterMetadata, ClusterResult, ClusterInputError, cluster_id, ключі порядку
├── links.py                          # прохід 1: Link'и з GraphResult — shared_funder (вікно-ланцюг), direct_transfer, delegated_buy, recovered_edge (US6); виключення позначених покупців
├── indirect.py                       # прохід 2: indirect_link через спільне джерело на глибині 2
├── cluster.py                        # union-find над покупцями; кластери з доказами-зв'язками; cluster_id
├── behavior.py                       # same_amounts / same_slot у кластерах і діагностичні сигнали токена
├── score.py                          # частка, впевненість (noisy-OR, множники, артефакт), risk_score, смуги, правило неповноти
├── service.py                        # ClusterService(config).analyze(graph_result, ingest_result) -> ClusterResult; перевірка узгодженості входів
├── serialize.py                      # to_dict / to_json за cluster-result.schema.json
└── evaluate.py                       # оцінювальна команда (FR-003-22/23): manifest → 001→002→003 → таблиця; __main__
scripts/
└── evaluate_clusters.py              # тонка обгортка над unmask.clusters.evaluate (trace: ignore-file)

tests/
├── fixtures/
│   ├── build_cluster_fixtures.py     # НОВЕ, trace: ignore-file; не імпортує unmask; реюз оракулів build_graph_fixtures.py; еталони-оракули
│   ├── clusters/<scenario>/{ingest.json, expected.json}   # c_shared, c_direct_flagged, c_delegated, c_below_min, c_diamond, c_two_clusters, c_behavior, c_giant, c_incomplete, c_empty, c_single, c_all_one, c_no_time, c_recovered
│   └── real/                         # НОВЕ: manifest.yaml, result_{ins0..ins4,cln1..cln4}.json (побайтово як зібрано), expected_table.md
├── test_clusters_config.py           # verifies: FR-003-18
├── test_clusters_changelog_guard.py  # verifies: FR-003-18
├── test_clusters_model.py            # verifies: FR-003-07, FR-003-09, FR-003-12
├── test_clusters_fixture_builder.py  # verifies: FR-003-19
├── test_clusters_links.py            # verifies: FR-003-01, FR-003-02, FR-003-03, FR-003-05, FR-003-06
├── test_clusters_window.py           # verifies: FR-003-02
├── test_clusters_union.py            # verifies: FR-003-01, FR-003-12
├── test_clusters_indirect.py         # verifies: FR-003-04, FR-003-06
├── test_clusters_recovered.py        # verifies: FR-003-06
├── test_clusters_behavior.py         # verifies: FR-003-08, FR-003-09, FR-003-10
├── test_clusters_score.py            # verifies: FR-003-11, FR-003-13, FR-003-14, FR-003-17
├── test_clusters_completeness.py     # verifies: FR-003-15, FR-003-16
├── test_clusters_service.py          # verifies: FR-003-19, FR-003-21
├── test_clusters_boundaries.py       # verifies: FR-003-21 (AST імпортів)
├── test_clusters_serialize_contract.py # verifies: FR-003-20, FR-003-18
├── test_clusters_determinism.py      # verifies: FR-003-12, FR-003-20
├── test_clusters_hub_fixtures.py     # verifies: FR-003-06 (SC-005 на g_hub/g_dust/g_dust_mixed/g_financier)
├── test_clusters_evaluate.py         # verifies: FR-003-22, FR-003-23
├── test_clusters_real_fixtures.py    # verifies: FR-003-23 (маніфест, sha256, валідність проти схеми 001, без секретів)
└── test_clusters_performance.py      # verifies: FR-003-01 (SC-006; маркер perf)
```

**Structure Decision**: src-layout, один новий пакет `unmask.clusters` у спільному корені. Поділ збігається з межами тестування: кожен файл пакета має щонайменше один тестовий файл-власник. `evaluate.py` — єдиний модуль, що знає про 001 і 002 як про конвеєр; ядро бачить лише типи `graph.model` і `ingest.model`.

## Модулі: що робить · як користуватись · від чого залежить

| Модуль | Що робить | Інтерфейс | Залежить від |
|---|---|---|---|
| `clusters.config` | читає й валідує `config/clusters.yaml`, дайджест | `load_cluster_config(path) -> ClusterConfig` | `pyyaml`; `hubs.config` — лише `ConfigError`, `content_digest`, `changelog_entries`, `check_changelog` (R-11) |
| `clusters.model` | типи результату з інваріантами; ідентифікатор і порядок кластерів | дата-класи; `cluster_id(wallets)`; `*_sort_key` | `graph.model` (`EdgeRef`, `GraphWarning`), `ingest.model` (переліки) |
| `clusters.links` | зв'язки проходу 1 з графа 002 | `extract_links(graph_result, config) -> tuple[Link, …]` | `clusters.model`, `graph.model` |
| `clusters.indirect` | непрямі зв'язки через спільне джерело глибини 2 | `indirect_links(graph_result, clusters_so_far, config) -> tuple[Link, …]` | `clusters.model`, `graph.model` |
| `clusters.cluster` | union-find, склад кластерів, прив'язка зв'язків як доказів | `form_clusters(buyers, links) -> tuple[ClusterDraft, …]` | `clusters.model` |
| `clusters.behavior` | поведінкові докази кластера й діагностика токена | `behavioral_evidence(cluster, graph_result, ingest_result, config)`, `diagnostics(clusters, graph_result, ingest_result, config)` | `clusters.model`, `graph.model`, `ingest.model` |
| `clusters.score` | частка, впевненість, артефакт, `risk_score`, смуги, правило неповноти | `confidence(evidence, config, *, artifact)`, `assess(clusters, ingest_result, graph_result, config) -> Assessment` | `clusters.model` |
| `clusters.service` | публічний вхід; узгодженість входів | `ClusterService(config).analyze(graph_result, ingest_result) -> ClusterResult` | усе вище |
| `clusters.serialize` | JSON за контрактом | `to_dict(result)`, `to_json(result)` | `clusters.model` |
| `clusters.evaluate` | оцінювання на збережених результатах | `evaluate(fixtures_dir, configs) -> str`; `main(argv)` | `clusters.service/serialize/config`, `graph.service`, `graph.serialize`, `hubs.config.load_hub_config`, `ingest.serialize.from_dict` |

Правило залежностей (тест `test_clusters_boundaries.py`, `ast` усіх файлів `src/unmask/clusters/*`): стрілки лише вниз таблицею; ядро (`config`…`serialize`) імпортує з `unmask` лише `unmask.graph.model`, `unmask.ingest.model`, `unmask.clusters.*` і чотири імені `unmask.hubs.config` (лише в `config.py`); `evaluate.py` додатково `unmask.graph.service`, `unmask.graph.serialize`, `unmask.hubs.config.load_hub_config`, `unmask.ingest.serialize`; заборонено скрізь: `unmask.ingest.{rpc,collector,buyers,funding,cache,service,budget,parse,purchases,delegated,addresses,config}`, `unmask.hubs.{criteria,prune,report}`, `unmask.graph.{build,measures,components}`, `socket`, `httpx`. `pruned[]` і `buyer_flags[]` читаються дак-типізацією (`address`, `incident_edges`, `criteria[].criterion`), як у `graph.model.GraphResult`.

## FR → модуль / артефакт

| FR | Де виконується | Де перевіряється |
|---|---|---|
| FR-003-01 кластери з графа, рівно один або жодний | `cluster.py` (union-find), `links.py` | `test_clusters_union.py`, `test_clusters_links.py`, perf |
| FR-003-02 прохід 1: спільне джерело, вікно, мінімальна сума | `links.py` (групування за вікном-ланцюгом, R-4), `config.py` | `test_clusters_links.py`, `test_clusters_window.py` |
| FR-003-03 прямий переказ і делегована купівля — окремі види | `links.py`, `model.py` (`EvidenceType`) | `test_clusters_links.py` |
| FR-003-04 прохід 2: непрямий зв'язок з проміжними вершинами, нижча впевненість | `indirect.py`; `config.py` (ваги: `indirect_link < shared_funder`) | `test_clusters_indirect.py`, `test_clusters_config.py` |
| FR-003-05 перекази нижче порога — не зв'язок, не доказ | `links.py`, `indirect.py`, `behavior.py` (суми лише ≥ порога) | `test_clusters_links.py` (три точки) |
| FR-003-06 відсічені не з'єднують; US6 відновлені ребра | `links.py` (лише ребра `graph`; `recovered_edge` з `pruned[]`), `indirect.py` | `test_clusters_hub_fixtures.py`, `test_clusters_recovered.py`, `test_clusters_indirect.py` |
| FR-003-07 докази непорожні, з типом, посиланнями, учасниками, вікном | `model.py` (`Evidence`, `Cluster.__post_init__`) | `test_clusters_model.py` |
| FR-003-08 поведінкові докази з підписами й значенням | `behavior.py` | `test_clusters_behavior.py` |
| FR-003-09 поведінкові не утворюють кластер; діагностика токена | `model.py` (інваріант: ≥ 1 доказ-зв'язок), `behavior.py` (`diagnostics`) | `test_clusters_model.py`, `test_clusters_behavior.py` |
| FR-003-10 поріг природного збігу | `behavior.py`, `config.py` | `test_clusters_behavior.py` (три точки) |
| FR-003-11 частка з названим знаменником; впевненість за правилом | `score.py`, `model.py` (`share_numerator/denominator`, `share_denominator_kind`) | `test_clusters_score.py` |
| FR-003-12 сортування, тай-брейк, стабільний id | `model.py` (`cluster_id`, `cluster_sort_key`) | `test_clusters_union.py`, `test_clusters_determinism.py` |
| FR-003-13 `risk_score`, смуги з конфігу | `score.py`, `config.py` | `test_clusters_score.py` |
| FR-003-14 число лише з переліком кластерів і доказів | `model.py` (`ClusterResult` несе `clusters` з доказами; `band_reasons` непорожній) | `test_clusters_score.py`, схема |
| FR-003-15 успадкування повноти; «чисто» → `insufficient_data` | `score.py`, `model.py` (`ClusterCompleteness`, інваріанти смуги) | `test_clusters_completeness.py`, схема |
| FR-003-16 порожній вхід → явне «немає даних» | `score.py`, `service.py` | `test_clusters_completeness.py` |
| FR-003-17 артефакт відсікання: попередження і знижена впевненість | `score.py` | `test_clusters_score.py` (`c_giant`) |
| FR-003-18 YAML з версією, журнал, sha256, версії 003+002+001 у результаті | `config.py`, `config/clusters.yaml`, `config/CHANGELOG.md`, `service.py` (метадані) | `test_clusters_config.py`, `test_clusters_changelog_guard.py`, `test_clusters_serialize_contract.py` |
| FR-003-19 лише результати 002 і 001; без мережі | `service.py`, фікстури | `test_clusters_service.py`, `test_clusters_fixture_builder.py`, гард мережі |
| FR-003-20 JSON за власним контрактом, побайтово | `serialize.py`, `contracts/cluster-result.schema.json` | `test_clusters_serialize_contract.py`, `test_clusters_determinism.py` |
| FR-003-21 не змінює 002; залежить лише від публічного результату | правило залежностей, незмінність входів | `test_clusters_boundaries.py`, `test_clusters_service.py` |
| FR-003-22 оцінювальна команда | `evaluate.py`, `scripts/evaluate_clusters.py` | `test_clusters_evaluate.py` |
| FR-003-23 збережені результати як фікстури; еталон поруч | `tests/fixtures/real/*`, `evaluate.py --write` | `test_clusters_real_fixtures.py`, `test_clusters_evaluate.py` |

## Маркери трасування

| Файл | Шапка |
|---|---|
| `src/unmask/clusters/__init__.py`, `tests/fixtures/build_cluster_fixtures.py`, `scripts/evaluate_clusters.py` | `# trace: ignore-file` |
| `clusters/config.py` | `# impl: FR-003-04, FR-003-10, FR-003-13, FR-003-18` |
| `clusters/model.py` | `# impl: FR-003-07, FR-003-09, FR-003-11, FR-003-12, FR-003-14, FR-003-15` |
| `clusters/links.py` | `# impl: FR-003-01, FR-003-02, FR-003-03, FR-003-05, FR-003-06` |
| `clusters/indirect.py` | `# impl: FR-003-04, FR-003-05, FR-003-06` |
| `clusters/cluster.py` | `# impl: FR-003-01, FR-003-12` |
| `clusters/behavior.py` | `# impl: FR-003-08, FR-003-09, FR-003-10` |
| `clusters/score.py` | `# impl: FR-003-11, FR-003-13, FR-003-14, FR-003-15, FR-003-16, FR-003-17` |
| `clusters/service.py` | `# impl: FR-003-16, FR-003-18, FR-003-19, FR-003-21` |
| `clusters/serialize.py` | `# impl: FR-003-18, FR-003-20` |
| `clusters/evaluate.py` | `# impl: FR-003-22, FR-003-23` |
| кожен `tests/test_clusters_*.py` | `# verifies: FR-003-NN[, …]` — за таблицею в «Source Code» |

`config/clusters.yaml`, `tests/fixtures/real/manifest.yaml`, `*.json`, `*.md` маркерів не потребують (`config/` і `tests/` не в `IMPL_ROOTS`; JSON і Markdown не скануються).

## Межі фічі й що відкладено

- **Входить**: FR-003-01…23; `config/clusters.yaml` v1 з журналом; пакет `unmask.clusters`; фікстури `c_*` з оракулами; реальні фікстури 9 токенів і еталон оцінювання; задача калібрування з `calibration.md` 003; perf-тест SC-006.
- **Не входить** (з причиною): HTTP-ендпоінт, бот, PNG, кеш, розгортання (наступні фічі); **перезбір 9 токенів новим кодом 001** (мережа — Q1, ручний крок власника); **час створення адрес** (немає в даних 001; R-8); **однакова послідовність дій** (`Buyer.programs` є, але FR-003-08 не вимагає, калібрувальних підстав немає; R-8); **SPL-ребра як зв'язок** (немає порога й даних; `link_assets` розширюється версією конфігу; R-2); **Louvain** (R-6); **рефакторинг спільного ядра завантажувача конфігів** (Q7); **заповнення `hub_addresses.yaml` пиловими адресами pump.fun** (R-18 — нотатка фічі 002); частка від загальної пропозиції (Assumption spec — API-фіча).
- Жодна задача не тягне в «Поза межами» PRD.

## Порядок реалізації і ризики (контрольна точка PRD — 8 жовтня; сьогодні 5 жовтня)

**Передумова**: T-058 фічі 002 (`hubs.yaml` v3, `one_off_min_senders=50`) має бути злитий до оцінювання 003 на реальних даних — з v2 головний фінансист ins1 відсікається і SC-001 падає до 2/5 (R-12 п. 5). Фікстури `c_*` від цього не залежать (їхні `expected.config` — власні).

**Мінімальний вертикальний зріз (P1, показати першим, ціль — вечір 7 жовтня)**:

1. `config/clusters.yaml` v1 + розділ журналу + `clusters/config.py` з гардом (без цього жодне число не має права з'явитись у коді).
2. `clusters/model.py` з інваріантами (кластер без доказу-зв'язку не існує; `insufficient_data` при неповноті).
3. Генератор `c_*` + `c_shared`, `c_direct_flagged`, `c_delegated`, `c_two_clusters`, `c_incomplete`, `c_empty` (решта — у P2).
4. `links.py` (shared_funder з вікном-ланцюгом, direct_transfer, delegated_buy; позначені не з'єднують) → `cluster.py` → `score.py` (частка, noisy-OR **лише** з доказів-зв'язків, `risk_score`, смуги, правило неповноти) → `serialize.py` → `service.py`.
5. Реальні фікстури + `evaluate.py` + перший еталон. **Критерій контрольної точки**: на ins4 — кластер з 14 гаманців (частка 0,75, джерело `DhLPHfDo`, 15 переказів по 40–100 SOL з підписами, `risk_score` ≈ 54 → `high_concentration`), на ins0 — кластер з 8 (0,44), на ins1 — 15 (0,34); на cln1 — жодного кластера. Це «осмислений результат хоча б на одному токені з відомою історією» — гейт пройдено скриптом без фронта, як і передбачає PRD.

**P2 (8–9 жовтня)**: `behavior.py` (поведінкові докази й діагностика), `indirect.py` (прохід 2), правило артефакту, `recovered_edge` (US6, `c_recovered`), решта фікстур (`c_below_min`, `c_diamond`, `c_behavior`, `c_giant`, `c_single`, `c_all_one`, `c_no_time`), SC-005 на фікстурах 002, AST-тест меж, детермінізм з перестановками, контрактні тести схеми, perf (SC-006), **задача калібрування** (прогін варіантів R-12 через реалізацію, фіксація v1 чи v2 конфігу із записом, перегенерація `expected_table.md`, `calibration.md` 003).

**Ризики**:

| Ризик | Що робимо |
|---|---|
| T-058 не злито → ins1 без кластера, SC-001 2/5 | еталон оцінювання фіксує `hub_config_version` у заголовку; до злиття T-058 еталон не генерується; порядок задач явно ставить залежність |
| SC-001 на межі (ins1 = 23, cln4 = 25): калібрування спокусить «підкрутити» вікно до 600 с (дає 4/4 чистих) | заборонено гейтом критерію успіху без запису; задача калібрування зобов'язана навести таблицю варіантів (як R-12) і обґрунтувати вибір не міткою, а механізмом (пачки з паузами до 1005 с у ins0 — реальна поведінка розподільника) |
| Смуга `clean` недосяжна на збережених даних (усі `incomplete`) — демо виглядає «усе недостатньо даних» | Q1: оцінювач показує `risk_score`, `band` і колонку «смуга за повних даних»; перезбір cln1–cln3 новим 001 — ручний крок до демо |
| Хибні злиття проходом 2 через невідсічений хаб | нижча вага, поріг суми, правило артефакту; на 9 токенах прохід 2 нічого не склеїв у чистих |
| Урок T-042: оцінка часу чистого Python хибна | perf-тест міряє лише 003-стадію, під маркером, на вільній машині; об'єктів у результаті на порядок менше, ніж у графі |
| Генератор 003 імпортує генератор 002 — зміна `build_graph_fixtures.py` ламає `c_*` | `--check` обох генераторів у тестах; `c_*` не залежать від комітованих файлів 002, лише від оракулів-функцій |
| Поведінкові докази з точною рівністю сум пропускають «майже однакові» суми | свідомо (R-8); діагностика токена показує найчастіші суми, калібрування може ввести допуск версією конфігу |

## Відкриті питання (не BLOCKED — архітектура однакова за будь-якої відповіді; змінюється одна-дві задачі)

| # | Питання | Обрано в плані | Якщо відповідь інша (ціна) |
|---|---|---|---|
| **Q1** | **SC-001 проти FR-003-15 на 9 збережених результатах**: усі зібрані до T-044/T-046 (без `delegated` → `NOT_ANALYZED` → граф `incomplete`), cln4 ще й `missing: 3` на рівні 001. «Чисто» заборонене на всіх дев'яти, тож колонка «смуга» для чистих завжди `insufficient_data`. Варіанти: **(а)** оцінювач виводить `risk_score`, `band` і окрему колонку `band_if_complete` (смуга, яку дав би той самий `risk_score` на повних даних) з явним застереженням у заголовку таблиці; SC-001 вимірюється на `risk_score`, як і сформульовано, бібліотека не змінюється; **(б)** перезібрати cln1–cln3 (і за бажання всі 9) новим кодом 001 через Helius вручну (~8 хв на 9 токенів за `run_all.log`), закомітити як фікстури замість старих; cln4 лишиться неповним (`corrupt_data`); **(в)** послабити FR-003-15 для `delegated_incomplete` — зміна вимоги, рішення власника | **(а)** зараз; **(б)** — рекомендований ручний крок власника до демо (поза фічею, потребує ключа RPC); **(в)** не рекомендовано: «чисто» на даних без аналізу делегованих купівель — саме той тип хибного «чисто», який принцип V забороняє | (б): задача «реальні фікстури» отримує нові файли й sha256, еталон перегенеровується (одна задача + запис у `calibration.md`); (в): `score.py` і схема (парність «incomplete ⇒ не clean» слабшає до «ingest incomplete ⇒ не clean») — зміна `spec.md` власником |
| **Q2** | **Позначені покупці 002 (`buyer_flags`) не з'єднують** (R-3): spec не передбачив покупця-PDA бондинг-кривої; FR-003-02 каже «джерело може бути й покупцем» | `link_through_flagged_buyers: false`, діагностика `flagged_buyers_excluded` | `true` у YAML (без коду): чисті токени стають «високою концентрацією» (cln3 → 54, cln2 → 36), SC-001 — 1/4; якщо власник хоче виключати лише `off_curve` — перемикач стає списком критеріїв: `config.py`, `links.py`, одна фікстура |
| **Q3** | **Семантика вікна FR-003-02**: ланцюг за проміжком між сусідніми переказами (обрано) чи загальний розмах групи ≤ W | ланцюг, W = 3600 с (R-4) | розмах: функція групування в `links.py`/`indirect.py`, фікстура `c_shared`, калібрування; на 9 токенах ins0 при W = 3600 той самий, при 600 — три уламки |
| Q4 | Знаменник частки — токени проаналізованих покупців (Assumption spec); названий у метаданих `share_denominator_kind` | так | частка від пропозиції — нове поле збору 001 і API-фіча (поза 003) |
| Q5 | Алгоритм проходу 2 — спільне джерело на глибині 2 (R-6), а не Louvain | так | інший алгоритм — лише `indirect.py` + фікстура `c_diamond`; контракт доказу (`via`, `refs`) той самий |
| Q6 | «Їхніх вікон» у FR-003-11 = базис вікна (секунди/слоти → множник), не «тісніше — надійніше» | так (R-9) | множник від розмаху: `score.py` + поле конфігу + фікстура; калібрувальної підстави немає |
| Q7 | Завантажувач: імпорт чотирьох публічних функцій `hubs.config` (R-11) без рефакторингу | так | рефакторинг спільного ядра `unmask/configio.py`: окрема задача після 003; ризик — монкіпатчі в `tests/test_hubs_config_robustness.py` на імена модуля `hubs.config` (`_read_at_most`, `_regular_size`, `_MAX_CHANGELOG_BYTES`) перестануть діяти → переписування тестів 002 |
| Q8 | Порожній вхід — `band = insufficient_data` з причиною `empty_input` (чотири значення смуги, як у Key Entities), не п'ята смуга `no_data` | так | п'ята смуга: `model.py`, схема, `score.py` — одна задача |
| Q9 | SPL-ребра не є зв'язком у v1 (`link_assets: [sol]`) | так (R-2) | додати `spl:<mint>` стейблкоїна — версія конфігу; поріг суми для SPL потребує окремого поля з `decimals` — `config.py`, `links.py` |
| Q10 | `risk_score` — ціле з округленням «половина вгору»; смуга з цілого | так (R-10) | число з двома знаками: `score.py`, схема, еталон |
| Q11 | Непрямий доказ додається лише коли з'єднує різні кластери/одинаків проходу 1 (R-6) | так | завжди додавати як доказ: `indirect.py`; впевненість кластерів з прямим фінансуванням зросте на ~0,1 — калібрування |
| Q12 | Відновлені ребра US6: позначені покупці виключаються і тут; вікно те саме | так (R-18) | інший поріг для відновлених — поле конфігу |

**Рішення власника (2026-10-05):** Q1 — прийнято (а) (оцінювач показує `risk_score`, `band`, `band_if_complete`); Q2 — прийнято `link_through_flagged_buyers: false`; Q3 — прийнято вікно-ланцюг, W = 3600 с. Q4–Q12 — умовчання плану. Деталі й задачі — `tasks.md`, розділ «Рішення власника».

## Complexity Tracking

Порушень Constitution Check немає — таблиця не заповнюється.
