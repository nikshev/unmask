# Contract: публічний інтерфейс фічі 003 — `ClusterService`, модулі, оцінювальна команда

Споживачі — HTTP-API/бот (наступні фічі) і оцінювальна команда. Усе, що вони знають про кластери, докази й оцінку, — цей контракт, `data-model.md` і `cluster-result.schema.json`.

Залежність від фічі 002 — **лише** через `unmask.graph.model.GraphResult` (об'єкт від `GraphService.analyze`); від фічі 001 — **лише** через `unmask.ingest.model.IngestResult` (той самий результат, з якого зроблено граф) і, для фікстур, через `unmask.ingest.serialize.from_dict`. Жоден модуль ядра 003 не імпортує `unmask.hubs.{criteria,prune,report}`, `unmask.graph.{build,measures,components,service}`, внутрішності збору 001 (перевіряється AST-тестом; `plan.md`, «Правило залежностей»).

## 1. `unmask.clusters.config` — конфігурація (принцип III)

```python
load_cluster_config(path: Path) -> ClusterConfig
```

- Відсутній файл, невідоме/відсутнє поле, значення поза межами, `indirect_link >= shared_funder`, `band_clean_max >= band_suspicious_max` → `ConfigError(field)` (клас `unmask.hubs.config.ConfigError` — один клас помилки конфігурації на проєкт).
- Спершу `hubs.config.content_digest(path)` (обмежений розбір 002 відкидає будь-яку ваду файла), потім значення; `ClusterConfig.digest` — цей дайджест.
- Чиста відносно файлової системи; нічого не кешує; журнал не читає (перевірка журналу — тест `test_clusters_changelog_guard.py` через `hubs.config.check_changelog(Path("config/clusters.yaml"), Path("config/CHANGELOG.md"))`).

## 2. `unmask.clusters.links` — зв'язки проходу 1 (FR-003-02, FR-003-03, FR-003-05, FR-003-06)

```python
extract_links(graph_result: GraphResult, config: ClusterConfig) -> tuple[Link, ...]
```

Повертає всі зв'язки видів `shared_funder`, `direct_transfer`, `delegated_buy`, `recovered_edge` за правилами:

| Вид | Умова | `via` | `refs` | `window`/`basis` |
|---|---|---|---|---|
| `direct_transfer` | ребро `graph.edges` виду `transfer`, `asset ∈ link_assets`, `amount >= link_min_amount_lamports`, обидва кінці — покупці | `()` | `edge.refs` | `[first_time, last_time]`/`block_time`; якщо `first_time is None` — `[first_slot, last_slot]`/`slot` |
| `shared_funder` | для не-покупця `S`: вихідні ребра виду `transfer` до покупців з тими самими умовами; впорядковані за часом (`first_time`; за відсутності — усі в слотах); сусідні з проміжком `<= funding_window_seconds` (чи `<= window_slots`) — одна група; група з `>= 2` різних покупців | `(S,)` | об'єднання `refs` ребер групи | `[min, max]` часів ребер групи |
| `delegated_buy` | ребро виду `delegated_buy`, обидва кінці — покупці | `()` | `edge.refs` | як `direct_transfer` |
| `recovered_edge` (US6) | для запису `pruned[i]`: `incident_edges` виду `transfer` з `sender == pruned[i].address`, `receiver` — покупець, `asset ∈ link_assets`, `amount >= поріг`; групування як `shared_funder` | `(pruned[i].address,)` | об'єднання `refs` | як `shared_funder` |

- `link_through_flagged_buyers == false` ⇒ покупець з непорожнім `buyer_flags` не є кінцем жодного зв'язку (ребро до/від нього пропускається). Ребро `delegated_buy` з не-покупцем на одному кінці зв'язку не утворює (один покупець) — не-покупець потрапить у `via` лише коли є два покупці через одну делеговану транзакцію (не буває за правилом 1:1 001; гілка існує для повноти контракту: такий `Link` не створюється).
- `S`, що є покупцем, обробляється як `direct_transfer` (кожне ребро окремо), не як `shared_funder`: члени кластера — покупці, джерело-покупець — член (Assumption spec).
- Ребра з `amount < link_min_amount_lamports` ніде не з'являються (ні зв'язок, ні доказ, FR-003-05). SPL-ребра (не в `link_assets`) — теж.
- Детермінізм: результат — функція вмісту `graph_result`, не порядку його кортежів; `Link`и впорядковані за `(type, via, sorted(buyers), refs[0])`.

## 3. `unmask.clusters.indirect` — прохід 2 (FR-003-04)

```python
indirect_links(graph_result: GraphResult, clusters_so_far: Mapping[str, str], config: ClusterConfig) -> tuple[Link, ...]
```

`clusters_so_far` — `{покупець: ідентифікатор компоненти після проходу 1}` (одинаки — власна адреса). Для кожного не-покупця `C` (не позначеного — не-покупці позначок не мають; відсічені в `graph` відсутні за побудовою): шляхи `C → M → P` і `C → P`, де `M` — не-покупець у `graph`, `P` — покупець (не позначений), усі ребра виду `transfer`, `asset ∈ link_assets`, `amount >= поріг`; ребра `C → *` групуються вікном-ланцюгом (як §2) за їхнім часом; група, що містить покупців з **≥ 2 різних** значень `clusters_so_far`, дає `Link(indirect_link, buyers, via=(C, M…), refs=усі ребра шляхів, window, basis)`. `indirect_enabled == false` → `()`. Вершина `M`, що є хабом, у `graph` відсутня — зв'язку через неї немає (US4.2) без окремого коду.

## 4. `unmask.clusters.cluster` — union-find (FR-003-01, FR-003-12)

```python
form_clusters(buyers: Sequence[str], links: Sequence[Link]) -> tuple[ClusterDraft, ...]
cluster_id(wallets: Iterable[str]) -> str          # "c-" + sha256(",".join(sorted(wallets)))[:16]
```

Union-find за рангом зі стисканням шляху, ітеративний, вершини — адреси покупців, об'єднання — за `Link.buyers`; компонента з ≥ 2 покупців → `ClusterDraft(wallets, links)` (усі `Link`и, чиї `buyers` лежать у компоненті — за побудовою кожен лежить рівно в одній). Одинаки кластера не утворюють. Порядок — за мінімальною адресою; результат не залежить від порядку `links`.

## 5. `unmask.clusters.behavior` — поведінкові докази й діагностика (FR-003-08…10)

```python
behavioral_evidence(draft: ClusterDraft, graph_result: GraphResult, ingest_result: IngestResult, config: ClusterConfig) -> tuple[Evidence, ...]
diagnostics(drafts: Sequence[ClusterDraft], graph_result: GraphResult, ingest_result: IngestResult, config: ClusterConfig) -> tuple[DiagnosticSignal, ...]
```

- `same_amounts`/`funding`: серед ребер-зв'язків (виду `transfer`, `asset ∈ link_assets`, `amount >= поріг`) до членів кластера найчастіший `amount` з кількістю `> same_amount_natural_max` → один доказ (`value` — сума, `refs` — ці ребра, `wallets` — отримувачі). `same_amounts`/`first_buy_spent`: серед `Buyer.spent` з `asset == sol` членів найчастіша сума `> natural_max` → один доказ (`refs` — `EdgeRef(first_buy_signature, first_buy_slot, None)`).
- `same_slot`: для членів кластера максимум кількості `first_buy_slot ∈ [s, s + same_slot_window_slots]` по `s` з множини слотів членів; `> same_slot_natural_max` → один доказ (`value` — `s`, найменший при нічиїй).
- Точна рівність цілих; допуску немає. Збіг рівно `natural_max` — не доказ (три точки тестом).
- `diagnostics`: ті самі підрахунки серед покупців, що не належать одному кластеру (пари/групи з різних кластерів чи одинаків) → `same_amounts_unlinked`, `same_slot_unlinked`; `flagged_buyers_excluded` — усі позначені покупці з критеріями, якщо `link_through_flagged_buyers == false` і позначені є.

## 6. `unmask.clusters.score` — впевненість, частка, оцінка (FR-003-11, FR-003-13…17)

```python
confidence(evidence: Sequence[Evidence], config: ClusterConfig, *, artifact: bool) -> float
is_artifact(draft: ClusterDraft, graph_result: GraphResult, config: ClusterConfig) -> bool
assess(clusters: Sequence[Cluster], completeness: ClusterCompleteness, config: ClusterConfig) -> tuple[int, RiskBand, RiskBand, tuple[str, ...]]   # risk_score, computed_band, band, band_reasons
```

- `weight_i = evidence_weights[type] × (slot_fallback_multiplier if type ∈ LINK_TYPES and basis == slot else 1)`; `confidence = round(1 − Π(1 − weight_i), 4)`, × `artifact_confidence_multiplier` якщо `artifact`.
- `is_artifact`: `len(wallets) / wallets_analyzed > artifact_buyer_share` (строго) **і** (`giant_component ∈ graph_result.report.warnings` **або** кількість `indirect_link` > кількості решти доказів-зв'язків).
- `share = round(Σ received_amount(members) / Σ received_amount(усіх покупців), 4)`; чисельник і знаменник у кластері.
- `risk_score = floor(100 × Σ share_c × confidence_c + 0.5)`; `computed_band` за `band_clean_max`/`band_suspicious_max`; `band`: `wallets_analyzed == 0` → `insufficient_data` (`empty_input`); `computed_band == clean` і `not completeness.can_be_clean` → `insufficient_data` (причини з повноти); інакше `computed_band`.

## 7. `unmask.clusters.service` — публічний вхід

```python
ClusterService(config: ClusterConfig)
ClusterService.analyze(graph_result: GraphResult, ingest_result: IngestResult) -> ClusterResult
```

Кроки: перевірка типів і узгодженості входів → `extract_links` → `form_clusters` → (`indirect_links` → повторне `form_clusters` з доданими зв'язками, якщо `indirect_enabled`) → `behavioral_evidence` на кожен кластер → `is_artifact`/`confidence`/`share` → `diagnostics` → `assess` → `ClusterMetadata` (версія й знімок `clusters.yaml`; `hub_config_version`, `address_lists_version`, `lists_applied`, `graph_schema_version`, `ingest_config_version`, `ingest_analyzed_at`, `ingest_source`, `wallets_analyzed` — з `graph_result.metadata`; `share_denominator`) → `ClusterCompleteness` з `graph_result.completeness` і `report.warnings` → `ClusterResult`.

Гарантії:

- Не піднімає винятків через дані: будь-яка узгоджена пара (повна, неповна, порожня, з `NOT_ANALYZED`, з позначеними покупцями, без ребер) дає `ClusterResult`. Винятки — лише дефекти: `TypeError` (не `GraphResult`/`IngestResult`; `Rejection`), `ClusterInputError` (неузгоджені входи: різні `mint`, різні множини покупців чи ранги, `wallets_analyzed`, `analyzed_at`), `ConfigError` (лише при завантаженні, до створення сервісу).
- Без годинника, мережі, файлів, стану між викликами; входи не змінюються (усі типи frozen); `analyze(g, r)` двічі → `to_json` побайтово однаковий; результат не залежить від порядку кортежів у `g`/`r` (SC-003).
- Жодна смуга `clean` на неповному вході; `risk_score` на неповному вході — та сама формула (нижня оцінка).
- Кожен кластер має ≥ 1 доказ типу зв'язку й ≥ 2 членів; члени кластерів не перетинаються; `cluster_id` стабільний для складу.

## 8. `unmask.clusters.serialize`

```python
to_dict(result: ClusterResult) -> dict     # валідний проти contracts/cluster-result.schema.json
to_json(result: ClusterResult) -> str      # sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
```

Відображення явне, поле за полем (не `asdict`): `StrEnum` → рядок; `EdgeRef` → `{signature, slot, instruction_path}`; кортежі — у канонічному порядку моделі; `None` → `null` лише там, де схема дозволяє. Невідомий тип → `TypeError`.

## 9. `unmask.clusters.evaluate` — оцінювальна команда (FR-003-22, FR-003-23, SC-008)

```python
evaluate(fixtures_dir: Path, *, clusters_config: Path, hubs_config: Path, lists_config: Path | None) -> str   # Markdown
main(argv: list[str] | None = None) -> int
```

```bash
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real            # друкує таблицю; код 0
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real --check    # 0, якщо побайтово == expected_table.md; інакше 1 і diff
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real --write    # перезаписує expected_table.md (лише крок калібрування)
uv run python scripts/evaluate_clusters.py --fixtures tests/fixtures/real           # те саме через обгортку
```

Прапорці шляхів конфігів (`--clusters-config`, `--hubs-config`, `--lists-config`) мають умовчання `config/*.yaml` відносно кореня репозиторію й **не впливають на результат** (принцип III: вони вибирають файл, а не значення). Конвеєр на токен: `from_dict(json)` → `GraphService(load_hub_config(...)).analyze` → `ClusterService(load_cluster_config(...)).analyze` → рядок таблиці (`data-model.md`, «Звіт оцінювання»). Порядок рядків — порядок `manifest.yaml`. Жодних звернень у мережу (гард тестів), жодного годинника й абсолютних шляхів у виведенні. Відсутній файл з маніфесту, розбіжність `sha256`, невалідний документ → ненульовий код з назвою файла (не мовчазний пропуск).

## 10. Що фіча не робить

Не збирає дані, не будує й не змінює граф, не відсікає хаби, не ходить у мережу, не читає конфіг і фікстури сама (крім оцінювальної команди, яка є викликачем), не зберігає нічого між викликами, не змінює `GraphResult`/`IngestResult`, не рендерить, не кешує, не обслуговує HTTP.
