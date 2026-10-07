# Tasks: Бенчмарк фінансової координації (005)

**Input**: `plan.md`, `spec.md`, `data-model.md`, `contracts/ground-truth.schema.json`, `quickstart.md`. Дедлайн — до подачі (11 жовтня) як supporting evidence.

**Tests**: обов'язкові й пишуться першими (принцип II). У кожній задачі названий тест(и), які мають бути червоними до коду. Мережі — лише в `--live` (пропускається в CI через маркер `live`); офлайн — на записаних фікстурах `tests/fixtures/real/`. Ground truth — `data/ground_truth.jsonl` (JSONL, схема `contracts/ground-truth.schema.json`). Еталон метрик — synthetic fixtures + знання очікуваних значень.

**Organization**: фаза 1 — ground truth схема/файл; фаза 2 — runner + метрики; фаза 3 — калібрування; фаза 4 — CLI/звіт; фаза 5 — інтеграція/гейти.

## Format

`- [ ] T-NNN [FR-005-NN, …] [USn?] [P?] critical?: опис — тест: …; мутація: …; файли: …`

- Нумерація `T-NNN` наскрізна: фічі 004 закінчилась на **T-110**; ця фічя — **T-111…T-121**.
- `[FR-…]` — кожна задача несе щонайменше одну вимогу.
- `[P]` — паралельно з сусідніми `[P]`.
- `critical:` — помилка тихо спотворює метрики/пороги → `implementer-senior`.

---

## Phase 1: Ground Truth Schema & Data

- [x] T-111 [FR-005-02] [P] JSON Schema + завантажувач. `contracts/ground-truth.schema.json` (draft 2020-12), `src/unmask/benchmark/ground_truth.py::load_ground_truth(path) -> list[GroundTruthRecord]` (frozen dataclass), валідація enum, base58 mint, версія=1. Тест: `tests/test_benchmark_ground_truth.py::test_schema_rejects_invalid`, `::test_loader_returns_typed_records`; мутація: змінити `class` на `insiderX` -> `ValidationError`; файли: `contracts/ground-truth.schema.json`, `src/unmask/benchmark/ground_truth.py` (`# impl: FR-005-02`), `tests/test_benchmark_ground_truth.py` (`# verifies: FR-005-02`).

- [x] T-112 [FR-005-01] [P] Ground truth файл v1. `data/ground_truth.jsonl` — 10 insider (creator_wallet=5, bundled_accounts=3, court_filings=2) + 10 clean (exchange_listing=6, manual_review=4). Джерела: creator wallet `39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg` + Solidus Labs bundled accounts (публічні адреси з репозиторію/статей), топ-10 ліквідних токенів за CoinGecko/DEXScreener. `config/CHANGELOG.md`: новий розділ `# data/ground_truth.jsonl` перед `# config/ingest.yaml`. Тест: `tests/test_benchmark_ground_truth.py::test_ground_truth_file_passes_validation` (валідація схеми + перевірка мінімум 10/10); файли: `data/ground_truth.jsonl`, `config/CHANGELOG.md`.

---

## Phase 2: Runner + Metrics (Offline)

- [x] T-113 [FR-005-01, FR-005-05] critical: Runner. `src/unmask/benchmark/runner.py::run_benchmark(ground_truth, fixtures_dir, *, live=False, rpc_source=None) -> list[Prediction]`. Використовує `DeliveryService` (004 T-090). `Prediction` dataclass: `mint, pred_has_cluster, risk_score, band, clusters_count, max_share, coordination_category`. `live=True` — створює `HttpRpcSource.from_env` + `IngestService` (потребує env keys). Тест: `tests/test_benchmark_runner.py::test_offline_on_fixtures_matches_ground_truth_shape`, `::test_prediction_has_all_fields`; мутація: підмінити `risk_score` у відповіді -> асерт на перевірці полів; файли: `src/unmask/benchmark/runner.py` (`# impl: FR-005-01, FR-005-05`), `tests/test_benchmark_runner.py` (`# verifies: FR-005-01, FR-005-05`).

- [x] T-114 [FR-005-03] [P] Metrics. `src/unmask/benchmark/metrics.py`:
  - `confusion_matrix(pred, gt, bands_order) -> ndarray`
  - `precision_recall_f1(pred_clusters, gt_classes) -> dict`
  - `calibration_curve(risk_scores, gt_labels, n_bins=10) -> list[dict]`
  - `pr_auc(scores, labels)`, `roc_auc(scores, labels)` — власна реалізація (trapezoidal rule), без sklearn
  - `fp_fn_tables(pred, gt) -> (list[dict], list[dict])`
  Всі функції pure, deterministic, тестовані на synthetic data. Тест: `tests/test_benchmark_metrics.py::test_confusion_matrix_on_known_data`, `::test_calibration_curve_monotonic`, `::test_pr_auc_matches_sklearn_on_synthetic` (якщо sklearn є — порівняння; інакше — еталонні значення); мутація: переставити pred/gt -> червоний; файли: `src/unmask/benchmark/metrics.py` (`# impl: FR-005-03`), `tests/test_benchmark_metrics.py` (`# verifies: FR-005-03`).

---

## Phase 3: Calibration

- [x] T-115 [FR-005-04] critical: Calibration. `src/unmask/benchmark/calibrate.py::calibrate_bands(ground_truth, fixtures_dir) -> dict`. Grid search:
  - `band_clean_max` in [10, 12, 15, 18, 20, 22, 25]
  - `band_suspicious_max` in [35, 40, 45, 48, 50, 55, 60]
  Цільова функція (lexicographic):
  1. `precision@high_concentration >= 0.8`
  2. `recall@insider >= 0.7`
  3. `fpr@clean <= 0.15`
  4. максимує `f1`
  Повертає: `{"band_clean_max": int, "band_suspicious_max": int, "metrics": {...}, "grid": [...]}`. Запис у `config/clusters.yaml` v2 (bump version) + `config/CHANGELOG.md` запис. Тест: `tests/test_benchmark_calibrate.py::test_calibration_improves_metrics_on_synthetic`, `::test_output_yaml_version_incremented`, `::test_changelog_entry_added`; мутація: зафіксувати suboptimal пороги -> асерт на метриках; файли: `src/unmask/benchmark/calibrate.py` (`# impl: FR-005-04`), `config/clusters.yaml`, `config/CHANGELOG.md`, `tests/test_benchmark_calibrate.py` (`# verifies: FR-005-04`).

---

## Phase 4: CLI & Report

- [x] T-116 [FR-005-01] CLI. `src/unmask/benchmark/cli.py::main(argv) -> int`:
  `--ground-truth PATH` (обов'язкове), `--fixtures PATH`, `--live`, `--mints CSV`, `--calibrate`, `--output PATH.md`, `--verbose`. `scripts/benchmark.py` (`# trace: ignore-file`) — `sys.exit(main())`. Тест: `tests/test_benchmark_cli.py::test_cli_offline_generates_report`, `::test_cli_help`, `::test_cli_live_flag_requires_env` (перевірка env keys); файли: `src/unmask/benchmark/cli.py` (`# impl: FR-005-01`), `scripts/benchmark.py` (`# trace: ignore-file`), `tests/test_benchmark_cli.py` (`# verifies: FR-005-01`).

- [x] T-117 [FR-005-03] Report. `src/unmask/benchmark/report.py::generate_report(metrics, ground_truth, config_versions) -> str`. Markdown: заголовок з версіями конфігів (ingest/hubs/hub_addresses/clusters + дайджести), confusion matrix, precision/recall/F1 таблиця, calibration plot (ASCII bar chart по біннах), PR-AUC/ROC-AUC, FP/FN таблиці, latency stats, calibrated thresholds таблиця. Тест: `tests/test_benchmark_report.py::test_report_contains_all_sections`, `::test_calibration_plot_ascii`, `::test_fp_fn_tables_formatted`; мутація: прибрати секцію FP -> червоний; файли: `src/unmask/benchmark/report.py` (`# impl: FR-005-03`), `tests/test_benchmark_report.py` (`# verifies: FR-005-03`).

---

## Phase 5: Integration & Gates

- [x] T-118 [FR-005-04] Config version bump. `config/clusters.yaml` v2 (новий `band_clean_max`, `band_suspicious_max` від калібрування), `config/CHANGELOG.md` запис. Тест: `tests/test_config.py::test_clusters_yaml_v2_changelog_digest_matches`; файли: `config/clusters.yaml`, `config/CHANGELOG.md`.

- [x] T-119 [FR-005-06] [P] Constants in YAML. AST/grep перевірка: у `calibrate.py`, `report.py`, `metrics.py`, `runner.py` жодних числових літералів для порогів — лише з конфігу. Тест: `tests/test_benchmark_boundaries.py::test_no_hardcoded_thresholds` (grep по коду); файли: `tests/test_benchmark_boundaries.py` (`# verifies: FR-005-06`).

- [x] T-120 [FR-005-06] E2E. `tests/test_benchmark_e2e.py::test_full_pipeline_offline_generates_report` (ground_truth 9 токенів з фікстур + fixtures_dir -> report.md валіден, містить всі секції quickstart). Тест: `tests/test_benchmark_e2e.py` (`# verifies: FR-005-01, FR-005-03, FR-005-04`).

- [x] T-121 [FR-005-02] Gate. `uv run pytest tests/test_benchmark_*.py -q` зелений; `uv run python -m unmask.benchmark --ground-truth data/ground_truth.jsonl --fixtures tests/fixtures/real --output /tmp/report.md` генерує валідний Markdown; `uv run python3 scripts/trace.py --check` зелений. Тест: ручна перевірка.

---

## Parallel Opportunities

- Task 111 || Task 112 (schema vs файл)
- Task 114 || Task 113 (metrics vs runner)
- Task 117 || Task 115 (CLI vs calibration)

## MVP Gate

**Checkpoint**: офлайн прогін на `tests/fixtures/real/` + `data/ground_truth.jsonl` (якщо >=9 токенів) генерує `report.md` з усіма секціями quickstart. `trace.py --check` зелений.
