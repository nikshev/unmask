# Implementation Plan: Захист публічного API і операційна готовність

**Branch**: `008-api-protection-ops` (робота в `main`) | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

## Summary

Один регулятор на процес (`delivery/limits.py::Protection`): ковзне вікно запитів на
адресу клієнта (HTTP), глобальні слоти важких аналізів (спільні для HTTP і бота, бо обидва
кличуть той самий `DeliveryService`), ворота користувача бота (вікно + один одночасний).
Слот береться всередині `build`-замикання `single_flight` — його викликає лише власник
холодного mint, тож кеш-хіт і приєднання до in-flight слот не споживають **без зміни API
кешу** (R-2). Відмови — `429`/`503` з `Retry-After` за окремим контрактом 008 (R-6). Межі —
`config/protection.yaml` v1 із журналом (R-5). Операційно: systemd-юніт, приклад nginx з TLS,
розділ README «Deploy», `deploy/smoke.sh`; статичні тести артефактів.

## Technical Context

**Language**: Python 3.12 · **Dependencies**: лише stdlib (`threading`, `ipaddress`, `math`, `time`) — нових залежностей немає
**Testing**: `uv run pytest`; гард мережі `tests/conftest.py`; HTTP — чисті функції без сокетів; бот — `JournalTransport`; годинник — інжектований `ManualClock` (R-3)
**Target**: один процес на Linux за nginx; стан лімітів у пам'яті
**Constraints**: успішні відповіді API/бота побайтово як у 004 (SC-006); без автентифікації (FR-008-04); жодної межі в коді чи CLI (FR-008-08)
**Scale**: 14 FR, 3 user stories, 13 задач (T-161…T-173)

## Constitution Check

| Принцип | Як виконується | Статус |
|---|---|---|
| I. Трасування | кожна задача `[FR-008-NN]`; маркери — таблиця нижче; FR-008-11/12/13 мають носія `impl:` у `deploy/smoke.sh` (R-11) | PASS |
| II. Test-First | тест першим у кожній задачі; без мережі й реального часу (`ManualClock`, синхронний/відкладений `spawn`, блокувальні дублі конвеєра на `threading.Event`) | PASS |
| III. Версіонування | усі межі — `config/protection.yaml` v1 + розділ журналу зі `sha256`; межі не змінюють висновок і не входять у `provenance` (SC-006) | PASS |
| IV. Межі модулів | `limits.py` — чисті примітиви без I/O; HTTP/бот лише викликають їх; AST-тест меж оновлюється явно (R-11) | PASS |
| V. Чесна неповнота | `429`/`503` — окремий вид помилки, ніколи не «чисто»/результат; не кешуються; схема 008 структурно забороняє поля результату | PASS |
| VI. Хаби | не зачіпається | PASS |
| VII. Обсяг | лише обмеження навантаження; авторизації, розподіленого ліміту, WAF, моніторингу немає | PASS |
| Ролі | 8 `critical:` → `implementer-senior` (sonnet); 5 → `implementer` (haiku); `reviewer` (opus) | PASS |

## Project Structure

```text
specs/008-api-protection-ops/
├── plan.md  research.md  data-model.md  quickstart.md  tasks.md
└── contracts/{overload-error.schema.json, overload.md}

config/protection.yaml             # НОВЕ v1 (# impl: FR-008-08)
config/CHANGELOG.md                # + розділ "# config/protection.yaml" ПЕРЕД ingest (ingest — останній)
src/unmask/delivery/
├── limits.py                      # НОВЕ: SlidingWindowLimiter, HeavySlots/Lease, UserGate, Protection
├── config.py                      # + ProtectionConfig, load_protection_config
├── service.py                     # + slots, _guarded_build, needs_analysis
├── http.py                        # + client_address, handle_get, Retry-After, 429/503, --host у serve
├── bot.py                         # + gate/spawn у run_polling, тексти відмов, потокобезпечний реєстр
└── main.py                        # + protection.yaml → Protection → service/serve/run_polling; --host
deploy/
├── unmask.service                 # НОВЕ systemd-юніт (не встановлюється)
├── nginx-unmask.conf.example      # НОВЕ приклад TLS-проксі
└── smoke.sh                       # НОВЕ перевірка розгортання оператором (# impl: FR-008-11, FR-008-12, FR-008-13)
README.md                          # + розділ "## Deploy"
tests/
├── test_protection_config.py      # verifies: FR-008-08
├── test_delivery_limits.py        # verifies: FR-008-01, FR-008-02, FR-008-06, FR-008-07, FR-008-14
├── test_delivery_service_slots.py # verifies: FR-008-02, FR-008-06, FR-008-09
├── test_delivery_client_ip.py     # verifies: FR-008-05
├── test_delivery_http_protection.py # verifies: FR-008-01, FR-008-03, FR-008-04, FR-008-09, FR-008-10
├── test_delivery_bot_limits.py    # verifies: FR-008-07, FR-008-09
├── test_deploy_artifacts.py       # verifies: FR-008-11, FR-008-12, FR-008-13
└── test_delivery_protection_e2e.py # verifies: FR-008-02, FR-008-09, FR-008-14
```

Правки тестів інших фіч (лише ці, з причиною): `tests/test_hubs_config.py` — +`# config/protection.yaml`
у точний набір заголовків журналу; `tests/test_delivery_boundaries.py` — +модуль `limits`,
+`unmask.delivery.limits`, `unmask.ingest.addresses` із заборонених у дозволені (R-2).

## Модулі: що робить · як користуватись · від чого залежить

| Модуль | Що робить | Інтерфейс | Залежить від |
|---|---|---|---|
| `delivery.limits` | примітиви обмеження, потокобезпечні, без I/O | `SlidingWindowLimiter.hit(key)`, `HeavySlots.try_acquire()`, `UserGate.try_enter/leave/refund`, `build_protection(cfg, clock)` | stdlib; тип `ProtectionConfig` |
| `delivery.config` | + завантаження `protection.yaml` | `load_protection_config(path)` | `yaml`, `hubs.config` |
| `delivery.service` | + слот для власника холодного аналізу | `DeliveryService(..., slots=None)`, `needs_analysis(mint)` | `cache`, `limits.HeavySlots`, `ingest.addresses.is_valid_address` |
| `delivery.http` | + адреса клієнта, ліміт запитів, 429/503, `Retry-After` | `client_address(...)`, `handle_get(...)`, `serve(..., host, protection)` | `limits`, `service` |
| `delivery.bot` | + ворота користувача, паралельний воркер | `run_polling(..., gate=None, spawn=None)` | `limits.UserGate`, `service` |
| `delivery.main` | wiring одного `Protection` | `main(argv)` | усе вище |

## FR → де виконується / де перевіряється

| FR | Код | Тест | Задачі |
|---|---|---|---|
| FR-008-01 | `limits.py`, `http.py` | `test_delivery_limits.py`, `test_delivery_http_protection.py` | T-162, T-166 |
| FR-008-02 | `limits.py`, `service.py` | `test_delivery_limits.py`, `test_delivery_service_slots.py`, e2e | T-163, T-164, T-173 |
| FR-008-03 | `http.py` | `test_delivery_http_protection.py` | T-166 |
| FR-008-04 | `http.py` (ключ — лише адреса) | `test_delivery_http_protection.py` | T-166 |
| FR-008-05 | `http.py::client_address` | `test_delivery_client_ip.py` | T-165 |
| FR-008-06 | `limits.py::Lease`, `service.py` | `test_delivery_limits.py`, `test_delivery_service_slots.py` | T-163, T-164 |
| FR-008-07 | `limits.py::UserGate`, `bot.py` | `test_delivery_limits.py`, `test_delivery_bot_limits.py` | T-168, T-169 |
| FR-008-08 | `config/protection.yaml`, `config.py`, `main.py` | `test_protection_config.py`, `test_delivery_main.py` (+тест) | T-161, T-167 |
| FR-008-09 | `service.py`, `http.py`, `bot.py` | slots/http/bot/e2e | T-164, T-166, T-169, T-173 |
| FR-008-10 | `http.py` + `contracts/overload-error.schema.json` | `test_delivery_http_protection.py` | T-166 |
| FR-008-11 | `deploy/unmask.service` (+носій `smoke.sh`) | `test_deploy_artifacts.py` | T-170 |
| FR-008-12 | `deploy/nginx-unmask.conf.example` | `test_deploy_artifacts.py` | T-171 |
| FR-008-13 | `README.md` «Deploy», `deploy/smoke.sh` | `test_readme_deploy.py` | T-172 |
| FR-008-14 | `limits.py` (інжектований годинник) | `test_delivery_limits.py`, e2e | T-162, T-173 |

## Маркери трасування

| Файл | Шапка (доповнює наявний рядок `impl:` через кому) |
|---|---|
| `src/unmask/delivery/limits.py` | `# impl: FR-008-01, FR-008-02, FR-008-06, FR-008-07, FR-008-14` |
| `src/unmask/delivery/config.py` | `+ FR-008-08` |
| `src/unmask/delivery/service.py` | `+ FR-008-02, FR-008-06, FR-008-09` |
| `src/unmask/delivery/http.py` | `+ FR-008-01, FR-008-03, FR-008-04, FR-008-05, FR-008-09, FR-008-10` |
| `src/unmask/delivery/bot.py` | `+ FR-008-07, FR-008-09` |
| `src/unmask/delivery/main.py` | `+ FR-008-08` |
| `config/protection.yaml` | `# impl: FR-008-08` |
| `deploy/smoke.sh` | `# impl: FR-008-11, FR-008-12, FR-008-13` |
| тести | `# verifies:` за деревом вище; `test_delivery_main.py` — дописати `FR-008-08` до наявного рядка |

## Спільні файли з чергою (паралельно йде 007 — пул RPC)

| Файл | Хто і в якому порядку | Правило |
|---|---|---|
| `src/unmask/delivery/main.py` | 1) wiring-задача пулу RPC з `specs/007-rpc-provider-pool/tasks.md` (у plan 007 — T-139); 2) T-167 (Protection, `--host`); 3) T-169 (один рядок `gate=`) | 008 стартує ПІСЛЯ злиття wiring 007; перед початком — `git log -- src/unmask/delivery/main.py` |
| `config/CHANGELOG.md` | 007 (розділ `# config/rpc_pool.yaml`) і T-161 — хто другий, той ребейзить | обидва розділи ПЕРЕД `# config/ingest.yaml`; ingest — останній; інших розділів не чіпати |
| `tests/test_hubs_config.py` | 007 і T-161 додають по одному заголовку в точний набір | хто другий — дописує свій до вже оновленого списку і число заголовків |
| `tests/test_delivery_main.py` | 007 (якщо чіпає) → T-167 | лише додавання тестів |
| `tests/test_delivery_boundaries.py` | 007 T-139 (+`unmask.ingest.rpc.pool_config`) → T-162 (+`limits`) → T-164 (`ingest.addresses`) | однорядкові додавання, ребейз тривіальний |
| `src/unmask/delivery/http.py`, `service.py`, `bot.py` | лише 008 | — |

## Порядок і мінімальний вертикальний зріз

**Зріз (ціль — 9.10 вечір):** T-161 → T-162 ∥ T-163 → T-164 → T-165 → T-166 → T-167 →
ручний дим за `quickstart.md` §2 (429 і 503 на локальному сервері). Бот уже отримує спільну
глобальну межу через `DeliveryService` (busy-текст додає T-169, до того бот покаже
загальне «incomplete data…» — чесно, але негарно).

Далі: T-168 → T-169 (бот); T-170 ∥ T-171 ∥ T-172 (деплой, незалежні — можна будь-коли,
зокрема паралельно зі зрізом); T-173 (e2e SC) останнім.

| Ризик | Що робимо |
|---|---|
| wiring 007 затримується → блокує T-167 | до 10.10 12:00 чекаємо; далі рішення людини: T-167 першим, 007 ребейзить свою зміну `main.py` (зміни незалежні: джерело vs регулятор) |
| флейк конкурентних тестів | синхронізація лише через `threading.Event`/`Barrier` з таймаутом; жодних `sleep` |
| nginx ріже холодні аналізи по 60 с | `proxy_read_timeout 1830s` у прикладі (було 660s до ingest v4) + тест (R-10) |
| юніт не стартує під іншим користувачем | без `User=`/`DynamicUser` (R-8, Q5) |

## Відкриті питання (не BLOCKED: обрано варіант, архітектура тримається; ціна зміни вказана)

| # | Питання | Обрано | Ціна іншої відповіді |
|---|---|---|---|
| Q1 | Бот послідовний (004) — US2.2 «вже виконується аналіз» без паралельної обробки неможливий | паралельний воркер на `/check` за наявності `gate` (R-9) | лишити послідовним: T-169 спрощується, сценарій US2.2 стає недосяжним (тест — лише юніт `UserGate`); але поки йде 600-с аналіз, бот мовчить для всіх |
| Q2 | Що рахує вікно бота «важких аналізів» | лише `/check` на валідний некешований mint (`needs_analysis`) | рахувати всі `/check`: −1 метод сервісу, правка 2 тестів T-169 |
| Q3 | Контракт FR-008-10: правити схему 004 чи окремий | окремий 008.1 (R-6) | правка 004: `oneOf` + переробка валідації в 3 тестових модулях 004 (~0,5 дня), ризик SC-006 |
| Q4 | `delivery.yaml` v2 чи `protection.yaml` | `protection.yaml` (R-5) | v2: правки `test_delivery_config.py` (4 тести), `DeliveryConfig` + рендер-тести, що будують його вручну |
| Q5 | Запуск від окремого користувача | root без `User=` (+`NoNewPrivileges`) | `User=unmask`: оператор створює користувача і ставить python поза `/root` (`UV_PYTHON_INSTALL_DIR=/opt/uv-python`); юніт +1 рядок |
| Q6 | Імпорт `ingest.addresses.is_valid_address` у доставку | дозволити (одне джерело істини формату) | локальний regex: невалідні за довжиною base58 хвилинно займуть слот і можуть отримати 503 замість 400 |
| Q7 | Docker-розгортання | поза обсягом (`docker-compose.yml` не змінюється); за Docker `peer` — шлюз мосту, його треба додати в `proxy.trusted` версією | оновити compose + README: +1 задача |
| Q8 | `Retry-After` для `busy` | фіксоване з YAML (60) | оцінка з часу старту найстарішого слота — нечесна (тривалість невідома), +логіка |
