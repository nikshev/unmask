# Feature Specification: Бенчмарк фінансової координації за ground truth

**Feature Branch**: `005-benchmark-funding-coordination`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description: "Потрібен правильний бенчмарк координації фінансування (не MELT price manipulation). Ground truth = публічно відомі інсайдерські пуллі (напр. від Pump.fun creator wallet, відомих біджерів, від розслідувань Solidus Labs / Carnahan v. Baton). Оцінюємо: precision/recall/F1 по кластерах, calibration curve risk_score→P(insider), confusion matrix по смугах."

## User Scenarios & Testing

### User Story 1 — Збір ground truth датасету (Priority: P1)

**Description**: Створити набір 50–100 токенів з підтвердженим інсайдерством (creator wallet, bundled accounts з судових розслідувань, публічні біджери) і 50–100 чистих токенів (великі ліквідні, відомі чесні пуллі, créаторські гаманці без зв'язків).

**Why this priority**: Без ground truth неможливо виміряти якість детектора; MELT не підходить (ціновий сигнал).

**Independent Test**: JSONL файл `ground_truth.jsonl` з полями: `mint`, `class` (insider|clean), `source` (creator_wallet|bundled_accounts|court_filings|exchange_listing|manual_review), `notes`. Скрипт валідації перевіряє: всі minти існують, класи розпізнані, джерела з дозволеного списку.

**Acceptance Scenarios**:
1. **Given** публічні дані про creator wallet Pump.fun (`39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg`) і пов'язані bundled accounts з Solidus Labs, **When** зібрано 30+ токенів, **Then** `ground_truth.jsonl` містить їх з `class=insider`, `source=bundled_accounts|court_filings`.
2. **Given** список топ-20 токенів за ліквідністю на Raydium (SOL, USDC, BONK, JUP, WIF, POPCAT тощо), **When** перевірено відсутність зв'язків з creator/bundled, **Then** додано до `ground_truth.jsonl` як `class=clean`, `source=exchange_listing|manual_review`.

---

### User Story 2 — Бенчмарк-скрипт (Priority: P1)

**Description**: Офлайн-команда `python -m unmask.benchmark --ground-truth path/to/ground_truth.jsonl --fixtures path/to/fixtures --output report.md`, що проганяє конвеєр 001→002→003 на фікстурах (або живий збір, прапорець `--live`) і виводить:
- Confusion matrix по смугах (clean/suspicious/high_concentration/insufficient_data vs insider/clean)
- Precision/Recall/F1 по кластерах (токен має кластер ⇔ insider)
- Calibration curve: `risk_score` bin → P(insider|bin)
- PR-AUC, ROC-AUC
- Таблиця false positives (чисті токени з кластерами) і false negatives (інсайдерські без кластерів)
- Latency stats (p50/p95 cold/hot)

**Why this priority**: Кількісна оцінка якості — єдиний спосіб обґрунтувати пороги перед релізом.

**Independent Test**: На фікстурах `tests/fixtures/real/` (9 токенів) + синтетичні дані скрипт запускається без помилок і генерує Markdown з усіма секціями.

**Acceptance Scenarios**:
1. **Given** ground truth з 2 класами, **When** запуск на фікстурах, **Then** вивід містить confusion matrix, PR-AUC, таблиці FP/FN.
2. **Given** прапорець `--live`, **When** збираються 10 нових токенів, **Then** результати додаються до звіту, живі RPC виклики логуються.

---

### User Story 3 — Калібрування порогів (Priority: P2)

**Description**: На основі бенчмарку вибрати `band_clean_max`, `band_suspicious_max` (clusters.yaml) так, щоб:
- `precision@high_concentration` ≥ 0.8 (коли ми кажемо high_concentration — це дійсно інсайдери)
- `recall@insider` ≥ 0.7 (знаходимо більшість інсайдерів)
- `false_positive_rate@clean` ≤ 0.15 (чисті токени рідко мають кластери)

Пороги записуються у `config/clusters.yaml` v2 з записом у `config/CHANGELOG.md` (принцип III).

**Why this priority**: Поточні пороги калібровані на MELT (ціновий сигнал), а не на ground truth фінансової координації.

**Independent Test**: Порівняння метрик до/після калібрування на тому ж ground truth показує покращення.

---

## Requirements

### Functional Requirements

- **FR-005-01**: Система MUST надавати офлайн бенчмарк-команду, що приймає ground truth JSONL і фікстури/живий збір.
- **FR-005-02**: Ground truth формат MUST бути версіонованим (`version: 1`), з полями `mint`, `class` (insider|clean), `source` (enum), `notes`.
- **FR-005-03**: Бенчмарк MUST виводити confusion matrix, precision/recall/F1 по кластерах, calibration curve, PR-AUC, ROC-AUC, таблиці FP/FN.
- **FR-005-04**: Калібрування порогів MUST базуватися на ground truth метриках, а не на MELT; нові пороги MUST записуватися у версіонований YAML з changelog.
- **FR-005-05**: Бенчмарк MUST підтримувати `--live` режим (живий збір нових токенів) і `--fixtures` режим (офлайн на записаних даних).
- **FR-005-06**: Всі константи, що впливають на метрики, MUST бути в YAML (clusters.yaml v2), не в коді (принцип III).

### Non-Functional Requirements

- **NFR-005-01**: Бенчмарк на 100 токенах (офлайн фікстури) MUST завершуватися < 300 с.
- **NFR-005-02**: Живий збір одного токена MUST логувати RPC calls і latency; загальний час `--live 10` ≤ 20 хв.

---

## Key Entities

- **Ground Truth Record**: `{mint, class: insider|clean, source: enum, notes}` — версія 1, JSONL.
- **Benchmark Report**: Markdown з таблицями метрик, calibration plot (ASCII або шлях до PNG), latency stats.
- **Calibrated Config**: `clusters.yaml` v2 з новими `band_clean_max`, `band_suspicious_max` і changelog записом.

---

## Success Criteria

### Measurable Outcomes

- **SC-001**: Ground truth файл містить ≥ 50 insider + ≥ 50 clean токенів з публічно верифікованими джерелами.
- **SC-002**: Бенчмарк на ground truth видає PR-AUC ≥ 0.75, ROC-AUC ≥ 0.80.
- **SC-003**: Після калібрування: `precision@high_concentration` ≥ 0.8, `recall@insider` ≥ 0.7, `FPR@clean` ≤ 0.15.
- **SC-004**: `config/clusters.yaml` v2 записана з changelog; старі пороги заархівовані.
- **SC-005**: Бенчмарк-скрипт працює в CI (офлайн на фікстурах) і локально (`--live`).

---

## Assumptions

- **Публічні дані**: creator wallet Pump.fun, bundled accounts з Solidus Labs / Carnahan v. Baton (5000+ чатів), список exchange hot wallets (Solscan, SolanaFM), відомі чесні пуллі (Bonfire, Solerium, інше).
- **Живий збір**: потребує Helius ключ (UNMASK_RPC_URL_ALT) і бюджету часу 600 с/токен.
- **Фікстури**: для CI використовуємо існуючі `tests/fixtures/real/` (9 токенів) + синтетичні генератори.
- **Поза обсягом**: автоматичне завантаження ground truth з вебу (ручний процес), ML-реранкинг, постійний моніторинг.

---

## Out of Scope

- Навчання ML моделі (тільки калібрування порогів евристик).
- Веб-інтерфейс для ground truth (файл JSONL).
- Автоматичне оновлення ground truth (ручне cura­torство).