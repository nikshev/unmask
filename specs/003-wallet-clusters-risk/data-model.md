# Data Model: Кластери пов'язаних гаманців, докази й оцінка ризику токена (003)

Усі сутності — незмінні Python-дата-класи (`frozen=True`), що перевіряють себе при побудові (некоректний стан — `TypeError`/`ValueError` гучно, стиль `graph/model.py` 002). Суми — цілі в лампортах / базових одиницях; частки й впевненості — `float`, округлені до 4 знаків, з цілими чисельником і знаменником поруч. Адреси — base58-рядки. Порядок кожного кортежу визначений ключем лише з даних (детермінізм, FR-003-12, SC-003).

Розташування: `src/unmask/clusters/model.py` (результат), `src/unmask/clusters/config.py` (конфігурація). Повторно використані типи 002: `graph.model.EdgeRef` (первинне посилання), `graph.model.GraphWarning` (успадковані попередження), `graph.model.GraphCompletenessStatus`.

## Перелічення

| Тип | Значення | Зміст |
|---|---|---|
| `EvidenceType` | `shared_funder` \| `direct_transfer` \| `delegated_buy` \| `recovered_edge` \| `indirect_link` \| `same_amounts` \| `same_slot` | типи доказів spec (Key Entities); перші п'ять — **зв'язки** (утворюють кластер), два останні — **поведінкові** (лише впевненість; FR-003-09) |
| `TimeBasis` | `block_time` \| `slot` | у чому виміряне вікно доказу: секунди за `block_time` чи слоти (запасний варіант, research R-4) |
| `RiskBand` | `clean` \| `suspicious` \| `high_concentration` \| `insufficient_data` | смуга оцінки (FR-003-13, FR-003-15, FR-003-16) |
| `ClusterWarning` | `possible_pruning_artifact` \| `slot_time_fallback` | попередження кластера: артефакт відсікання (FR-003-17); хоч один доказ-зв'язок виміряно в слотах |
| `DiagnosticKind` | `same_amounts_unlinked` \| `same_slot_unlinked` \| `flagged_buyers_excluded` | діагностичні сигнали токена (FR-003-09, research R-3, R-8) |
| `BandReason` (рядки) | `no_clusters_on_complete_data` \| `clusters_within_clean_threshold` \| `clusters_share_weighted` \| `empty_input` \| `ingest_incomplete` \| `delegated_incomplete` \| `missing_histories:<n>` | чому саме така смуга (FR-003-14, FR-003-15, FR-003-16); кортеж непорожній завжди |

Константи: `LINK_TYPES = {shared_funder, direct_transfer, delegated_buy, recovered_edge, indirect_link}`, `BEHAVIORAL_TYPES = {same_amounts, same_slot}`; `CLUSTER_SCHEMA_VERSION = "003.1"`; `SHARE_DENOMINATOR_KIND = "analyzed_buyers_received_amount"`.

## Конфігурація (`src/unmask/clusters/config.py`; принцип III; контракт `config-clusters.md`)

### `ClusterConfig` — з `config/clusters.yaml`

| Поле | Тип | Обмеження | v1 (пропозиція; фіксує калібрування) |
|---|---|---|---|
| `version` | int | ≥ 1; підіймається із записом у `config/CHANGELOG.md` (розділ `# config/clusters.yaml`) + `sha256` | 1 |
| `link_min_amount_lamports` | int | ≥ 1; зв'язок iff `amount >= поріг` (рівно поріг — зв'язок; строго менше — ні; FR-003-05) | 10 000 000 |
| `funding_window_seconds` | int | ≥ 1; сусідні перекази джерела з проміжком `<= W` — одна група (research R-4) | 3600 |
| `seconds_per_slot` | float | > 0; `window_slots = floor(funding_window_seconds / seconds_per_slot)`; лише при `block_time=None` | 0.4 |
| `link_through_flagged_buyers` | bool | `false` ⇒ ребра, інцидентні покупцям з `buyer_flags` 002, не є зв'язком (research R-3) | false |
| `link_assets` | tuple[str, …] | непорожній; кожен — `sol` або `spl:<mint>`; без дублів | `[sol]` |
| `indirect_enabled` | bool | прохід 2 (research R-6) | true |
| `same_amount_natural_max` | int | ≥ 1; доказ iff повторів `> natural_max` (строго більше) | 3 |
| `same_slot_natural_max` | int | ≥ 1; доказ iff покупців у вікні слотів `> natural_max` | 5 |
| `same_slot_window_slots` | int | ≥ 0; `0` — рівно один слот | 0 |
| `evidence_weights` | `EvidenceWeights` | по одному полю на кожен `EvidenceType`, `0 < w < 1`; **`indirect_link < shared_funder`** (FR-003-04) | 0.6 / 0.5 / 0.6 / 0.4 / 0.3 / 0.2 / 0.15 |
| `slot_fallback_multiplier` | float | `0 < m <= 1`; вага доказу-зв'язку з `basis=slot` × m | 0.8 |
| `artifact_buyer_share` | float | `0 < x <= 1`; артефакт iff `len(wallets)/wallets_analyzed > x` (строго) **і** (`giant_component` у попередженнях графа **або** непрямих доказів більше за решту доказів-зв'язків) | 0.5 |
| `artifact_confidence_multiplier` | float | `0 < m <= 1` | 0.5 |
| `band_clean_max` | int | 0 ≤ … < `band_suspicious_max`; `risk_score <= band_clean_max` → `clean` | 20 |
| `band_suspicious_max` | int | ≤ 100; `<= band_suspicious_max` → `suspicious`; інакше `high_concentration` | 50 |

Поля без умовчань — усі ключі обов'язкові; відсутнє/невідоме поле чи значення поза межами → `ConfigError(field)` (той самий клас, що в `hubs.config`). `ClusterConfig.digest: str` — `content_digest(path)` (sha256 канонічного вмісту) для журналу й еталона оцінювання.

### `ThresholdsSnapshot` (у метаданих результату)

Усі поля `ClusterConfig`, крім `version` і `digest`, з тими самими межами (конструктор перевіряє — новий поріг без нового поля знімка ловить тест, як T-039 у 002).

## Зв'язок (проміжний тип; `clusters/links.py`, `clusters/indirect.py`)

### `Link`

| Поле | Тип | Правило |
|---|---|---|
| `type` | `EvidenceType` ∈ `LINK_TYPES` | |
| `buyers` | frozenset[str] | ≥ 2 покупців, що з'єднуються; жоден — позначений (якщо `link_through_flagged_buyers=false`) |
| `via` | tuple[str, …] | не-покупці шляху: джерело (`shared_funder`, `recovered_edge`), не-покупець делегованої пари (`delegated_buy`), `(C, M1, M2…)` для `indirect_link`; порожньо для `direct_transfer`; упорядковано |
| `refs` | tuple[`EdgeRef`, …] | непорожній, без дублів, за `ref_sort_key` 002 — усі перекази/транзакції, що утворюють зв'язок |
| `window` | `Window` | `[min, max]` часів (чи слотів) переказів групи |
| `basis` | `TimeBasis` | `slot`, якщо хоч один переказ групи джерела без `block_time` (тоді вся група в слотах) |

`Link` — вхід для union-find і джерело `Evidence` тих самих полів; у результат не серіалізується (лише як `Evidence`).

## Результат (`src/unmask/clusters/model.py`)

### `Window`

`Window(basis: TimeBasis, start: int ≥ 0, end: int ≥ start)` — для `block_time` Unix-секунди, для `slot` номери слотів.

### `Evidence` — доказ (FR-003-07, принцип V)

| Поле | Тип | Правило |
|---|---|---|
| `type` | `EvidenceType` | |
| `wallets` | tuple[str, …] | покупці, яких доказ стосується; ≥ 2 для `LINK_TYPES`, ≥ `natural_max + 1` для поведінкових; усі — члени кластера; упорядковано за адресою |
| `via` | tuple[str, …] | не-покупці шляху (як у `Link`); `indirect_link` ⇒ непорожній (FR-003-04); `direct_transfer`/поведінкові ⇒ порожній |
| `refs` | tuple[`EdgeRef`, …] | непорожній; підпис, слот, `instruction_path` (`None` для купівель і делегованих транзакцій) |
| `window` | `Window` | для поведінкових — у слотах: `same_slot` → `[s, s + same_slot_window_slots]`, `same_amounts` → `[min_slot, max_slot]` посилань |
| `basis` | `TimeBasis` | `== window.basis` |
| `value` | int \| None | `same_amounts` → сума в лампортах; `same_slot` → слот; зв'язки → `None` |
| `detail` | str \| None | `same_amounts` → `funding` \| `first_buy_spent`; інакше `None` |
| `weight` | float | фактичний внесок у noisy-OR після множників: `evidence_weights[type] × (slot_fallback_multiplier if type ∈ LINK_TYPES and basis == slot else 1)`; `0 < weight < 1`; округлено до 4 знаків |

Ключ порядку — `(type, via, wallets, refs[0])`.

### `ClusterMember`

`ClusterMember(wallet: str, buyer_rank: int ≥ 1, received_amount: int ≥ 1)` — порядок за `buyer_rank`.

### `Cluster` (FR-003-01, FR-003-07, FR-003-09, FR-003-11, FR-003-12, FR-003-17)

| Поле | Тип | Правило |
|---|---|---|
| `cluster_id` | str | `"c-" + sha256(",".join(sorted(wallets)))[:16]`; конструктор перевіряє збіг зі складом |
| `members` | tuple[`ClusterMember`, …] | ≥ 2; адреси унікальні; за `buyer_rank` |
| `share_numerator` | int | `Σ members.received_amount` |
| `share_denominator` | int ≥ 1 | `Σ received_amount` усіх проаналізованих покупців (один на результат) |
| `share` | float | `round(numerator / denominator, 4)`; `0 < share <= 1` |
| `evidence` | tuple[`Evidence`, …] | **непорожній**; **≥ 1 доказ з `LINK_TYPES`** (FR-003-09: поведінкові самі кластера не утворюють); кожен `evidence.wallets ⊆ members`; упорядкований |
| `confidence` | float | `round(1 − Π(1 − e.weight), 4)` × `artifact_confidence_multiplier`, якщо `possible_pruning_artifact ∈ warnings`; `0 < confidence < 1`; конструктор перевіряє відповідність формулі (розкладність, принцип V) |
| `warnings` | tuple[`ClusterWarning`, …] | без дублів, за рядком; `slot_time_fallback ∈ warnings` ⇔ ∃ доказ-зв'язок з `basis == slot` |

Ключ порядку кластерів — `(−share_numerator, cluster_id)` (частка спадно; за рівності чисельників — за ідентифікатором; знаменник спільний, тож порівняння цілих еквівалентне порівнянню часток без float).

### `DiagnosticSignal` (FR-003-09; research R-3, R-8)

| Поле | Тип | Правило |
|---|---|---|
| `kind` | `DiagnosticKind` | |
| `wallets` | tuple[str, …] | покупці, яких стосується; для `flagged_buyers_excluded` — позначені покупці |
| `value` | int \| None | сума / слот для збігів; `None` для позначених |
| `count` | int ≥ 1 | кількість збігів / позначених |
| `detail` | tuple[str, …] | для `same_amounts_unlinked` — `funding` \| `first_buy_spent`; для `flagged_buyers_excluded` — критерії 002 за адресою (`<address>:<criterion>`); інакше порожньо |

Порядок — `(kind, value, wallets)`. `same_*_unlinked` рахуються серед покупців, що **не** належать одному кластеру; збіг нижчий за `natural_max` — не сигнал (FR-003-10 для діагностики теж).

### `ClusterCompleteness` (FR-003-15)

| Поле | Тип | Правило |
|---|---|---|
| `graph_status` | `GraphCompletenessStatus` | з `GraphResult.completeness.status` |
| `ingest_status` | `complete` \| `incomplete` | з 002 |
| `missing_count` | int ≥ 0 | `len(graph.completeness.missing)` |
| `delegated_complete` | bool | з 002 |
| `delegated_reason` | str \| None | з 002 (`not_analyzed` для результатів до T-044) |
| `graph_warnings` | tuple[`GraphWarning`, …] | копія `report.warnings` 002 |
| `can_be_clean` | властивість | `graph_status == complete` **і** `wallets_analyzed >= 1` |

### `ClusterMetadata` (FR-003-18)

| Поле | Тип |
|---|---|
| `mint` | str |
| `schema_version` | const `"003.1"` |
| `cluster_config_version` | int ≥ 1 |
| `thresholds` | `ThresholdsSnapshot` — знімок усіх значень `clusters.yaml`, крім `version` |
| `graph_schema_version` | str — `GraphMetadata.schema_version` (`"002.1"`) |
| `hub_config_version` | int ≥ 1 — з 002 |
| `address_lists_version` | int \| None — з 002 |
| `lists_applied` | bool — з 002; `address_lists_version is None ⇔ not lists_applied` |
| `ingest_config_version` | int ≥ 1 — з 002/001 |
| `ingest_analyzed_at` | int — з 002 (єдине «часове» поле; з входу) |
| `ingest_source` | str |
| `wallets_analyzed` | int ≥ 0 |
| `share_denominator` | int ≥ 0 — `Σ received_amount`; `0 ⇔ wallets_analyzed == 0` |
| `share_denominator_kind` | const `"analyzed_buyers_received_amount"` — знаменник названо явно (FR-003-11) |

### `ClusterResult`

```
ClusterResult(
  metadata: ClusterMetadata,
  completeness: ClusterCompleteness,
  clusters: tuple[Cluster, …],              # упорядковані; усі share_denominator == metadata.share_denominator
  diagnostics: tuple[DiagnosticSignal, …],
  risk_score: int,                          # 0..100 = floor(100 · Σ share·confidence + 0.5)
  computed_band: RiskBand,                  # clean | suspicious | high_concentration — з risk_score і меж конфігу
  band: RiskBand,                           # кінцева, з правилом неповноти
  band_reasons: tuple[str, …],              # непорожній
)
```

Інваріанти (`__post_init__`, гучно):

- члени кластерів попарно не перетинаються (кожен покупець — рівно в одному кластері або в жодному, FR-003-01); кожен член — покупець входу (перевіряє `service` за `IngestResult`);
- `computed_band` відповідає `risk_score` і межам `metadata.thresholds` (`<= band_clean_max` → `clean`; `<= band_suspicious_max` → `suspicious`; інакше `high_concentration`);
- `risk_score == floor(100 · Σ_c share_c · confidence_c + 0.5)` за збереженими округленими `share`/`confidence`;
- **FR-003-15/16 в обидва боки**: `band == clean` ⇔ (`computed_band == clean` **і** `completeness.can_be_clean`); `computed_band ∈ {suspicious, high_concentration}` ⇒ `band == computed_band` (нижня оцінка чинна); `wallets_analyzed == 0` ⇒ `band == insufficient_data`, `clusters == ()`, `risk_score == 0`, `empty_input ∈ band_reasons`;
- `band_reasons`: `clean` ⇒ рівно одна з `no_clusters_on_complete_data` (коли `clusters == ()`) / `clusters_within_clean_threshold`; `insufficient_data` ⇒ містить `empty_input` або `ingest_incomplete`/`delegated_incomplete`/`missing_histories:<n>` відповідно до `completeness`; інакше `clusters_share_weighted`;
- `flagged_buyers_excluded ∈ diagnostics.kind` ⇔ у вході були позначені покупці **і** `link_through_flagged_buyers == false` (перевіряє `service`).

Серіалізація — `clusters/serialize.py::to_dict/to_json` за `contracts/cluster-result.schema.json`.

## Звіт оцінювання (`clusters/evaluate.py`; FR-003-22, FR-003-23)

### `tests/fixtures/real/manifest.yaml`

```yaml
source: MELT (ціновий/модельний детектор; НЕ підтверджене інсайдерство)
collected_at: 2026-10-04
collection: {first_buyers_n: 30, funding_depth: 2, max_signatures_per_wallet: 30, collect_spl_inbound: false, ingest_config_version: 2, delegated_analyzed: false}
tokens:
  - {label: ins0, class: insider, file: result_ins0.json, mint: AedQTgnV…, sha256: <hex>, note: "melt475c"}
  - …                                        # 5 insider + 4 clean; порядок — як у калібруванні 002
```

`class ∈ {insider, clean}`; `sha256` — файла побайтово; мітки впливають лише на зведення.

### Рядок таблиці (на токен) і зведення

`label | class | mint (8 символів) | clusters | largest_share | risk_score | computed_band | band | band_if_complete | status | warnings` — усі значення лише з результатів; `band_if_complete` = `computed_band` (Q1 (а), з явним застереженням у заголовку). Зведення: `insider_above_clean k/5`, `clean_within_clean m/4` (за `risk_score`, як сформульовано SC-001), `clean_band_shown n/4`, `prd_criterion_met yes|no`, блок застережень (мітки MELT; калібрування = перевірка; усі результати без аналізу делегованих купівель; cln4 неповний на рівні 001; версія `hubs.yaml`). Заголовок: `clusters.yaml v<version> sha256 <digest>`, `hubs.yaml v`, `hub_addresses.yaml v`, `schema 003.1`. Жодних шляхів машини й часу запуску (SC-008).

## Зв'язки

```
config/clusters.yaml ── load_cluster_config ──▶ ClusterConfig ──▶ ClusterService
                                                                      │
GraphResult (002: graph, pruned[], buyer_flags[], completeness, report) ─┤ links.extract_links ──▶ Link[]
IngestResult (001: buyers.received_amount/spent/first_buy_*) ────────────┤ indirect.indirect_links ──▶ Link[]
                                                                      ├ cluster.form_clusters ──▶ ClusterDraft[] (члени + докази-зв'язки)
                                                                      ├ behavior.* ──▶ Evidence (поведінкові), DiagnosticSignal[]
                                                                      ├ score.assess ──▶ share, confidence, risk_score, bands
                                                                      └──▶ ClusterResult ──▶ to_dict/to_json ──▶ cluster-result.schema.json
manifest.yaml + result_*.json ──▶ evaluate: from_dict → GraphService → ClusterService ──▶ таблиця ⇄ expected_table.md
```

Cluster 1 — * ClusterMember; Cluster 1 — 1..* Evidence (≥ 1 типу зв'язку); Evidence * — * EdgeRef; ClusterResult 1 — * Cluster (члени не перетинаються); ClusterResult 1 — * DiagnosticSignal; ClusterMetadata 1 — 1 ThresholdsSnapshot.
