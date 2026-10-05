# verifies: FR-003-21
"""AST-тест меж модулів 003 (T-083): правило залежностей з `plan.md`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "unmask" / "clusters"

CORE = ["config", "model", "links", "indirect", "cluster", "behavior",
        "score", "service", "serialize", "evaluate"]

ALLOWED_UNMASK = {
    "unmask.graph.model",
    "unmask.ingest.model",
    "unmask.graph.service",
    "unmask.ingest.serialize",
    "unmask.hubs.config",
    "unmask.clusters.config",
    "unmask.clusters.model",
    "unmask.clusters.links",
    "unmask.clusters.indirect",
    "unmask.clusters.cluster",
    "unmask.clusters.behavior",
    "unmask.clusters.score",
    "unmask.clusters.service",
    "unmask.clusters.serialize",
}

FORBIDDEN_SUBSTRINGS = [
    "unmask.ingest.rpc",
    "unmask.ingest.collector",
    "unmask.ingest.buyers",
    "unmask.ingest.funding",
    "unmask.ingest.cache",
    "unmask.ingest.service",
    "unmask.ingest.budget",
    "unmask.ingest.parse",
    "unmask.ingest.purchases",
    "unmask.ingest.delegated",
    "unmask.ingest.addresses",
    "unmask.ingest.config",
    "unmask.hubs.criteria",
    "unmask.hubs.prune",
    "unmask.hubs.report",
    "unmask.graph.build",
    "unmask.graph.measures",
    "unmask.graph.components",
    "socket",
    "httpx",
    "requests",
    "urllib",
    "importlib",
]

# Модулі ядра (крім evaluate) — чисті функції: жодного читання файлів і годинника.
# config.py читає лише свій YAML; evaluate.py — вхід CLI (фікстури, маніфест, еталон).
_IO_CALLS = ("open", "read_text", "read_bytes", "write_text")
_CLOCK_ATTRS = ("perf_counter", "monotonic", "time", "now", "today", "utcnow")


def _imports(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _under(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_package_has_exactly_the_eleven_documented_modules() -> None:
    modules = sorted(p.stem for p in PKG.glob("*.py"))
    assert modules == ["__init__", "behavior", "cluster", "config", "evaluate",
                       "indirect", "links", "model", "score", "serialize", "service"]


@pytest.mark.parametrize("module", CORE)
def test_core_modules_import_only_allowed_modules(module: str) -> None:
    names = _imports(_under(PKG / f"{module}.py"))
    for name in names:
        if not name.startswith("unmask"):
            continue
        assert name in ALLOWED_UNMASK, (module, name)


def test_config_py_imports_from_hubs_config_subset_of_four_public_names() -> None:
    tree = _under(PKG / "config.py")
    from_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "unmask.hubs.config":
            from_names.extend(a.name for a in node.names)
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "unmask.hubs.config", "import module as a whole is forbidden"
    assert set(from_names) <= {"ConfigError", "content_digest", "changelog_entries", "check_changelog"}
    assert from_names, "config.py must use hubs.config public API"


def test_no_other_core_module_touches_hubs_config() -> None:
    for module in [m for m in CORE if m != "config" and m != "evaluate"]:
        names = _imports(_under(PKG / f"{module}.py"))
        assert not any(n == "unmask.hubs.config" or n.startswith("unmask.hubs") for n in names), module


def test_evaluate_may_additionally_import_graph_service_graph_serialize_load_hub_config_and_ingest_serialize_only() -> None:
    names = _imports(_under(PKG / "evaluate.py"))
    unmask_names = [n for n in names if n.startswith("unmask")]
    allowed = ALLOWED_UNMASK | {"unmask.graph.serialize"}
    for name in unmask_names:
        assert name in allowed, name
    assert "unmask.graph.service" in unmask_names
    assert "unmask.ingest.serialize" in unmask_names


@pytest.mark.parametrize("bad", FORBIDDEN_SUBSTRINGS)
def test_forbidden_everywhere(bad: str) -> None:
    for module in CORE:
        names = _imports(_under(PKG / f"{module}.py"))
        assert not any(n == bad or n.startswith(bad + ".") for n in names), (module, bad)


def test_core_has_no_file_io_or_clock() -> None:
    for module in CORE:
        if module in ("config", "evaluate"):
            continue  # config читає свій YAML; evaluate — вхід CLI
        tree = _under(PKG / f"{module}.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] not in ("time", "datetime"), (module, a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in ("time", "datetime"), (module, node.module)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else "")
                assert name not in _IO_CALLS, (module, name)
            elif isinstance(node, ast.Attribute):
                assert node.attr not in ("perf_counter", "perf_counter_ns", "monotonic",
                                         "monotonic_ns", "process_time", "now", "utcnow",
                                         "today", "sleep"), (module, node.attr)


def test_rule_catches_forbidden_imports(tmp_path: Path) -> None:
    bad1 = tmp_path / "bad1.py"
    bad1.write_text("from unmask.hubs import prune\n", encoding="utf-8")
    assert any("unmask.hubs.prune" in n or n == "unmask.hubs" for n in _imports(_under(bad1)))
    bad2 = tmp_path / "bad2.py"
    bad2.write_text("import socket\n", encoding="utf-8")
    assert "socket" in _imports(_under(bad2))
