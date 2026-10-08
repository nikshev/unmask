# Quickstart: експеримент 30+30 (006)

## Offline (без ключа, без мережі — доступно зараз)

```bash
uv run pytest tests/test_cohort_lists.py -q   # T-122…T-125, синтетика
python3 scripts/cohort_check.py data/cohort_b.jsonl --expect-control-30
```

## Live (потрібні B1: pool mints, B2: UNMASK_RPC_URL, B3: підтверджений контроль)

```bash
export UNMASK_RPC_URL='https://…key…'
# 1. Screening пулу (2–10 год; маніфест у runs/<date>/, сирі JSON НЕ комітити)
uv run python scripts/benchmark.py --live --mints data/cohort_pool.jsonl \
  --ground-truth data/ground_truth.jsonl --output runs/<date>/screen.md
# 2. Відбір топ-30
python3 scripts/cohort_select.py runs/<date>/summaries.csv > data/cohort_a.jsonl
# 3. Контроль
uv run python scripts/benchmark.py --live --mints data/cohort_b.jsonl \
  --ground-truth data/ground_truth.jsonl --output runs/<date>/control.md
# 4. Зведення + порівняння (комітиться)
python3 scripts/cohort_compare.py summaries/ > /tmp/cohort_block.md
```

## Що комітиться (точний список для `git add`)

- `data/cohort_pool.jsonl`, `data/cohort_a.jsonl`, `data/cohort_b.jsonl`
- `summaries/cohort_a.csv`, `summaries/cohort_b.csv`
- `runs/<date>/manifest.json` (без сирих docs)
- `README.md` (блок з §4), `docs/traceability.md` (регенерується хуком)

НЕ комітиться: сирі per-token JSON, `UNMASK_RPC_URL`, будь-які ключі.
