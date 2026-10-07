# verifies: FR-005-06
"""Перевірка відсутності захардкоджених порогів класифікації у коді бенчмарку (T-119).

Перевіряємо, що числові константи порогів класифікації (band_clean_max, band_suspicious_max)
не захардкоджені як літерали в коді класифікації, а читаються з конфігу YAML.
Grid search values, UI constants, default fallbacks — допустимі.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "src" / "unmask" / "benchmark"

# Файли, які слід перевіряти (крім тестів)
CHECKED_FILES = {
    "ground_truth.py",
    "runner.py",
    "metrics.py",
    "calibrate.py",
    "cli.py",
    "report.py",
}

# Контексти, які вказують на використання як поріг класифікації
THRESHOLD_CONTEXTS = [
    r"band_clean_max\s*[=:]\s*\d+",
    r"band_suspicious_max\s*[=:]\s*\d+",
    r"if\s+.*band_clean_max\s*[<>]=",
    r"if\s+.*band_suspicious_max\s*[<>]=",
    r"risk_score\s*[<>]=",
]


def _is_threshold_assignment(line: str) -> bool:
    """Перевірка, чи рядок містить призначення порогу класифікації."""
    for pattern in [
        r"band_clean_max\s*[=:]\s*\d+",
        r"band_suspicious_max\s*[=:]\s*\d+",
        r"if\s+.*band_clean_max\s*[<>]=",
        r"if\s+.*band_suspicious_max\s*[<>]=",
        r"risk_score\s*[<>]=",
    ]:
        if re.search(pattern, line):
            return True
    return False


def _scan_file(path: Path) -> list[str]:
    """Повертає список знайдених захардкоджених порогів класифікації у файлі."""
    text = path.read_text(encoding="utf-8")
    violations = []

    if path.name in {"test_benchmark_ground_truth.py", "test_benchmark_runner.py",
                      "test_benchmark_metrics.py", "test_benchmark_calibrate.py",
                      "test_benchmark_cli.py", "test_benchmark_report.py",
                      "test_benchmark_boundaries.py", "test_benchmark_e2e.py"}:
        return []

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if node.value in {20, 50, 18, 48}:
                line_no = node.lineno
                lines = text.splitlines()
                if 0 <= line_no - 1 < len(lines):
                    line = lines[line_no - 1].strip()
                    if line.startswith("#") or '"""' in line or "'''" in line:
                        continue
                    # Check if this is a threshold assignment context
                    if _is_threshold_assignment(line):
                        violations.append(f"{path}:{line_no}: hardcoded threshold literal {node.value} in: {line}")

    return violations


def test_no_hardcoded_thresholds_in_benchmark_code() -> None:
    """У коді бенчмарку (крім тестів) не має бути захардкоджених числових порогів класифікації."""
    all_violations = []
    for py_file in BENCHMARK_DIR.rglob("*.py"):
        if py_file.name.startswith("test_"):
            continue
        violations = _scan_file(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = "\n".join(all_violations)
        pytest.fail(f"Found hardcoded thresholds in benchmark code:\n{msg}")