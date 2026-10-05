# verifies: FR-004-01, FR-004-07, FR-004-08
"""Тести HTTP-шару без сокетів: маршрути, статуси, відсутність авторизації (T-092)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    jsonschema = None
    Draft202012Validator = None

from test_delivery_service import RecordedIngest, _mint_of, _real_services
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.http import handle_health, handle_token_request, route
from unmask.delivery.service import DeliveryService

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"
SCHEMA = json.loads((ROOT / "specs" / "004-api-bot-delivery" / "contracts"
                     / "api-response.schema.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")


def _service(mints: list[str]) -> DeliveryService:
    import yaml
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    files = {}
    for label in mints:
        token = next(t for t in manifest["tokens"] if t["label"] == label)
        files[token["mint"]] = REAL / token["file"]
    graph, clusters = _real_services()
    return DeliveryService(RecordedIngest(files), graph, clusters, DeliveryCache())


def test_token_endpoint_returns_schema_valid_document() -> None:
    mint, _ = _mint_of("ins4")
    service = _service(["ins4"])
    status, doc = handle_token_request(mint, service)
    assert status == 200
    Draft202012Validator(SCHEMA).validate(doc)
    assert doc["risk_score"] == 54 and len(doc["clusters"]) == 1
    assert all(len(c["evidence"]) >= 1 for c in doc["clusters"])


def test_invalid_mint_returns_4xx_with_explanation_not_stacktrace() -> None:
    from unmask.ingest.model import RejectKind, Rejection

    class Rejecting:
        def collect(self, mint: str):
            return Rejection(kind=RejectKind.INVALID_ADDRESS, mint=mint, detail="bad base58")

    graph, clusters = _real_services()
    service = DeliveryService(Rejecting(), graph, clusters, DeliveryCache())
    status, doc = handle_token_request("!!!", service)
    assert status == 400
    assert doc["error"]["kind"] == "invalid_address"
    assert "Traceback" not in json.dumps(doc)


def test_unknown_token_returns_4xx_rejection_mapped() -> None:
    from unmask.ingest.model import RejectKind, Rejection

    class Missing:
        def collect(self, mint: str):
            return Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=mint, detail="account_missing")

    graph, clusters = _real_services()
    service = DeliveryService(Missing(), graph, clusters, DeliveryCache())
    status, doc = handle_token_request("11111111111111111111111111111111", service)
    assert status == 400
    assert doc["error"]["kind"] == "token_not_found"


def test_healthz_ok() -> None:
    assert route("GET", "/healthz") == "health"
    assert handle_health() == (200, {"ok": True})


def test_no_auth_branch_exists() -> None:
    tree = ast.parse((ROOT / "src" / "unmask" / "delivery" / "http.py").read_text(encoding="utf-8"))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)):
            literals.append(str(node.value))
        elif isinstance(node, ast.Attribute):
            literals.append(node.attr)
    assert "401" not in literals and "403" not in literals
    assert not any("uthoriz" in lit for lit in literals)
    assert route("GET", "/api/token/ABC") == "token"
    assert route("GET", "/nope") is None


def test_wrong_method_returns_405() -> None:
    from unmask.delivery import http as http_module
    handler = http_module._Handler
    for method in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert getattr(handler, method) is handler._method_not_allowed
    assert "405" in ast.dump(ast.parse(
        (ROOT / "src" / "unmask" / "delivery" / "http.py").read_text(encoding="utf-8")))


def test_unexpected_pipeline_exception_returns_500_without_stacktrace() -> None:
    class Exploding:
        def analyze(self, mint: str):
            raise RuntimeError("defect")

    status, doc = handle_token_request("11111111111111111111111111111111", Exploding())
    assert status == 500
    assert doc["error"] == {"kind": "internal", "detail": "RuntimeError"}
    assert "Traceback" not in json.dumps(doc)
