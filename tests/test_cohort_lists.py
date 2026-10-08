# verifies: FR-006-01, FR-006-02, FR-006-03, FR-006-04, FR-006-05, FR-006-06, FR-006-07, FR-006-08
"""Тести тулінгу 006: схема списків, детермінований відбір, порівняння (T-122…T-125)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cohort_compare import compare
from cohort_select import select_top

ROOT = Path(__file__).resolve().parents[1]


def _row(mint: str, risk, clusters=0, share=0.0, band="clean", cat="weak") -> dict:
    return {"mint": mint, "risk_score": risk, "clusters_count": clusters,
            "max_share": share, "band": band, "coordination_category": cat}


def test_schema_rejects_bad_cohort(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"version": 1, "mint": "So11111111111111111111111111111111111111112",
                               "cohort": "zzz", "source": "melt_high"}) + "\n")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "cohort_check.py"),
                           str(bad)], capture_output=True, text=True)
    assert proc.returncode == 1 and "bad cohort" in proc.stderr


def test_schema_rejects_duplicate_mint(tmp_path: Path) -> None:
    dup = tmp_path / "dup.jsonl"
    row = {"version": 1, "mint": "So11111111111111111111111111111111111111112",
           "cohort": "b", "source": "liquid"}
    dup.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "cohort_check.py"),
                           str(dup)], capture_output=True, text=True)
    assert proc.returncode == 1 and "duplicate" in proc.stderr


def test_control_list_has_30_disjoint_mints() -> None:
    pool = {json.loads(l)["mint"] for l in
            (ROOT / "data" / "cohort_pool.jsonl").read_text().splitlines()}
    ctrl = [json.loads(l) for l in
            (ROOT / "data" / "cohort_b.jsonl").read_text().splitlines()]
    assert len(ctrl) == 30
    assert len({c["mint"] for c in ctrl}) == 30
    assert not ({c["mint"] for c in ctrl} & pool)


def test_selection_tiebreaks_deterministic() -> None:
    rows = [_row(f"MINT{i:04d}", 50, clusters=1, share=0.2) for i in range(35)]
    top = select_top(rows)
    assert [r["mint"] for r in top] == sorted(r["mint"] for r in rows)[:30]
    mixed = ([_row("HIGH", 90, 1, 0.5)] + [_row("MID", 50, 5, 0.9)]
             + [_row("LOW", 10, 0, 0.0)])
    assert [r["mint"] for r in select_top(mixed)] == ["HIGH", "MID", "LOW"]
    tie = [_row("B", 50, 1, 0.5), _row("A", 50, 1, 0.5)]
    assert [r["mint"] for r in select_top(tie)] == ["A", "B"]


def test_compare_output_matches_hand_computed() -> None:
    a = [_row(f"A{i}", r, 1 if r > 10 else 0, 0.3,
              "suspicious" if r > 10 else "clean",
              "moderate" if r > 10 else "weak")
         for i, r in enumerate([40, 30, 20, 5, 0])]
    b = [_row(f"B{i}", r, 0, 0.0, "clean", "weak")
         for i, r in enumerate([10, 5, 5, 0, 0])]
    out = compare("A", a, "B", b)
    assert "| risk_score median [p25–p75] | 20 [5–30] | 5 [0–5] |" in out
    assert "| tokens with ≥1 cluster | 3 (60.0%) | 0 (0.0%) |" in out
    assert "| median risk A > median risk B | met |" in out
    assert "| share-with-cluster A > share-with-cluster B | met |" in out


def test_readme_numbers_match_compare_output() -> None:
    """T-129: README-блок побайтово з виходу compare на закомічених summaries."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    begin = "<!-- cohort-compare:begin"
    end = "<!-- cohort-compare:end -->"
    assert begin in text and end in text
    block = text.split(begin, 1)[1].split(end, 1)[0]
    a = list(csv.DictReader(
        open(ROOT / "summaries" / "cohort_a.csv", newline="", encoding="utf-8")))
    b = list(csv.DictReader(
        open(ROOT / "summaries" / "cohort_b.csv", newline="", encoding="utf-8")))
    for line in compare("A", a, "B", b).splitlines():
        assert line in block, line


def test_offline_tooling_makes_no_network_calls() -> None:
    """FR-006-08: офлайн-тулінг без мережі; живі кроки — ручні гейти, не pytest."""
    import ast

    for script in ("cohort_check.py", "cohort_select.py", "cohort_compare.py"):
        tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
        assert not (imports & {"socket", "httpx", "urllib", "requests"}), script
    assert (ROOT / "summaries" / "cohort_a.csv").is_file()
    assert (ROOT / "summaries" / "cohort_b.csv").is_file()
    assert (ROOT / "runs" / "2026-10-07" / "manifest.json").is_file()


def test_screening_pins_fixture_profile() -> None:
    """FR-006-02: живий збір — профіль фікстур (N=30, depth=2, cap 30, без SPL)."""
    body = (ROOT / "scripts" / "screen_worker.py").read_text(encoding="utf-8")
    for pin in ("first_buyers_n=30", "max_signatures_per_wallet=30",
                "collect_spl_inbound=False", 'profile="helius_free"'):
        assert pin in body, pin


def test_committed_summaries_match_fr005_shape() -> None:
    """FR-006-05: зведення — рівні FR-006-05 поля, A: 30 з рангом, B: 30."""
    import csv as _csv

    with open(ROOT / "summaries" / "cohort_a.csv", newline="", encoding="utf-8") as f:
        a = list(_csv.DictReader(f))
    with open(ROOT / "summaries" / "cohort_b.csv", newline="", encoding="utf-8") as f:
        b = list(_csv.DictReader(f))
    need = {"mint", "risk_score", "band", "clusters_count", "max_share",
            "coordination_category", "wallets", "graph_status", "reasons",
            "elapsed_s", "error"}
    assert need <= set(a[0]) <= need | {"rank"} and len(a) == 30
    assert [r["rank"] for r in a] == [str(i) for i in range(1, 31)]
    assert need <= set(b[0]) and len(b) == 30
