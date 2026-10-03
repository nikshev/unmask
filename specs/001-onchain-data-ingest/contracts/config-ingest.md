# Contract: `config/ingest.yaml` — версіонована конфігурація збору (принцип III)

Кожне значення тут змінює результат збору, тому живе у YAML під git, а не в коді й не в прапорцях CLI. Будь-яка зміна файла — підняти `version` на 1 і додати запис у `config/CHANGELOG.md` у тому ж коміті. Результат збору несе `metadata.config_version`; прогони з різними версіями не порівнюються.

Завантаження: `config.py::load_config(path: Path) -> IngestConfig`. Невідоме поле, відсутнє поле або значення поза межами → `ConfigError` з назвою поля. Тихих умовчань у коді немає.

## Схема (версія 1)

```yaml
version: 1                       # int ≥ 1. Підіймається при будь-якій зміні нижче.

first_buyers_n: 300              # int, 1..500. Скільки перших покупців брати (FR-001-01).
                                 # У робочому конфігу тримати в 200..500 (spec); менші значення — лише для тестових конфігів.
funding_depth: 2                 # int, 1..3. Глибина обходу джерел фінансування (FR-001-05).
counterparty_threshold: 200      # int ≥ 1. Унікальних відправників, після яких вершина не розгортається (FR-001-08).
max_signatures_per_wallet: 300   # int ≥ 1. Скільки підписів (гаманець + токен-рахунки) переглядати до межі відсікання
                                 # (research R-3). Довша історія → unexpanded(signature_cap).
collect_spl_inbound: true        # bool. Збирати вхідні SPL через історії токен-рахунків (FR-001-04, research R-8).
time_budget_seconds: 40          # float > 0. Бюджет холодного збору; решта 20 с із 60 с — на наступні етапи (FR-001-16).
commitment: finalized            # finalized | confirmed. Рівень підтвердження для всіх RPC-викликів.

rpc:
  page_size: 1000                # int, 1..1000. limit для getSignaturesForAddress і розмір batch getTransaction.
  request_timeout_seconds: 10    # float > 0. Таймаут одного запиту (обрізається залишком бюджету).
  max_retries: 2                 # int ≥ 0. Повторів на RpcRateLimited / RpcUnavailable у межах бюджету.
  retry_backoff_seconds: 0.5     # float ≥ 0. База експоненційної паузи між повторами.
  max_concurrency: 8             # int ≥ 1. Паралельних запитів в адаптері (ядро лишається послідовним).
```

## `config/CHANGELOG.md`

```markdown
# config/ingest.yaml — changelog

## 1 — 2026-10-0X
Початкова версія: N=300, depth=2, counterparty_threshold=200, max_signatures_per_wallet=300,
collect_spl_inbound=true, time_budget_seconds=40, commitment=finalized, rpc.*=(1000, 10, 2, 0.5, 8).
```

Кожен наступний запис: `## <version> — <дата>`, що змінено, чому, на яких токенах перевірено. Підбір порогів «під результат» без запису — заборонений (гейт критерію успіху).

## Що тут не живе

URL і ключ RPC-провайдера (секрет → змінна середовища `UNMASK_RPC_URL`), шлях до фікстур (аргумент конструктора джерела), рівень логування. Вони не змінюють результат.
