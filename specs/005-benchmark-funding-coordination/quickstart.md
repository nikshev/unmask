# Quickstart: Бенчмарк фінансової координації (005)

## Офлайн прогін на фікстурах

```bash
# Запуск бенчмарку на фікстурах tests/fixtures/real/ (9 токенів)
uv run python -m unmask.benchmark \
  --ground-truth data/ground_truth.jsonl \
  --fixtures tests/fixtures/real \
  --output /tmp/benchmark_report.md

# Перегляд звіту
cat /tmp/benchmark_report.md
```

## Живий прогін (потрібні RPC ключі)

```bash
export UNMASK_RPC_URL='https://mainnet.helius-rpc.com/?api-key=...'
uv run python -m unmask.benchmark \
  --ground-truth data/ground_truth.jsonl \
  --live \
  --mints "MINT1,MINT2,MINT3" \
  --output /tmp/live_report.md
```

## Калібрування порогів

```bash
# Автоматичний підбір band_clean_max / band_suspicious_max
uv run python -m unmask.benchmark \
  --ground-truth data/ground_truth.jsonl \
  --fixtures tests/fixtures/real \
  --calibrate \
  --output /tmp/calibration_report.md
```

## Очікуваний вивід (offline на 9 токенах фікстур)

```
# Benchmark Report (005)

config: clusters.yaml v2, hubs.yaml v3, ingest.yaml v3, address_lists v1
ground_truth: data/ground_truth.jsonl v1 (N=5 insider, 4 clean)
fixtures: tests/fixtures/real/ (ingest v2, N=30, depth=2, cap=30)
date: 2026-10-07

## Confusion Matrix (by band)
| GT \ Pred | clean | suspicious | high_concentration | insufficient_data |
|---|---|---|---|---|
| insider | 0 | 2 | 3 | 0 |
| clean | 3 | 1 | 0 | 0 |

## Precision / Recall / F1 (cluster detection)
| metric | value |
|---|---|
| precision | 0.71 |
| recall | 1.00 |
| f1 | 0.83 |

## Calibration Curve
| bin | count | insider_rate | avg_risk |
|---|---|---|---|
| 0-10 | 2 | 0.00 | 3.5 |
| 10-20 | 1 | 1.00 | 16.0 |
| 20-30 | 2 | 1.00 | 22.5 |
| 30-40 | 1 | 0.00 | 32.0 |
| 40-50 | 2 | 0.50 | 44.5 |
| 50-60 | 1 | 1.00 | 54.0 |

## PR-AUC: 0.85 | ROC-AUC: 0.92

## False Positives
| mint | pred_band | risk_score | clusters | max_share |
|---|---|---|---|---|
| DwxtQ9nM... | suspicious | 10 | 1 | 0.21 |

## False Negatives
| mint | gt_source | pred_band | risk_score |
|---|---|---|---|
| (none) |  |  |  |

## Latency Stats
| mode | p50 | p95 | max |
|---|---|---|---|
| cold | 12s | 45s | 120s |
| hot | 0.01s | 0.03s | 0.05s |

## Calibrated Thresholds
| threshold | old | new | rationale |
|---|---|---|---|
| band_clean_max | 20 | 18 | precision@high_concentration ↑ |
| band_suspicious_max | 50 | 48 | recall@insider ↑ |
```

## Валідація ground truth файлу

```bash
# Перевірка сховища
uv run python -c "
from unmask.benchmark.ground_truth import load_ground_truth
records = load_ground_truth('data/ground_truth.jsonl')
print(f'Loaded {len(records)} records')
for r in records:
    print(f'  {r.mint[:10]} {r.class} {r.source}')
"
```

## Структура проєкту

```
src/unmask/benchmark/
  __init__.py
  ground_truth.py   # load_ground_truth, GroundTruthRecord
  runner.py         # run_benchmark
  metrics.py        # confusion_matrix, pr_auc, calibration_curve
  calibrate.py      # calibrate_bands
  cli.py            # main()
  report.py         # generate_report
scripts/benchmark.py  # entry point
data/ground_truth.jsonl  # v1, JSONL
```

## Тести

```bash
# Усі тести бенчмарку
uv run pytest tests/test_benchmark_*.py -q

# Конкретні
uv run pytest tests/test_benchmark_ground_truth.py -v
uv run pytest tests/test_benchmark_runner.py -v
uv run pytest tests/test_benchmark_metrics.py -v
uv run pytest tests/test_benchmark_calibrate.py -v
uv run pytest tests/test_benchmark_cli.py -v
uv run pytest tests/test_benchmark_report.py -v
uv run pytest tests/test_benchmark_e2e.py -v
```

## Traceability

```bash
uv run python3 scripts/trace.py --check
# має показати FR-005-01..06 з задачами T-100..T-110
```