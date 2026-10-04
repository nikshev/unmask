# Contract: `config/hubs.yaml` і `config/hub_addresses.yaml` — версіонована конфігурація відсікання (принцип III)

Кожне значення тут змінює, які вершини буде відсічено, отже й кластери. Тому воно живе у YAML під git із `version`, а не в коді й не в прапорцях. Будь-яка зміна файла — підняти його `version` на 1 і додати запис у `config/CHANGELOG.md` **у тому ж коміті** з `sha256` канонічного вмісту (research R-14). Тест `tests/test_hubs_changelog_guard.py` (SC-008) червоніє на будь-якій розбіжності версії й вмісту.

Результат графа несе обидві версії (`metadata.hub_config_version`, `metadata.address_lists_version`) і знімок порогів; результати з різними версіями не порівнюються (FR-002-13).

Завантаження: `unmask.hubs.config.load_hub_config(thresholds_path, lists_path) -> HubConfig`. Невідоме/відсутнє поле чи значення поза межами → `ConfigError` з назвою поля.

## `config/hubs.yaml` (версія 1)

```yaml
# Версіонована конфігурація відсікання хабів (принцип III, VI).
# Схема: specs/002-funding-graph-hub-pruning/contracts/config-hubs.md
# Будь-яка зміна: підняти version і додати запис із sha256 у config/CHANGELOG.md (розділ "# config/hubs.yaml").
version: 1

# Правило порогу (FR-002-14, research R-9): критерій спрацьовує, коли виміряне значення СТРОГО БІЛЬШЕ за поріг.
# Рівно поріг — не хаб. Те саме для попередження про гігантську компоненту.

degree_threshold: 100            # int ≥ 1. Унікальних контрагентів (вхідні ∪ вихідні, всі активи й види ребер).
                                 # Хаб, якщо degree > 100. Страхувальний критерій: у графі 002 вхідний ступінь
                                 # обмежено збором (counterparty_threshold 001), головну роботу робить
                                 # prune_ingest_high_degree. Не калібровано на реальних токенах (research R-12).
one_off_senders_share: 0.8       # float 0..1. Частка унікальних відправників, що надіслали рівно один переказ.
                                 # Хаб, якщо share > 0.8 (біржові депозити — майже всі одноразові).
one_off_min_senders: 10          # int ≥ 2. ПЕРЕДУМОВА критерію (не поріг хаба): застосовується, лише коли
                                 # унікальних відправників >= 10 (включно). Нижче частка статистично безглузда.
giant_component_warn_share: 0.5  # float 0..1. Попередження giant_component, якщо після відсікання частка покупців
                                 # у найбільшій (за покупцями) компоненті > 0.5.
prune_off_curve: true            # bool. Вершини поза кривою ed25519 (PDA: пули, сховища, бондинг-криві) —
                                 # хаби за критерієм known_list із detail address_type:off_curve.
prune_ingest_high_degree: true   # bool. Вершини, які збір позначив unexpanded(high_degree), — хаби (FR-002-07г).
                                 # signature_cap критерієм не є.
```

Обмеження: `version ≥ 1`; `degree_threshold ≥ 1`; `0 ≤ one_off_senders_share ≤ 1`; `one_off_min_senders ≥ 2`; `0 < giant_component_warn_share ≤ 1`; булеві — лише `true`/`false`.

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

# config/hub_addresses.yaml

## 1 — 2026-10-0X
Початкова версія: system_programs(3), token_programs(3), dex_routers(1), amm_programs(2), launchpads(1),
exchanges(0), market_makers(0). Джерела — коментарі у файлі. Біржі й ММ — порожньо до калібрування.
sha256: <hex>
```

Правила: розділ рівня 1 — ім'я файла; запис рівня 2 — `## <version> — <дата>`; останній рядок запису — `sha256: <64 hex>`. `changelog_entries(changelog, "hubs.yaml")` повертає `{1: "<hex>", …}`. Канонічний вміст = `json.dumps(yaml.safe_load(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; коментарі й форматування на дайджест не впливають.

Кожен наступний запис: що змінено, чому, на яких токенах перевірено. Підбір порогів «під результат» без запису заборонений (гейт критерію успіху).

## Що тут не живе

Шляхи до файлів (аргументи `load_hub_config`), рівень логування, будь-що з `config/ingest.yaml` (поріг `counterparty_threshold` для критерію `ingest_high_degree` береться з **метаданих результату 001**, а не з файла — результат уже несе використане значення).
