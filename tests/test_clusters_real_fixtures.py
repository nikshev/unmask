# verifies: FR-003-23
"""Реальні результати збору як фікстури: маніфест, sha256, валідність (T-072)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

REAL = Path(__file__).parent / "fixtures" / "real"
MANIFEST = REAL / "manifest.yaml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_has_nine_tokens_in_order_with_classes() -> None:
    manifest = _manifest()
    assert [t["label"] for t in manifest["tokens"]] == [
        "ins0", "ins1", "ins2", "ins3", "ins4", "cln1", "cln2", "cln3", "cln4"]
    assert [t["class"] for t in manifest["tokens"][:5]] == ["insider"] * 5
    assert [t["class"] for t in manifest["tokens"][5:]] == ["clean"] * 4
    assert manifest["collection"]["delegated_analyzed"] is False


def test_files_match_manifest_sha256_byte_for_byte() -> None:
    manifest = _manifest()
    for token in manifest["tokens"]:
        path = REAL / token["file"]
        assert path.is_file(), token["file"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == token["sha256"], token["file"]


def test_results_are_valid_ingest_documents_without_secrets() -> None:
    from unmask.ingest.model import IngestResult
    from unmask.ingest.serialize import from_dict
    manifest = _manifest()
    for token in manifest["tokens"]:
        data = json.loads((REAL / token["file"]).read_text(encoding="utf-8"))
        assert data["metadata"]["mint"] == token["mint"]
        result = from_dict(data)
        assert isinstance(result, IngestResult)
        text = (REAL / token["file"]).read_text(encoding="utf-8").lower()
        for secret in ("api_key", "apikey", "secret", "private_key", "rpc_key"):
            assert secret not in text


def test_total_bytes_and_no_extra_result_files() -> None:
    result_files = sorted(p for p in REAL.iterdir()
                          if p.name.startswith("result_") and p.suffix == ".json")
    assert [p.name for p in result_files] == sorted(
        t["file"] for t in _manifest()["tokens"])
    total = sum(p.stat().st_size for p in result_files)
    assert total == 1144228
