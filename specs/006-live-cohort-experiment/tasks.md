# Tasks: Живий когортний експеримент 30+30 (006)

**Input**: `spec.md`, `plan.md`, `contracts/cohort.schema.json`, `quickstart.md`.

**Tests**: offline only (constitution). Live steps are manual gates, marked
`[GATE]` — never pytest. Each task carries ≥1 FR.

## Format

`- [ ] T-NNN [FR-006-NN, …] [GATE?] [BLOCKED BY?] critical?: опис — тест: …; файли: …`

- Numbering continues 005 → **T-122…T-130**.
- `critical:` — silent corruption of published numbers → `implementer-senior`.

---

## Phase 1: Lists + tooling (offline, unblocked)

- [x] T-122 [FR-006-01] [P] Cohort schema + validator. `contracts/cohort.schema.json`
  (draft 2020-12), `scripts/cohort_check.py` (`# trace: ignore-file`): validates
  version=1, enum cohort/source, base58 mint, pool ≥100 / control =30 on demand.
  Тест: `tests/test_cohort_lists.py::test_schema_rejects_bad_cohort` (синтетика);
  файли: `contracts/cohort.schema.json`, `scripts/cohort_check.py`,
  `tests/test_cohort_lists.py` (`# verifies: FR-006-01`).

- [x] T-123 [FR-006-04] [P] Control list v1. `data/cohort_b.jsonl` — 30 liquid mints
  (draft з plan.md, чекає B3-підтвердження;_until confirmed — `source: liquid_draft`).
  Тест: `tests/test_cohort_lists.py::test_control_list_has_30_disjoint_mints`;
  файли: `data/cohort_b.jsonl`.

- [x] T-124 [FR-006-03] Selection rule as code. `scripts/cohort_select.py`
  (`# trace: ignore-file`): stdin rows → top-30 (risk desc, clusters desc,
  max_share desc, mint asc) → `cohort_a.jsonl`. Тест:
  `tests/test_cohort_lists.py::test_selection_tiebreaks_deterministic` (синтетика з
  рівними скорами); мутація: прибрати tie-break → червоний; файли:
  `scripts/cohort_select.py`, `tests/test_cohort_lists.py`.

- [x] T-125 [FR-006-06, FR-006-07] critical: Comparison script. `scripts/cohort_compare.py`
  (`# trace: ignore-file`): читає `summaries/cohort_*.csv` → Markdown-блок
  (median/p25/p75 risk, share-with-cluster, evidence histogram, band split) +
  preregistered expectations table (met/unmet). Тест:
  `tests/test_cohort_lists.py::test_compare_output_matches_hand_computed` (синтетичні
  summaries 5+5 з відомими медіанами); мутація: медіана замість середнього →
  червоний; файли: `scripts/cohort_compare.py`, `tests/test_cohort_lists.py`.

---

## Phase 2: Live gates (BLOCKED — B1, B2, B3)

- [x] T-126 [FR-006-01, FR-006-02] [GATE] [BLOCKED BY B1] Pool list.
  Owner mint pool → `data/cohort_pool.jsonl` (≥100, `source: melt_high`) +
  `scripts/cohort_check.py` зелений. Ручна перевірка.
- [x] T-127 [FR-006-02, FR-006-05] [GATE] [BLOCKED BY B1, B2] Screening run.
  `scripts/benchmark.py --live --mints data/cohort_pool.jsonl --fixtures <out>`
  (профіль фікстур, конфіги v3/v3/v2); маніфест прогону в `runs/<date>/`;
  сирі JSON НЕ комітяться. Очікувано 2–10 год. Ручна перевірка.
- [x] T-128 [FR-006-03, FR-006-05] [GATE] Selection + control run.
  `cohort_select.py` → `data/cohort_a.jsonl` (30); контроль:
  `benchmark.py --live --mints data/cohort_b.jsonl`; зведення в
  `summaries/cohort_a.csv`, `summaries/cohort_b.csv`. Ручна перевірка.

## Phase 3: Publication (after live)

- [x] T-129 [FR-006-06, FR-006-07] critical: README section + numbers.
  `cohort_compare.py` → блок у README (замість/після MELT-секції) + caveats;
  числа побайтово з виходу скрипта (тест звіряє README-блок з виходом на
  закомічених summaries). Тест: `tests/test_cohort_lists.py::test_readme_numbers_match_compare_output`;
  файли: `README.md`, `summaries/`.
- [x] T-130 [FR-006-08] Gate. `uv run pytest tests/test_cohort_lists.py -q` зелений;
  `python3 scripts/trace.py --check` зелений; `git status` без сирих JSON/секретів.
  Ручна перевірка.

---

## MVP Gate

**Checkpoint**: T-122…T-125 зелені офлайн; T-126…T-128 чекають B1–B3; `trace.py --check` зелений.
