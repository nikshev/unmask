# Plan: Живий когортний експеримент 30+30 (006)

**Branch**: `006-live-cohort-experiment` · **Spec**: `spec.md` (FR-006-01…08, SC-006-01…03)

## Architecture

Reuse, don't build: screening runs on the 005 runner (`scripts/benchmark.py
--live --mints ...`) with `DeliveryService` (004); selection is a deterministic
sort; comparison is an offline script over committed summaries. The only new
committed code is cohort-list validation + the comparison script. Live steps are
manual gates (constitution: tests never touch the network).

```
cohort pool (B1) ──▶ scripts/benchmark.py --live ──▶ per-token docs (NOT committed)
                                                        │ rank FR-006-03
                                                        ▼
cohort_a.jsonl (30, committed) + cohort_b.jsonl (30, committed)
                                                        │ offline analysis
                                                        ▼
summaries/ + README section (script-generated numbers only)
```

## Control list draft (B3 — owner to confirm)

SOL, USDC, BONK, JUP, WIF, POPCAT, RAY, ORCA, MOBILE, HNT, HONEY, PYTH, JTO,
WEN, MEW, SLERF, BOME, WIF-companions… — final 30 fixed in `cohort_b.jsonl`
v1; any substitution = new version + note (FR-006-04).

## Data model

- `contracts/cohort.schema.json`: `{version: 1, mint: base58, cohort:
  pool_a|a|b, source: melt_high|liquid|screen_rank, notes: string}`.
- `summaries/cohort_a.csv`, `summaries/cohort_b.csv`: FR-006-05 columns + rank.
- `runs/<date>/manifest.json`: command, config digests, start/end, per-token
  latency + rpc_calls (committed); raw docs excluded via `.gitignore`-style
  discipline (explicit `git add` of listed paths only).

## Time estimate (NFR-006-01)

| Step | Tokens | Per token | Wall total |
|---|---|---|---|
| Screen pool | ~120 | 60–300 s | 2–10 h |
| Cohort A top-30 rerun (cache warm) | 30 | ~0 s extra | — |
| Cohort B control | 30 | 60–300 s | 0.5–2.5 h |
| **Total live** | ~150 | | **2.5–12.5 h** |

Resume: ingest resume + warm `DeliveryCache` within a run; manifest checkpoints
every token, so an interrupted run continues from the next mint.

## Risks

| Risk | Mitigation |
|---|---|
| Pool mints too old / history pruned | record `token_not_found`/empty as data, keep denominator honest |
| Liquid giants exceed budget | fixture profile caps work (N=30, cap 30) — same as `tests/fixtures/real` |
| Selection overfits our own score | preregistered expectations + honest n=30 caveats (FR-006-06) |
| Raw data leaks into git | explicit add-list in quickstart; pre-commit hook runs trace only, so the runbook names exact paths |

## Phases

1. **Lists + tooling** (offline, unblocked): schema, control list, validation +
   comparison scripts with tests on synthetic summaries.
2. **Live gates** (blocked B1–B3): screening → selection → control run.
3. **Publication** (after live): summaries, README section, ground-truth
   extension proposal, gate.

## Control substitution (2026-10-07, measured — FR-006-04)

Draft v1 (30 mega-liquid: SOL/USDC/BONK/…) is UNSCANNABLE: signature-depth probe
on all 30 shows >100k lifetime signatures each (USDC 26k+ in the first 10 s alone,
never exhausted); first-buyer scan needs the oldest page, so every giant burns the
full 600 s budget with zero buyers (2 workers died silently on USDC/RAY first).
Replacement v2: 30 MELT `low` (price-clean) tokens, deterministic spread over the
1,878 low rows — same venue/era/size population as cohort A, differing only in the
price-manipulation label. Rationale recorded; owner may veto → new version + note.
