#!/usr/bin/env python3
"""Тести scripts/trace.py.

trace.py — примус для принципу I конституції. Валідатор, який ніколи не бачив
розірваного ланцюга, не доводить нічого, тому кожен клас порушення тут
відтворюється явно.

Запуск: python3 scripts/test_trace.py

trace: ignore-file — FR-ID нижче є тестовими даними, а не ланками ланцюга.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

# Ім'я не "trace": стандартна бібліотека вже займає його.
_spec = importlib.util.spec_from_file_location("cc_trace", Path(__file__).parent / "trace.py")
trace = importlib.util.module_from_spec(_spec)
sys.modules["cc_trace"] = trace  # @dataclass розвʼязує анотації через sys.modules
_spec.loader.exec_module(trace)


def build(root: Path, spec: str = "", tasks: str = "", code: str = "", test: str = "",
          extra: str = "", feature: str = "003-tron-ingest") -> None:
    (root / "specs" / feature).mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    if spec:
        (root / "specs" / feature / "spec.md").write_text(spec, encoding="utf-8")
    if tasks:
        (root / "specs" / feature / "tasks.md").write_text(tasks, encoding="utf-8")
    if code:
        (root / "src" / "ingest.rs").write_text(code, encoding="utf-8")
    if test:
        (root / "src" / "ingest_test.rs").write_text(test, encoding="utf-8")
    if extra:
        (root / "src" / "helper.rs").write_text(extra, encoding="utf-8")


def run(root: Path) -> int:
    trace.set_root(root)
    return trace.main(["--root", str(root)])


CASES: list[tuple[str, dict, int]] = [
    (
        "замкнений ланцюг проходить",
        dict(
            spec="- FR-003-07 Пагінація TronGrid",
            tasks="- [x] T-034 [FR-003-07] Реалізувати пагінацію",
            code="// impl: FR-003-07\nfn paginate() {}",
            test="// verifies: FR-003-07\nfn test_paginate() {}",
        ),
        0,
    ),
    (
        "вимога без задачі — падає",
        dict(spec="- FR-003-07 Пагінація TronGrid", tasks="# порожньо"),
        1,
    ),
    (
        "задача без посилання на вимогу — падає",
        dict(spec="- FR-003-07 Пагінація", tasks="- [ ] T-034 Реалізувати пагінацію"),
        1,
    ),
    (
        "задача на неіснуючу вимогу — падає",
        dict(spec="- FR-003-07 Пагінація", tasks="- [x] T-034 [FR-003-99] Щось"),
        1,
    ),
    (
        "виконана задача без коду — падає",
        dict(
            spec="- FR-003-07 Пагінація",
            tasks="- [x] T-034 [FR-003-07] Реалізувати",
            test="// verifies: FR-003-07",
        ),
        1,
    ),
    (
        "виконана задача без тесту — падає",
        dict(
            spec="- FR-003-07 Пагінація",
            tasks="- [x] T-034 [FR-003-07] Реалізувати",
            code="// impl: FR-003-07",
        ),
        1,
    ),
    (
        "спека без tasks.md — ще не порушення, фіча в плануванні",
        dict(spec="- FR-003-07 Пагінація TronGrid"),
        0,
    ),
    (
        "щойно зʼявився tasks.md — непокрита вимога знову порушення",
        dict(spec="- FR-003-07 Пагінація\n- FR-003-08 Ретраї",
             tasks="- [ ] T-034 [FR-003-07] Реалізувати"),
        1,
    ),
    (
        "невиконана задача коду ще не потребує",
        dict(spec="- FR-003-07 Пагінація", tasks="- [ ] T-034 [FR-003-07] Реалізувати"),
        0,
    ),
    (
        "проза зі згадкою задачі не парситься як задача",
        dict(spec="- FR-003-07 Пагінація",
             tasks="- [ ] T-034 [FR-003-07] Реалізувати\n\n"
                   "Граф: T-034 залежить від T-033 і блокує T-035."),
        0,
    ),
    (
        "пункт списку без чекбокса — падає",
        dict(spec="- FR-003-07 Пагінація",
             tasks="- [ ] T-034 [FR-003-07] Реалізувати\n"
                   "- T-035 [FR-003-07] Задача без статусу"),
        1,
    ),
    (
        "номер вимоги не збігається з номером фічі — падає",
        dict(spec="- FR-009-01 Чужа вимога", tasks="- [ ] T-034 [FR-009-01] Щось"),
        1,
    ),
    (
        "маркер на неіснуючу вимогу — падає",
        dict(
            spec="- FR-003-07 Пагінація",
            tasks="- [ ] T-034 [FR-003-07] Реалізувати",
            code="// impl: FR-003-88",
        ),
        1,
    ),
    (
        "код поза ланцюгом — падає, навіть коли решта замкнена",
        dict(
            spec="- FR-003-07 Пагінація",
            tasks="- [x] T-034 [FR-003-07] Реалізувати",
            code="// impl: FR-003-07\nfn paginate() {}",
            test="// verifies: FR-003-07\nfn t() {}",
            extra="fn helper() {}",
        ),
        1,
    ),
    (
        "ignore-file свідомо виводить файл із ланцюга",
        dict(
            spec="- FR-003-07 Пагінація",
            tasks="- [x] T-034 [FR-003-07] Реалізувати",
            code="// impl: FR-003-07\nfn paginate() {}",
            test="// verifies: FR-003-07\nfn t() {}",
            extra="// trace: ignore-file\nfn helper() {}",
        ),
        0,
    ),
    (
        "порожнє дерево проходить",
        dict(),
        0,
    ),
]


def main() -> int:
    failures = []
    for name, kwargs, expected in CASES:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build(root, **kwargs)
            actual = run(root)
        mark = "ok " if actual == expected else "FAIL"
        if actual != expected:
            failures.append(f"{name}: очікували exit={expected}, отримали {actual}")
        print(f"  [{mark}] {name}")

    if failures:
        print(f"\ntest_trace: {len(failures)} провалів", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\ntest_trace: ok — {len(CASES)} випадків")
    return 0


if __name__ == "__main__":
    sys.exit(main())
