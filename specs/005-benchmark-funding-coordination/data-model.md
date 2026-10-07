# Data Model: Бенчмарк фінансової координації (005)

## Ground Truth Record (v1)

Файл: `data/ground_truth.jsonl` — по рядку JSON об'єкт.

```json
{
  "version": 1,
  "mint": "AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump",
  "class": "insider",
  "source": "bundled_accounts",
  "notes": "Pump.fun creator wallet 39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg; Solidus Labs bundled accounts"
}
```

### Поля

| Поле | Тип | Обов'язкове | Опис |
|------|-----|-------------|------|
| `version` | integer | так | Версія схеми ground truth (початкова = 1) |
| `mint` | string (base58, 32-44 chars) | так | Адреса токена (mint) |
| `class` | enum: `insider` \| `clean` | так | Ground truth клас |
| `source` | enum | так | Джерело мітки (див. нижче) |
| `notes` | string | ні | Додатковий контекст (URL, посилання на судовий матеріал, адреси creator wallet) |

### Enum `source`

| Значення | Опис |
|----------|------|
| `creator_wallet` | Прямий зв'язок з creator wallet Pump.fun (`39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg`) |
| `bundled_accounts` | Bundled accounts з досліджень (Solidus Labs, Chainalysis, Carnahan v. Baton чати) |
| `court_filings` | Судові документи (Carnahan v. Baton Corp, S.D.N.Y. 1:25-cv-00490/00880) |
| `exchange_listing` | Токен лістовий на великіх CEX/DEX, відомий як ліквідний/чесний |
| `manual_review` | Ручна перевірка експертом (потрібен `notes` з обґрунтуванням) |

### Приклади

**Insider (creator wallet):**
```json
{"version":1,"mint":"AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump","class":"insider","source":"creator_wallet","notes":"First buyer from creator wallet 39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"}
```

**Insider (bundled accounts):**
```json
{"version":1,"mint":"3vgBAf4c6TKTfBgZwrVhnJeqN3NuFpkRjWyUVHvvpump","class":"insider","source":"bundled_accounts","notes":"Solidus Labs: bundled accounts 384axe7w, 3WuDgGNh, 8Sb3H45q"}
```

**Clean (exchange listing):**
```json
{"version":1,"mint":"So11111111111111111111111111111111111111112","class":"clean","source":"exchange_listing","notes":"Wrapped SOL, major CEX/DEX listing"}
```

**Clean (manual review):**
```json
{"version":1,"mint":"BONK...","class":"clean","source":"manual_review","notes":"Large community launch, no creator wallet links found after review"}
```

---

## Prediction Record (внутрішня структура бенчмарку)

```python
@dataclass
class Prediction:
    mint: str
    pred_class: str          # "has_cluster" | "no_cluster"  (або band: clean/suspicious/high_concentration/insufficient_data)
    risk_score: int          # 0-100
    band: str                # clean | suspicious | high_concentration | insufficient_data
    clusters_count: int
    max_share: float         # largest cluster supply_share
    coordination_category: str  # none | weak | moderate | strong | high_concentration
```

---

## Metrics Output (звіт)

### Confusion Matrix (по смугах)

| GT \ Pred | clean | suspicious | high_concentration | insufficient_data |
|-----------|-------|------------|-------------------|-------------------|
| insider   | FN    | TP         | TP                | FN                |
| clean     | TN    | FP         | FP                | TN                |

*Примітка: `insufficient_data` трактується як «no cluster» для бинарної класифікації.*

### Precision / Recall / F1 (по кластерах)

- `has_cluster` ⇔ `clusters_count > 0` (або `band != clean and band != insufficient_data`)
- `precision = TP / (TP + FP)`
- `recall = TP / (TP + FN)`
- `f1 = 2 * precision * recall / (precision + recall)`

### Calibration Curve

Біни по `risk_score` (0-10, 10-20, ..., 90-100):
- `bin_center`, `count`, `insider_rate` (P(insider|bin)), `avg_risk_score`

### PR-AUC / ROC-AUC

- `risk_score` як continuous score для `class=insider`
- PR-AUC (precision-recall) — головна метрика для незбалансованих класів
- ROC-AUC — допоміжна

### FP / FN Tables

| mint | gt_class | pred_band | risk_score | clusters | max_share | notes |
|------|----------|-----------|------------|----------|-----------|-------|

---

## Calibration Output

```json
{
  "band_clean_max": 20,
  "band_suspicious_max": 50,
  "metrics_at_optimal": {
    "precision_high_concentration": 0.82,
    "recall_insider": 0.71,
    "fpr_clean": 0.12,
    "f1": 0.76
  },
  "grid_search": [
    {"band_clean_max": 15, "band_suspicious_max": 40, "precision_hc": 0.75, "recall_ins": 0.80, "fpr_clean": 0.18},
    ...
  ]
}
```

---

## Report Markdown Structure

```markdown
# Benchmark Report (005)

config: clusters.yaml v2, hubs.yaml v3, ingest.yaml v3, address_lists v1
ground_truth: data/ground_truth.jsonl v1 (N=XX insider, YY clean)
fixtures: tests/fixtures/real/ (ingest v2, N=30, depth=2, cap=30)
date: 2026-10-07

## Confusion Matrix (by band)
| GT \ Pred | clean | suspicious | high_concentration | insufficient_data |
|---|---|---|---|---|
| insider | 2 | 3 | 5 | 0 |
| clean | 8 | 1 | 0 | 1 |

## Precision / Recall / F1 (cluster detection)
| metric | value |
|---|---|
| precision | 0.73 |
| recall | 0.80 |
| f1 | 0.76 |

## Calibration Curve (risk_score bins)
| bin | count | insider_rate | avg_risk |
|---|---|---|---|
| 0-10 | 15 | 0.07 | 3.2 |
| 10-20 | 12 | 0.25 | 14.1 |
| ... | ... | ... | ... |

## PR-AUC: 0.78 | ROC-AUC: 0.85

## False Positives (clean tokens with clusters)
| mint | pred_band | risk_score | clusters | max_share | evidence_types |
|---|---|---|---|---|---|

## False Negatives (insider tokens without clusters)
| mint | gt_source | pred_band | risk_score | notes |
|---|---|---|---|---|

## Latency Stats
| mode | p50 | p95 | max |
|---|---|---|---|
| cold | 45s | 120s | 280s |
| hot (cache) | 0.02s | 0.05s | 0.1s |

## Calibrated Thresholds
| threshold | old | new | rationale |
|---|---|---|---|
| band_clean_max | 20 | 18 | precision@high_concentration ↑ |
| band_suspicious_max | 50 | 48 | recall@insider ↑, FPR@clean ↓ |
```