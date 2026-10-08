# verifies: FR-007-01
"""AST-межі пулу RPC (T-142, FR-007-01): `pool.py` тягне лише дозволені модулі, `pool_config.py` не бачить хаби.

Правило для `pool.py` — allowlist, а не blocklist: дозволені лише stdlib (список береться з
`sys.stdlib_module_names` у момент виклику, не з жорсткого переліку) і `unmask.ingest.rpc.protocol`,
`unmask.ingest.budget`. Перевіряються всі `import` / `from ... import` у файлі, включно з вкладеними й lazy
імпортами в функціях, а також рядкові аргументи `__import__` / `importlib.import_module`: динамічний імпорт
не обходить межу. Нелітеральний аргумент динамічного імпорту — порушення, бо його не можна перевірити.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

RPC = Path(__file__).resolve().parents[1] / "src" / "unmask" / "ingest" / "rpc"
POOL = RPC / "pool.py"
POOL_CONFIG = RPC / "pool_config.py"

POOL_ALLOWED_UNMASK = ("unmask.ingest.rpc.protocol", "unmask.ingest.budget")
POOL_FORBIDDEN = (
    "httpx",
    "yaml",
    "os",
    "unmask.ingest.rpc.http",
    "unmask.ingest.rpc.fixture",
    "unmask.graph",
    "unmask.hubs",
    "unmask.clusters",
    "unmask.delivery",
)
CONFIG_FORBIDDEN = ("unmask.hubs",)
DYNAMIC_IMPORTERS = frozenset({"__import__", "import_module"})


# --------------------------------------------------------------------------- AST-хелпер


def _under(name: str, prefix: str) -> bool:
    """`name` дорівнює `prefix` або лежить під ним як піпакет (границя по крапці)."""
    return name == prefix or name.startswith(prefix + ".")


def _is_dynamic_import(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in DYNAMIC_IMPORTERS
    if isinstance(func, ast.Attribute):
        return func.attr in DYNAMIC_IMPORTERS
    return False


def import_targets(tree: ast.AST) -> list[tuple[int, str]]:
    """Усі модулі, які тягне дерево: (рядок, повне імʼя). Обходить усе дерево, тож lazy-імпорти теж.

    `from m import a, b` дає `m.a` і `m.b`; голе `m` додається лише коли імпорт без псевдонімів-імен (`*`).
    Відносний імпорт повертається з крапками на початку — він ніколи не дозволений.
    """
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            if any(alias.name == "*" for alias in node.names) or not node.names:
                targets.append((node.lineno, base))
            else:
                joiner = "" if base.endswith(".") else "."
                targets.extend((node.lineno, f"{base}{joiner}{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call) and _is_dynamic_import(node):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                targets.append((node.lineno, arg.value))
            else:
                targets.append((node.lineno, "<динамічний імпорт з нелітеральним аргументом>"))
    return targets


def boundary_violations(
    source: str,
    *,
    forbidden: tuple[str, ...],
    allowed_unmask: tuple[str, ...] | None,
) -> list[str]:
    """Порушення межі в коді. `allowed_unmask is None` — дозволено все, крім `forbidden`.

    Інакше дозволені: stdlib (верхній рівень з `sys.stdlib_module_names`, читається тут, у момент виклику)
    і `unmask.*` під `allowed_unmask`. `forbidden` перевіряється першим: `os` — stdlib, але заборонений.
    """
    problems: list[str] = []
    for lineno, name in import_targets(ast.parse(source)):
        if any(_under(name, bad) for bad in forbidden):
            problems.append(f"L{lineno}: {name} (заборонено)")
        elif allowed_unmask is None:
            continue
        elif any(_under(name, ok) for ok in allowed_unmask):
            continue
        elif name.split(".")[0] in sys.stdlib_module_names:
            continue
        else:
            problems.append(f"L{lineno}: {name} (не в allowlist)")
    return problems


# --------------------------------------------------------------------------- межі


def test_pool_imports_only_protocol_and_budget() -> None:
    source = POOL.read_text(encoding="utf-8")
    names = {name for _, name in import_targets(ast.parse(source))}
    # не порожня перевірка: файл справді тягне дозволені модулі, а не нічого
    assert "unmask.ingest.budget.Clock" in names
    assert "unmask.ingest.rpc.protocol.RpcRateLimited" in names

    problems = boundary_violations(source, forbidden=POOL_FORBIDDEN, allowed_unmask=POOL_ALLOWED_UNMASK)
    assert problems == [], f"pool.py порушує межу FR-007-01: {problems}"


def test_pool_config_does_not_import_hubs() -> None:
    source = POOL_CONFIG.read_text(encoding="utf-8")
    problems = boundary_violations(source, forbidden=CONFIG_FORBIDDEN, allowed_unmask=None)
    assert problems == [], f"pool_config.py імпортує хаби: {problems}"


# --------------------------------------------------------------------------- самотест хелпера


SYNTHETIC_FORBIDDEN_IN_POOL = [
    pytest.param("def f():\n    import httpx\n", "httpx", id="lazy-httpx-in-function"),
    pytest.param("def f():\n    from unmask.delivery.limits import X\n", "unmask.delivery", id="lazy-delivery"),
    pytest.param("import yaml\n", "yaml", id="top-level-yaml"),
    pytest.param("import os\n", "os", id="stdlib-but-forbidden-os"),
    pytest.param("from os import path\n", "os", id="from-os-import"),
    pytest.param("import importlib\nimportlib.import_module('yaml')\n", "yaml", id="import-module-literal"),
    pytest.param("__import__('unmask.hubs.criteria')\n", "unmask.hubs", id="dunder-import-literal"),
    pytest.param("import importlib\nm = importlib.import_module(name)\n", "нелітеральн", id="import-module-dynamic"),
    pytest.param("from unmask.ingest.rpc import http\n", "unmask.ingest.rpc.http", id="from-rpc-import-http"),
    pytest.param("from unmask.graph.model import G\n", "unmask.graph", id="graph-model"),
    pytest.param("from solders.keypair import Keypair\n", "solders", id="third-party-not-in-allowlist"),
    pytest.param("from . import sibling\n", "sibling", id="relative-import"),
]


@pytest.mark.parametrize(("code", "expected"), SYNTHETIC_FORBIDDEN_IN_POOL)
def test_boundary_helper_catches_forbidden_import_in_synthetic_code(code: str, expected: str) -> None:
    problems = boundary_violations(code, forbidden=POOL_FORBIDDEN, allowed_unmask=POOL_ALLOWED_UNMASK)
    assert problems, f"хелпер пропустив порушення в: {code!r}"
    assert any(expected in p for p in problems), problems


def test_boundary_helper_accepts_allowed_imports_in_synthetic_code() -> None:
    code = (
        "from __future__ import annotations\n"
        "import math\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from unmask.ingest.budget import Clock\n"
        "from unmask.ingest import budget\n"
        "from unmask.ingest.rpc.protocol import RpcTimeout\n"
        "def f():\n"
        "    import threading\n"
    )
    assert boundary_violations(code, forbidden=POOL_FORBIDDEN, allowed_unmask=POOL_ALLOWED_UNMASK) == []


def test_boundary_helper_stdlib_list_is_read_from_sys_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Список stdlib — `sys.stdlib_module_names`, а не вбудований перелік: підміна джерела змінює вердикт.
    monkeypatch.setattr(sys, "stdlib_module_names", frozenset({"fakestd"}))
    assert boundary_violations("import fakestd\n", forbidden=(), allowed_unmask=()) == []
    assert boundary_violations("import json\n", forbidden=(), allowed_unmask=()) != []
