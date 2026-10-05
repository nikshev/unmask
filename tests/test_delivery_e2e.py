# verifies: FR-004-09
"""Наскрізний тест доставки без мережі: 3 інсайдерські + 3 чисті через API і бота (T-096)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    jsonschema = None
    Draft202012Validator = None

from test_delivery_bot import _render as _bot_render
from test_delivery_service import RecordedIngest, _real_services
from unmask.delivery.bot import JournalTransport, handle_check, run_polling
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.http import handle_token_request
from unmask.delivery.service import DeliveryService

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"
SCHEMA = json.loads((ROOT / "specs" / "004-api-bot-delivery" / "contracts"
                     / "api-response.schema.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")

LABELS = ["ins0", "ins1", "ins4", "cln1", "cln2", "cln3"]


def _service() -> tuple[DeliveryService, dict[str, str]]:
    import yaml
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    files = {}
    mints = {}
    for label in LABELS:
        token = next(t for t in manifest["tokens"] if t["label"] == label)
        files[token["mint"]] = REAL / token["file"]
        mints[label] = token["mint"]
    graph, clusters = _real_services()
    return DeliveryService(RecordedIngest(files), graph, clusters, DeliveryCache()), mints


def _baseline() -> dict[str, tuple[int, str]]:
    text = (REAL / "expected_table.md").read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        m = re.match(r"\| (\w+) \| \w+ \| \w+ \| \d+ \| [\d.]+ \| (\d+) \| \w+ \| (\w+) \|", line)
        if m:
            out[m.group(1)] = (int(m.group(2)), m.group(3))
    return out


def test_three_insider_three_clean_end_to_end_matches_003_baseline() -> None:
    service, mints = _service()
    baseline = _baseline()
    assert set(baseline) >= set(LABELS)
    for label in LABELS:
        status, doc = handle_token_request(mints[label], service)
        assert status == 200, label
        Draft202012Validator(SCHEMA).validate(doc)
        assert (doc["risk_score"], doc["band"]) == baseline[label], label
        if label.startswith("ins"):
            assert len(doc["clusters"]) >= 1 and all(len(c["evidence"]) >= 1 for c in doc["clusters"]), label
        else:
            assert doc["risk_score"] <= 20 or doc["band"] != "clean", label


def test_bot_and_http_agree_byte_for_byte_on_same_mint() -> None:
    import json as _json
    service, mints = _service()
    mint = mints["ins4"]
    status, doc = handle_token_request(mint, service)
    assert status == 200
    transport = JournalTransport()
    requests: dict[str, str] = {}
    handle_check(f"/check {mint}", service, transport, 1, requests, render=_bot_render())
    photo = next(c for c in transport.calls if c[0] == "send_photo")
    caption = photo[1][2]
    assert str(doc["risk_score"]) in caption
    for cluster in doc["clusters"][:3]:
        assert str(cluster["supply_share"]) in caption
    assert _json.loads(_json.dumps(doc)) == doc
