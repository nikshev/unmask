# Implementation Plan: Бенчмарк фінансової координації (005)

**Created**: 2026-10-07
**Based on**: `spec.md`, `data-model.md`, `contracts/ground-truth.schema.json`

## Overview

Фіча 005 додає офлайн бенчмарк-інструмент для кількісної оцінки детектора фінансової координації за ground truth (не MELT). Включає: ground truth датасет, bench-скрипт, калібрування порогів clusters.yaml.

## Architecture

```
src/unmask/benchmark/
  __init__.py          # trace: ignore-file
  ground_truth.py      # завантаження/валідація JSONL
  runner.py            # прогін конвеєра 001→002→003, збір метрик
  metrics.py           # confusion matrix, PR/ROC-AUC, calibration
  calibrate.py         # підбір порогів bands
  cli.py               # CLI: --ground-truth, --fixtures, --live, --output
  report.py            # Markdown генерація
```

Залежності: лише stdlib + `sklearn.metrics` (або власна реалізація AUC, щоб не тягнути sklearn).

## Phase 1: Ground Truth Data & Schema

**Goal**: JSONL схема, приклад файлу з 10+ токенами, валідатор.

- [ ] T-100 [FR-005-02] [P] Schema + loader. `contracts/ground-truth.schema.json` (JSON Schema draft 2020-12), `src/unmask/benchmark/ground_truth.py::load_ground_truth(path) -> list[GroundTruthRecord]`, валідація enum полів, перевірка base58 mint. Тест: `tests/test_benchmark_ground_truth.py::test_schema_rejects_invalid`, `::test_loader_returns_typed_records`.
- [ ] T-101 [FR-005-01] [P] Ground truth файл. `data/ground_truth.jsonl` v1 — мінімум 10 insider (creator wallet, bundled accounts з Solidus) + 10 clean (SOL, USDC, BONK, JUP, WIF, POPCAT, RAY, ORCA, MNDE, HNT). Запис у `config/CHANGELOG.md` розділ `# data/ground_truth.jsonl`. Тест: `tests/test_benchmark_ground_truth.py::test_ground_truth_file_passes_validation`.

## Phase 2: Benchmark Runner (Offline)

**Goal**: Прогін 001→002→003 на фікстурах, збір предікшн/ground truth пар.

- [ ] T-102 [FR-005-01, FR-005-05] critical: Runner. `src/unmask/benchmark/runner.py::run_benchmark(ground_truth, fixtures_dir, *, live=False, rpc_source=None) -> list[Prediction]`. Використовує `DeliveryService` (T-090 004). Для кожного mint: `service.analyze(mint)` → `Prediction(mint, pred_class, risk_score, band, clusters_count, max_share)`. `live=True` — живий `HttpRpcSource` + `IngestService` (потребує env keys). Тест: `tests/test_benchmark_runner.py::test_offline_on_fixtures_matches_ground_truth_shape`, `::test_live_smoke_one_token` (маркер `live`, пропускається в CI).
- [ ] T-103 [FR-005-03] [P] Metrics. `src/unmask/benchmark/metrics.py`: `confusion_matrix(pred, gt, bands)`, `precision_recall_f1(pred_clusters, gt_class)`, `calibration_curve(risk_scores, gt, n_bins=10)`, `pr_auc`, `roc_auc`, `fp_fn_tables(pred, gt)`. Власна реалізація AUC (trapezoidal rule), без sklearn. Тест: `tests/test_benchmark_metrics.py::test_confusion_matrix_on_known_data`, `::test_calibration_curve_monotonic`, `::test_pr_auc_matches_sklearn_on_synthetic` (порівняння з sklearn якщо є).

## Phase 3: Calibration

**Goal**: Підбір `band_clean_max`, `band_suspicious_max` у `clusters.yaml` v2.

- [ ] T-104 [FR-005-04] critical: Calibration. `src/unmask/benchmark/calibrate.py::calibrate_bands(ground_truth, fixtures_dir) -> dict`. Grid search по `band_clean_max ∈ [10,15,20,25,30]`, `band_suspicious_max ∈ [30,40,50,60,70]`. Цільова функція: `precision@high_concentration ≥ 0.8` AND `recall@insider ≥ 0.7` AND `FPR@clean ≤ 0.15`. Якщо кілька — максимізує F1. Повертає оптимальні пороги + метрики. Запис у `config/clusters.yaml` v2 + `config/CHANGELOG.md`. Тест: `tests/test_benchmark_calibrate.py::test_calibration_improves_metrics_on_synthetic`, `::test_output_yaml_version_incremented`.

## Phase 4: CLI & Report

- [ ] T-105 [FR-005-01] CLI. `src/unmask/benchmark/cli.py::main(argv)`: `--ground-truth PATH`, `--fixtures PATH`, `--live`, `--output PATH.md`, `--verbose`. `scripts/benchmark.py` (`# trace: ignore-file`) — `sys.exit(main())`. Тест: `tests/test_benchmark_cli.py::test_cli_offline_generates_report`, `::test_cli_help`.
- [ ] T-106 [FR-005-03] Report. `src/unmask/benchmark/report.py::generate_report(metrics, ground_truth, config_versions) -> str`. Markdown: заголовок з версіями конфігів, confusion matrix, precision/recall/F1 таблиця, calibration plot (ASCII bar chart або шлях до PNG якщо matplotlib є), PR-AUC/ROC-AUC, FP/FN таблиці, latency stats. Тест: `tests/test_benchmark_report.py::test_report_contains_all_sections`, `::test_calibration_plot_ascii`.

## Phase 5: Integration & Gates

- [ ] T-107 [FR-005-04] Config version bump. `config/clusters.yaml` v2 (новий `band_clean_max`, `band_suspicious_max`), `config/CHANGELOG.md` запис. Тест: `tests/test_config.py::test_clusters_yaml_v2_changelog_digest_matches`.
- [ ] T-108 [FR-005-06] Constants in YAML. Перевірка: у `calibrate.py`/`report.py`/`metrics.py` жодних числових літералів для порогів — лише з конфігу. Тест: `tests/test_benchmark_boundaries.py::test_no_hardcoded_thresholds` (grep по коду).
- [ ] T-109 [FR-005-06] E2E. `tests/test_benchmark_e2e.py::test_full_pipeline_offline_generates_report` (фікстури 9 токенів + ground truth 9 токенів → report.md валіден).
- [ ] T-110 [FR-005-02] Gate: `uv run pytest tests/test_benchmark_*.py -q` зелений; `uv run python -m unmask.benchmark --ground-truth data/ground_truth.jsonl --fixtures tests/fixtures/real --output /tmp/report.md` генерує валідний Markdown.

## Dependencies

| Task | Depends On |
|------|------------|
| T-100 | — |
| T-101 | T-100 |
| T-102 | T-101, 004 (DeliveryService) |
| T-103 | T-102 |
| T-104 | T-102, T-103 |
| T-105 | T-102, T-103 |
| T-106 | T-103, T-104 |
| T-107 | T-104 |
| T-108 | T-104, T-106 |
| T-109 | T-105, T-106, T-107 |
| T-110 | T-109 |

## Parallel Opportunities

- T-100 ∥ T-101 (schema vs файл)
- T-103 ∥ T-102 (metrics vs runner — різні файли)
- T-105 ∥ T-104 (CLI vs calibration)

## MVP Gate

**Checkpoint**: офлайн прогін на `tests/fixtures/real/` + `data/ground_truth.jsonl` (якщо ≥9 токенів) генерує `report.md` з усіма секціями. `trace.py --check` зелений.