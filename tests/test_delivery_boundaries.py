# verifies: FR-004-11
"""AST-тест меж пакета доставки (T-097): правило залежностей з `plan.md`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "unmask" / "delivery"

CORE = ["config", "report", "render", "cache", "service", "http", "bot", "main"]

ALLOWED_UNMASK = {
    "unmask.ingest.model",
    "unmask.ingest.config",
    "unmask.ingest.service",
    "unmask.ingest.rpc.http",
    "unmask.graph.model",
    "unmask.graph.service",
    "unmask.hubs.config",
    "unmask.clusters.model",
    "unmask.clusters.config",
    "unmask.clusters.service",
    "unmask.clusters.serialize",
    "unmask.delivery.config",
    "unmask.delivery.report",
    "unmask.delivery.render",
    "unmask.delivery.cache",
    "unmask.delivery.service",
    "unmask.delivery.http",
    "unmask.delivery.bot",
    "unmask.delivery.main",
}

FORBIDDEN_SUBSTRINGS = [
    "unmask.ingest.buyers",
    "unmask.ingest.funding",
    "unmask.ingest.cache",
    "unmask.ingest.collector",
    "unmask.ingest.parse",
    "unmask.ingest.purchases",
    "unmask.ingest.delegated",
    "unmask.ingest.addresses",
    "unmask.ingest.budget",
    "unmask.ingest.rpc.fixture",
    "unmask.ingest.rpc.protocol",
    "unmask.hubs.criteria",
    "unmask.hubs.prune",
    "unmask.hubs.report",
    "unmask.graph.build",
    "unmask.graph.measures",
    "unmask.graph.components",
    "socket",
    "requests",
    "urllib.request",
    "urllib3",
    "importlib",
]


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


def test_package_has_exactly_the_documented_modules() -> None:
    modules = sorted(p.stem for p in PKG.glob("*.py"))
    assert modules == ["__init__", "bot", "cache", "config", "http", "main", "render",
                       "report", "service"]


@pytest.mark.parametrize("module", CORE)
def test_core_imports_only_allowed_modules(module: str) -> None:
    names = _imports(_under(PKG / f"{module}.py"))
    for name in names:
        if not name.startswith("unmask"):
            continue
        assert name in ALLOWED_UNMASK, (module, name)


def test_httpx_only_in_bot_pil_only_in_render_yaml_only_in_config() -> None:
    for module in CORE:
        names = _imports(_under(PKG / f"{module}.py"))
        if module != "bot":
            assert "httpx" not in names, module
        if module != "render":
            assert not any(n == "PIL" or n.startswith("PIL.") for n in names), module
        if module != "config":
            assert "yaml" not in names, module
    assert "httpx" in _imports(_under(PKG / "bot.py"))
    assert "PIL" in _imports(_under(PKG / "render.py")) or \
        any(n.startswith("PIL") for n in _imports(_under(PKG / "render.py")))


def test_os_environ_only_in_main() -> None:
    for module in CORE:
        tree = _under(PKG / f"{module}.py")
        uses_env = any(isinstance(n, ast.Attribute) and n.attr == "environ"
                       for n in ast.walk(tree))
        if module != "main":
            assert not uses_env, module
    assert any(isinstance(n, ast.Attribute) and n.attr == "environ"
               for n in ast.walk(_under(PKG / "main.py")))


@pytest.mark.parametrize("bad", FORBIDDEN_SUBSTRINGS)
def test_forbidden_everywhere(bad: str) -> None:
    for module in CORE:
        names = _imports(_under(PKG / f"{module}.py"))
        assert not any(n == bad or n.startswith(bad + ".") for n in names), (module, bad)
    # urllib.parse — розбір шляху без мережі, дозволено лише в http.py:
    for module in CORE:
        names = _imports(_under(PKG / f"{module}.py"))
        if module != "http":
            assert "urllib.parse" not in names, module


def test_no_recompute_imports_in_report_service_cache_http_bot() -> None:
    for module in ("report", "service", "cache", "http", "bot"):
        names = _imports(_under(PKG / f"{module}.py"))
        for forbidden in ("math", "statistics", "random", "decimal", "fractions", "numpy"):
            assert forbidden not in names, (module, forbidden)


def test_rule_catches_forbidden_imports(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from unmask.hubs import prune\n", encoding="utf-8")
    assert any("unmask.hubs.prune" in n or n == "unmask.hubs" for n in _imports(_under(bad)))
