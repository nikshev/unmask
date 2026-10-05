# Contract: `config/clusters.yaml` — версіонована конфігурація кластеризації й оцінки (принцип III)

Кожне значення тут змінює, які покупці потраплять в один кластер, яку впевненість він отримає і яку смугу отримає токен. Тому воно живе у YAML під git із `version`, а не в коді й не в прапорцях. Будь-яка зміна файла — підняти `version` на 1 і додати запис у `config/CHANGELOG.md` (розділ `# config/clusters.yaml`) **у тому ж коміті** з `sha256` канонічного вмісту (той самий механізм, що в 002: research 002 R-14). Тест `tests/test_clusters_changelog_guard.py` (SC-007) червоніє на будь-якій розбіжності версії й вмісту.

Результат несе `metadata.cluster_config_version` і знімок усіх значень (`metadata.thresholds`), а також версії конфігів 002 і 001, з яких отримано вхід (FR-003-18); результати з різними версіями не порівнюються.

Завантаження: `unmask.clusters.config.load_cluster_config(path) -> ClusterConfig`. Невідоме/відсутнє поле чи значення поза межами → `ConfigError` (клас `unmask.hubs.config.ConfigError`) з назвою поля. Завантажувач спершу викликає `hubs.config.content_digest(path)` — обмежений розбір 002 (без анкерів/аліасів/тегів, ліміти розміру й глибини) відкидає будь-яку ваду файла `ConfigError`, — і лише потім читає значення (research R-11); `hubs/config.py` не змінюється.

## `config/clusters.yaml` (версія 1 — ПРОПОЗИЦІЯ; точні значення фіксує задача калібрування із записом)

```yaml
# Версіонована конфігурація кластеризації й оцінки ризику (принцип III).
# Схема: specs/003-wallet-clusters-risk/contracts/config-clusters.md
# Будь-яка зміна: підняти version і додати запис із sha256 у config/CHANGELOG.md (розділ "# config/clusters.yaml").
version: 1

# Правило порогу — як у всьому проєкті (002 R-9): нерівність строга, рівно поріг не змінює рішення в бік
# «спрацювало». Напрямок — властивість поля: *_min_* — зв'язок при «>=» (рівно поріг — зв'язок; строго менше — ні);
# *_natural_max — доказ при «>» (рівно поріг — природний збіг, не доказ); artifact_buyer_share — «>».

# --- Зв'язки (прохід 1; FR-003-02, FR-003-03, FR-003-05) ---
link_min_amount_lamports: 10000000   # int >= 1 (лампорти; 10_000_000 = 0,01 SOL). Ребро SOL до покупця є зв'язком і доказом,
                                     # лише якщо Edge.amount >= порога. На порядок вище за пил (< 0,001 SOL, 002 R-22) і нижче
                                     # найменших реальних сум фінансування у калібруванні (0,098 SOL). Research R-2, R-12.
funding_window_seconds: 3600         # int >= 1. Перекази одного джерела РІЗНИМ покупцям сортуються за часом; сусідні з
                                     # проміжком <= W — одна група (вікно-ланцюг); група з >= 2 покупців — зв'язок shared_funder.
                                     # Калібрування: пачки інсайдерських розподільників мають паузи до 1005 с усередині й
                                     # 1,9–12 год між хвилями. Research R-4.
seconds_per_slot: 0.4                # float > 0. Наближення тривалості слота для вікна в слотах, коли block_time відсутній:
                                     # window_slots = floor(funding_window_seconds / seconds_per_slot). Виміряно 0,375–0,436 с
                                     # на 9 токенах; не гарантія мережі. Такий доказ має basis=slot і нижчу вагу (див. нижче).
link_through_flagged_buyers: false   # bool. false: ребра, інцидентні покупцям з buyer_flags 002 (напр. PDA бондинг-кривої,
                                     # що «купила» при продажу), не є зв'язком жодного виду; такі покупці видимі в
                                     # diagnostics як flagged_buyers_excluded. Research R-3 (без цього чисті токени склеюються).
link_assets:                         # непорожній список активів ребер, що можуть бути зв'язком. v1 — лише SOL: поріг у
  - sol                              # лампортах не має сенсу для SPL, калібрувальні збори без SPL. Research R-2.

# --- Прохід 2 (FR-003-04) ---
indirect_enabled: true               # bool. Непрямий зв'язок: спільне джерело C на глибині 2 (C→M→P1, C→M'→P2) через
                                     # невідсічені не-покупці, усі ребра >= link_min_amount_lamports, вікно-ланцюг за C→M.
                                     # Доказ indirect_link називає C і всі M. Research R-6.

# --- Поведінкові докази (FR-003-08, FR-003-10) ---
same_amount_natural_max: 3           # int >= 1. «Однакові суми» — доказ, якщо найчастіша сума (фінансування >= порога зв'язку,
                                     # або spent SOL першої купівлі) повторюється СТРОГО БІЛЬШЕ ніж natural_max разів у кластері.
                                     # Калібрування: ins {4, 15, 11} проти чистих {1, 1, 3, 3}. Точна рівність у лампортах.
same_slot_natural_max: 5             # int >= 1. «Купівлі в одному слоті» — доказ, якщо покупців кластера у вікні слотів
                                     # СТРОГО БІЛЬШЕ ніж natural_max. Калібрування: ins {16, 6, 3, 5, 17} проти чистих {2, 2, 4, 8}.
same_slot_window_slots: 0            # int >= 0. Ширина вікна слотів [s, s + w]; 0 — рівно один слот.

# --- Впевненість (FR-003-11, FR-003-04): noisy-OR незалежних доказів ---
# confidence = 1 − Π(1 − weight_i); weight_i = evidence_weights[type] × (slot_fallback_multiplier, якщо доказ-зв'язок
# виміряно в слотах). Монотонна: більше доказів → не менша впевненість. Research R-9.
evidence_weights:                    # кожне 0 < w < 1; indirect_link МУСИТЬ бути < shared_funder (FR-003-04; ConfigError інакше)
  shared_funder: 0.6                 # спільне джерело у вікні — найсильніший і найкраще відкалібрований сигнал
  direct_transfer: 0.5               # прямий переказ між покупцями (китова консолідація — не обов'язково одна особа)
  delegated_buy: 0.6                 # делегована купівля — перевірене правило 1:1 фічі 001
  recovered_edge: 0.4                # справжнє ребро від відсіченого змішаного відправника (US6)
  indirect_link: 0.3                 # непрямий зв'язок через спільне джерело глибини 2
  same_amounts: 0.2                  # поведінковий, допоміжний
  same_slot: 0.15                    # поведінковий, допоміжний
slot_fallback_multiplier: 0.8        # float 0 < m <= 1. Вага доказу-зв'язку, виміряного в слотах (block_time відсутній).

# --- Артефакт відсікання (FR-003-17) ---
artifact_buyer_share: 0.5            # float 0 < x <= 1. Кластер з часткою ПОКУПЦІВ вибірки > x (строго) при giant_component у
                                     # попередженнях графа 002 або при переважно непрямих доказах → possible_pruning_artifact.
                                     # 0,5 = giant_component_warn_share 002 (той самий зміст).
artifact_confidence_multiplier: 0.5  # float 0 < m <= 1. Впевненість такого кластера × m.

# --- Оцінка токена (FR-003-13; PRD) ---
# risk_score = floor(100 × Σ_clusters share × confidence + 0.5), ціле 0..100.
band_clean_max: 20                   # int >= 0, < band_suspicious_max. risk_score <= 20 → clean (лише на повних даних;
                                     # на неповних — insufficient_data з причинами, FR-003-15).
band_suspicious_max: 50              # int <= 100. 20 < risk_score <= 50 → suspicious; > 50 → high_concentration.
```

Обмеження (усі перевіряються завантажувачем; відсутній або невідомий ключ → `ConfigError(field)`): `version ≥ 1`; `link_min_amount_lamports ≥ 1` (ціле, не bool); `funding_window_seconds ≥ 1`; `seconds_per_slot > 0` (число, не NaN); булеві — лише `true`/`false`; `link_assets` — непорожній список рядків `sol` | `spl:<base58 32 байти>` без дублів; `same_amount_natural_max ≥ 1`, `same_slot_natural_max ≥ 1`, `same_slot_window_slots ≥ 0`; `evidence_weights` — рівно сім ключів, кожен `0 < w < 1`, `indirect_link < shared_funder`; `0 < slot_fallback_multiplier ≤ 1`; `0 < artifact_buyer_share ≤ 1`; `0 < artifact_confidence_multiplier ≤ 1`; `0 ≤ band_clean_max < band_suspicious_max ≤ 100`.

Знімок у результаті (`metadata.thresholds`, `ThresholdsSnapshot`) містить усі ключі, крім `version` (17 полів з тими самими межами); `metadata.cluster_config_version` несе `version`.

## `config/CHANGELOG.md` — розділ і його місце

Розділ `# config/clusters.yaml` додається **між** `# config/hub_addresses.yaml` і `# config/ingest.yaml`. Розділ `# config/ingest.yaml` **мусить лишатися останнім**: тест фічі 001 `tests/test_tx_batch_size.py` бере хвіст останнього `## `-запису до кінця файла, і це закріплено тестом 002 `test_hubs_changelog_guard.py::test_ingest_section_stays_last_for_001_changelog_test`. Парсер `changelog_entries` від порядку розділів не залежить, тож для 002/003 місце розділу байдуже — обмеження лише з боку 001.

```markdown
# config/hub_addresses.yaml
…

# config/clusters.yaml

## 1 — 2026-10-0X
Початкова версія за research.md R-2, R-4, R-6, R-8, R-9, R-10, R-12 (пропозиція; калібрування на 9 збережених
результатах 002 — calibration.md 003): link_min_amount_lamports=10000000 (0,01 SOL), funding_window_seconds=3600
(вікно-ланцюг за проміжком), seconds_per_slot=0.4, link_through_flagged_buyers=false (PDA бондинг-кривої як покупець
склеює чисті токени), link_assets=[sol], indirect_enabled=true, same_amount_natural_max=3, same_slot_natural_max=5,
same_slot_window_slots=0, evidence_weights (shared_funder 0.6, direct_transfer 0.5, delegated_buy 0.6, recovered_edge 0.4,
indirect_link 0.3, same_amounts 0.2, same_slot 0.15), slot_fallback_multiplier=0.8, artifact_buyer_share=0.5,
artifact_confidence_multiplier=0.5, band_clean_max=20, band_suspicious_max=50. На 9 токенах: інсайдерські > 20 — 3/5
(32, 23, 6, 5, 54), чисті <= 20 — 3/4 (0, 10, 5, 25); мітки MELT цінові, вибірка не статистична.
sha256: c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05

# config/ingest.yaml
…
```

Правила ті самі, що в 002: запис рівня 2 — `## <version> — <дата>`; останній непорожній рядок запису — `sha256: <64 hex>`; `check_changelog(Path("config/clusters.yaml"), Path("config/CHANGELOG.md"))` вимагає, щоб `version` файла був останнім записом розділу і дайджест збігався. Канонічний вміст = `json.dumps(yaml.safe_load(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; коментарі й форматування не впливають.

Дайджест запису 1 вище обчислено з канонічного вмісту `{"artifact_buyer_share":0.5,"artifact_confidence_multiplier":0.5,"band_clean_max":20,"band_suspicious_max":50,"evidence_weights":{"delegated_buy":0.6,"direct_transfer":0.5,"indirect_link":0.3,"recovered_edge":0.4,"same_amounts":0.2,"same_slot":0.15,"shared_funder":0.6},"funding_window_seconds":3600,"indirect_enabled":true,"link_assets":["sol"],"link_min_amount_lamports":10000000,"link_through_flagged_buyers":false,"same_amount_natural_max":3,"same_slot_natural_max":5,"same_slot_window_slots":0,"seconds_per_slot":0.4,"slot_fallback_multiplier":0.8,"version":1}` — задача, що створює файл з рівно цими значеннями й типами, має отримати саме його з `content_digest(Path("config/clusters.yaml"))`; розбіжність означає інше значення чи тип. Якщо калібрування змінить значення **до** першого коміту файла — запис 1 пишеться з новими значеннями й новим дайджестом (версія лишається 1, бо файла ще не було в git); після коміту — лише версія 2 з новим записом.

Кожен наступний запис: що змінено, чому, на яких токенах перевірено, таблиця варіантів (як research R-12). Підбір порогів «під мітки» без запису заборонений гейтом критерію успіху.

## Еталон оцінювання (FR-003-23)

`tests/fixtures/real/expected_table.md` — виведення `python -m unmask.clusters.evaluate --fixtures tests/fixtures/real`, записане прапорцем `--write`. Заголовок еталона містить `clusters.yaml v<version> sha256 <digest>`; тест звіряє дайджест з `content_digest(config/clusters.yaml)` — зміна порога без перегенерації еталона червона. Перегенерація — лише свідомий крок калібрування із записом у `specs/003-wallet-clusters-risk/calibration.md` (журнал еталона: дата, версія конфігу, що змінилось у таблиці, чому).

## Що тут не живе

Шляхи до файлів (аргументи `load_cluster_config` і оцінювальної команди), рівень логування, будь-що з `config/hubs.yaml`, `config/hub_addresses.yaml`, `config/ingest.yaml` (версії й пороги 002/001 читаються з метаданих `GraphResult`, а не з файлів — результат уже несе використані значення). Мітки `insider`/`clean` оцінювального набору живуть у `tests/fixtures/real/manifest.yaml` — це дані перевірки, не конфіг аналізу; `ClusterService` їх не бачить.
