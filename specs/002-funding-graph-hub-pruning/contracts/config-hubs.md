# Contract: `config/hubs.yaml` і `config/hub_addresses.yaml` — версіонована конфігурація відсікання (принцип III)

Кожне значення тут змінює, які вершини буде відсічено, отже й кластери. Тому воно живе у YAML під git із `version`, а не в коді й не в прапорцях. Будь-яка зміна файла — підняти його `version` на 1 і додати запис у `config/CHANGELOG.md` **у тому ж коміті** з `sha256` канонічного вмісту (research R-14). Тест `tests/test_hubs_changelog_guard.py` (SC-008) червоніє на будь-якій розбіжності версії й вмісту.

Результат графа несе обидві версії (`metadata.hub_config_version`, `metadata.address_lists_version`) і знімок порогів; результати з різними версіями не порівнюються (FR-002-13).

Завантаження: `unmask.hubs.config.load_hub_config(thresholds_path, lists_path) -> HubConfig`. Невідоме/відсутнє поле чи значення поза межами → `ConfigError` з назвою поля.

## `config/hubs.yaml` (версія 3; версія 2 — `one_off_min_senders: 10`; версія 1 — ще й без двох останніх ключів `dust_*`)

Версія 2 вводиться задачею T-054 за калібруванням (`calibration.md`, research R-22): додано критерій `dust_fanout` (FR-002-22). Решта значень v1 без змін — свідомо (`degree_threshold` на реальних даних неактивний, але зниження відсікло б справжнього фінансиста).

Версія 3 вводиться задачею T-058 за перерахунком калібрування (`calibration.md`, розділ «Перерахунок R-23 (2026-10-05)»; research R-23): `one_off_min_senders` 10 → 50. При капі збору 30 видно не більше ~30 відправників, і передумова 10 давала відсікання за `one_off_senders` вершин із 15–30 відправниками — зокрема головного інсайдерського фінансиста ins1. Решта значень v2 без змін. Ціна: на збірках із капом ≤ 49 відправників критерій фактично вимкнено (known-issues 002, §3); переоцінити на збірках із капом ≥ 100.

```yaml
# Версіонована конфігурація відсікання хабів (принцип III, VI).
# Схема: specs/002-funding-graph-hub-pruning/contracts/config-hubs.md
# Будь-яка зміна: підняти version і додати запис із sha256 у config/CHANGELOG.md (розділ "# config/hubs.yaml").
version: 3

# Правило порогу (FR-002-14, research R-9, R-22): рівно поріг НІКОЛИ не спрацьовує, нерівність строга.
# Напрямок — властивість критерію: degree / one_off_senders_share / giant_component_warn_share — СТРОГО БІЛЬШЕ
# (багато — хаб); dust_amount_lamports — СТРОГО МЕНШЕ (мало — пил). Передумови (*_min_*) — «>=», включно.

degree_threshold: 100            # int ≥ 1. Унікальних контрагентів (вхідні ∪ вихідні, всі активи й види ребер).
                                 # Хаб, якщо degree > 100. Страхувальний критерій: у графі 002 вхідний ступінь
                                 # обмежено збором (counterparty_threshold 001), головну роботу робить
                                 # prune_ingest_high_degree. Не калібровано на реальних токенах (research R-12).
one_off_senders_share: 0.8       # float 0..1. Частка унікальних відправників, що надіслали рівно один переказ.
                                 # Хаб, якщо share > 0.8 (біржові депозити — майже всі одноразові).
one_off_min_senders: 50          # int ≥ 2. ПЕРЕДУМОВА критерію (не поріг хаба): застосовується, лише коли
                                 # унікальних відправників >= 50 (включно). Нижче частка статистично безглузда.
                                 # v3 (T-058, R-23, calibration.md «Перерахунок R-23»): було 10. При
                                 # max_signatures_per_wallet = 30 (калібрувальні збори) видимих відправників <= ~30,
                                 # і поріг 10 відсікав вузли з 15–30 відправниками, у т.ч. головного інсайдерського
                                 # фінансиста ins1 (30 відправників, 29 одноразових, fan-out 15, 2,4 SOL). Відомий
                                 # компроміс: при капі 30 передумова 50 недосяжна — критерій фактично вимкнено на
                                 # таких збірках; при капі 300 (дефолт ingest.yaml) працює. Пил ловить dust_fanout;
                                 # переоцінити на збірках із капом >= 100 (known-issues 002).
giant_component_warn_share: 0.5  # float 0..1. Попередження giant_component, якщо після відсікання частка покупців
                                 # у найбільшій (за покупцями) компоненті > 0.5.
prune_off_curve: true            # bool. Вершини поза кривою ed25519 (PDA: пули, сховища, бондинг-криві) —
                                 # хаби за критерієм known_list із detail address_type:off_curve.
prune_ingest_high_degree: true   # bool. Вершини, які збір позначив unexpanded(high_degree), — хаби (FR-002-07г).
                                 # signature_cap критерієм не є.
dust_amount_lamports: 1000000    # int ≥ 1 (лампорти; 1_000_000 = 0,001 SOL). Критерій dust_fanout (FR-002-22,
                                 # research R-22): хаб, якщо медіана сум SOL-ребер до РІЗНИХ покупців (одна сума на
                                 # покупця — агрегат усіх SOL-переказів до нього; верхня медіана) СТРОГО МЕНША за поріг.
                                 # Рівно поріг — не хаб. SPL і делеговані купівлі не рахуються. Значення 1 вимикає
                                 # критерій (жодна сума ребра не < 1). Калібровано на 9 токенах pump.fun
                                 # (calibration.md): пил < 0,001 SOL при fan-out 5–23; фінансисти ≥ 0,7 SOL.
dust_min_fanout: 5               # int ≥ 2. ПЕРЕДУМОВА критерію dust_fanout (не поріг хаба): застосовується, лише коли
                                 # різних покупців, профінансованих у SOL, >= 5 (включно). 5 — найменший fan-out
                                 # пилових джерел у калібруванні.
```

Обмеження: `version ≥ 1`; `degree_threshold ≥ 1`; `0 ≤ one_off_senders_share ≤ 1`; `one_off_min_senders ≥ 2`; `0 < giant_component_warn_share ≤ 1`; булеві — лише `true`/`false`; `dust_amount_lamports ≥ 1` (ціле, не bool); `dust_min_fanout ≥ 2`. Усі дев'ять ключів обов'язкові; відсутній або невідомий → `ConfigError(field)`.

Знімок у результаті (`metadata.thresholds`, `ThresholdsSnapshot`) містить усі ключі, крім `version` (вісім полів); `metadata.hub_config_version` несе `version`.

## `config/hub_addresses.yaml` (версія 1)

```yaml
# Відомі адреси хабів за категоріями (FR-002-07а). Окрема версія від hubs.yaml (FR-002-13).
# Схема: specs/002-funding-graph-hub-pruning/contracts/config-hubs.md
# Будь-яка зміна: підняти version і додати запис із sha256 у config/CHANGELOG.md
# (розділ "# config/hub_addresses.yaml"). Кожна адреса — з коментарем: що це і джерело.
# Адреса може бути лише в одній категорії. Порожня категорія — валідна і означає "ще не зібрано",
# а НЕ "таких хабів немає": результат графа показує lists_applied і версію списку.
version: 1

categories:
  system_programs:
    - "11111111111111111111111111111111"              # System Program
    - "ComputeBudget111111111111111111111111111111"   # Compute Budget Program
    - "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"   # Memo Program v2
  token_programs:
    - "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"   # SPL Token
    - "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"   # SPL Token-2022
    - "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"   # Associated Token Account Program
  dex_routers:
    - "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"   # Jupiter Aggregator v6
  amm_programs:
    - "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"  # Raydium Liquidity Pool v4 (AMM)
    - "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"   # Orca Whirlpool
    # TODO (перевірити адресу перед додаванням, інакше не додавати): Raydium CLMM, Raydium CPMM,
    # Meteora DLMM, Meteora DAMM, PumpSwap AMM, Phoenix, OpenBook v2.
  launchpads:
    - "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"   # Pump.fun
  exchanges: []
    # Гарячі гаманці бірж. НЕ вигадувати. Джерело для заповнення: публічні мітки експлорерів
    # (Solscan / SolanaFM "labels") або власне спостереження на реальних токенах під час калібрування
    # (research R-12); кожна адреса — з назвою біржі, джерелом і датою в коментарі; запис у CHANGELOG.
  market_makers: []
    # Маркет-мейкери. Те саме правило, що й для exchanges.
```

Обмеження: `version ≥ 1`; `categories` містить рівно сім ключів вище (відсутній або невідомий ключ → `ConfigError`); кожна адреса — base58 рівно 32 байти; адреса не повторюється між категоріями.

**Примітка про корисність**: адреси програм майже ніколи не є вершинами графа фінансування (програми не тримають SOL — це роблять їхні PDA, які ловить `prune_off_curve`). Категорії програм лишаються за spec і як захист від дефектів розбору; реальна сила списку — `exchanges`/`market_makers`, що заповнюються при калібруванні.

## `config/CHANGELOG.md` — формат

```markdown
# config/ingest.yaml

## 1 — 2026-10-03
Початкова версія: … (без змін, перенесено під розділ)

# config/hubs.yaml

## 1 — 2026-10-0X
Початкова версія, не калібровано на реальних токенах: degree_threshold=100, one_off_senders_share=0.8,
one_off_min_senders=10, giant_component_warn_share=0.5, prune_off_curve=true, prune_ingest_high_degree=true.
Обґрунтування — specs/002-funding-graph-hub-pruning/research.md R-12.
sha256: <hex канонічного вмісту файла>

## 2 — 2026-10-04
Калібрування на 9 реальних токенах pump.fun (5 інсайдерських за MELT, 4 чисті; Helius, N=30, depth=2, кап 30;
specs/002-funding-graph-hub-pruning/calibration.md). Додано критерій dust_fanout (FR-002-22, research R-22):
dust_amount_lamports=1000000 (0,001 SOL; хаб, якщо медіана SOL-сум до різних покупців СТРОГО МЕНША),
dust_min_fanout=5 (передумова, включно). Чому: пилові джерела (fan-out 5–23, медіана < 0,001 SOL) є в кожному
токені й склеюють до 21/30 покупців; жоден критерій v1 їх не ловить. degree_threshold=100 лишено свідомо: на цих
даних неактивний (макс. ступінь 45), зниження до ~30 відсікло б справжнього фінансиста ins1 (ступінь 45, медіана
≥ 0,7 SOL). Решта значень v1 без змін. Пил/фінансист розділяє сума, не ступінь.
sha256: fdf65bb5369e4e40e629ca4cd45f4952466ee21447ae8bbe79a4ee0035f45409

## 3 — 2026-10-05
Змінено: one_off_min_senders=10 → 50 (передумова критерію one_off_senders, «>=» включно). Решта значень v2 без змін.
Чому (T-058; research R-23; specs/002-funding-graph-hub-pruning/calibration.md, розділ «Перерахунок R-23»): …
(що змінено — значення старе → нове; чому — посилання на калібрування/дослідження/задачу; на яких токенах перевірено;
відомий компроміс і коли переоцінити)
sha256: acfa643b0133da213d54341e6a70026dab93ce3aa67087dfddaab4a465211e2a

# config/hub_addresses.yaml

## 1 — 2026-10-0X
Початкова версія: system_programs(3), token_programs(3), dex_routers(1), amm_programs(2), launchpads(1),
exchanges(0), market_makers(0). Джерела — коментарі у файлі. Біржі й ММ — порожньо до калібрування.
sha256: <hex>
```

Правила: розділ рівня 1 — ім'я файла; запис рівня 2 — `## <version> — <дата>`; останній рядок запису — `sha256: <64 hex>`. `changelog_entries(changelog, "hubs.yaml")` повертає `{1: "<hex>", 2: "<hex>", …}`; `check_changelog` вимагає, щоб `version` файла був останнім записом розділу. Канонічний вміст = `json.dumps(yaml.safe_load(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; коментарі й форматування на дайджест не впливають. Порядок розділів парсеру байдужий, але в поставленому журналі розділ `# config/ingest.yaml` стоїть **останнім** (приклад вище — лише формат): тест 001 `tests/test_tx_batch_size.py` бере хвіст останнього `## `-запису до кінця файла.

Дайджест запису 3 (поточна версія, T-058) обчислено з канонічного вмісту `{"degree_threshold":100,"dust_amount_lamports":1000000,"dust_min_fanout":5,"giant_component_warn_share":0.5,"one_off_min_senders":50,"one_off_senders_share":0.8,"prune_ingest_high_degree":true,"prune_off_curve":true,"version":3}` → `acfa643b0133da213d54341e6a70026dab93ce3aa67087dfddaab4a465211e2a` (звірено трьома незалежними обчисленнями: `content_digest(Path("config/hubs.yaml"))`, `sha256(json.dumps(yaml.safe_load(...)))` і `sha256sum` цього рядка). Копія v3 з `one_off_min_senders: 10` і `version: 2` дає рівно дайджест запису 2 — решта значень не змінилась (`tests/test_hubs_changelog_guard.py`).

Дайджест запису 2 вище (історичний, T-054) обчислено з канонічного вмісту `{"degree_threshold":100,"dust_amount_lamports":1000000,"dust_min_fanout":5,"giant_component_warn_share":0.5,"one_off_min_senders":10,"one_off_senders_share":0.8,"prune_ingest_high_degree":true,"prune_off_curve":true,"version":2}` — він не залежить від коментарів, тож T-054 має отримати рівно його (`content_digest(Path("config/hubs.yaml"))`); розбіжність означає інше значення або тип у файлі.

Кожен наступний запис: що змінено, чому, на яких токенах перевірено. Підбір порогів «під результат» без запису заборонений (гейт критерію успіху).

## Що тут не живе

Шляхи до файлів (аргументи `load_hub_config`), рівень логування, будь-що з `config/ingest.yaml` (поріг `counterparty_threshold` для критерію `ingest_high_degree` береться з **метаданих результату 001**, а не з файла — результат уже несе використане значення).
