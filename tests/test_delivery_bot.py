# verifies: FR-004-03, FR-004-04
"""Тести бота з несправжнім транспортом: /check, кнопка доказів, помилки (T-094)."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_delivery_service import RecordedIngest, _mint_of, _real_services
from unmask.delivery.bot import (
    JournalTransport,
    format_check,
    format_evidence,
    handle_check,
    handle_evidence,
    run_polling,
)
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.config import load_delivery_config
from unmask.delivery.render import render_png
from unmask.delivery.service import DeliveryService
from unmask.ingest.model import RejectKind, Rejection

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"


def _service(labels: list[str]) -> tuple[DeliveryService, dict]:
    import yaml
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    files = {}
    for label in labels:
        token = next(t for t in manifest["tokens"] if t["label"] == label)
        files[token["mint"]] = REAL / token["file"]
    graph, clusters = _real_services()
    return DeliveryService(RecordedIngest(files), graph, clusters, DeliveryCache()), files


def _render(cfg=None):
    delivery_cfg = cfg or load_delivery_config(ROOT / "config" / "delivery.yaml")
    return lambda doc: render_png(doc, delivery_cfg)


def _check_id(transport: JournalTransport) -> str:
    for name, args, kwargs in transport.calls:
        if name == "send_photo":
            return kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    raise AssertionError("no send_photo with evidence button")


def test_check_returns_photo_message_with_evidence_button() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    transport = JournalTransport()
    requests: dict[str, str] = {}
    request_id = handle_check(f"/check {mint}", service, transport, 123, requests, render=_render())
    assert request_id is not None
    photos = [c for c in transport.calls if c[0] == "send_photo"]
    assert len(photos) == 1
    _, (_, _, caption), kwargs = photos[0]
    assert "54/100" in caption and "висока концентрація" in caption
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["text"] == "докази"
    assert _check_id(transport) == f"evidence:{request_id}"


def test_evidence_callback_sends_full_proof_text() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    transport = JournalTransport()
    requests: dict[str, str] = {}
    handle_check(f"/check {mint}", service, transport, 123, requests, render=_render())
    callback_id = _check_id(transport)
    handle_evidence(callback_id, "q1", service, transport, 123, requests)
    kinds = [c[0] for c in transport.calls]
    assert kinds[0] == "send_photo" and kinds[1] == "answer_callback"
    texts = [c[1][1] for c in transport.calls if c[0] == "send_message"]
    assert len(texts) == 1
    doc = service.analyze(mint)
    for cluster in doc["clusters"]:
        for ev in cluster["evidence"]:
            assert ev["type"] in texts[0]


def test_evidence_overflow_is_truncated_with_remainder_count() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    doc = service.analyze(mint)
    assert len(format_evidence(doc, 4000)) == 1
    chunks = format_evidence(doc, 50)
    assert len(chunks) > 1
    assert "і ще" in chunks[0]
    assert "".join(chunks).count("• ") >= sum(len(c["evidence"]) for c in doc["clusters"])


def test_no_clusters_message_says_so_without_evidence_button() -> None:
    service, _ = _service(["cln1"])
    mint, _ = _mint_of("cln1")
    transport = JournalTransport()
    handle_check(f"/check {mint}", service, transport, 123, {}, render=_render())
    photos = [c for c in transport.calls if c[0] == "send_photo"]
    assert len(photos) == 1
    _, (_, _, caption), kwargs = photos[0]
    assert "не знайдено" in caption
    assert kwargs["reply_markup"] is None


def test_invalid_mint_replies_error_and_stays_alive() -> None:
    class Rejecting:
        def collect(self, mint: str):
            return Rejection(kind=RejectKind.INVALID_ADDRESS, mint=mint, detail="bad base58")

    graph, clusters = _real_services()
    service = DeliveryService(Rejecting(), graph, clusters, DeliveryCache())
    transport = JournalTransport([
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "/check !!!"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": "/check ???"}},
    ])
    requests = run_polling(transport, service, stop_after=2)
    assert requests == {}
    texts = [c[1][1] for c in transport.calls if c[0] == "send_message"]
    assert len(texts) == 2 and all("адресу" in t for t in texts)


def test_unknown_command_is_ignored_silently() -> None:
    service, _ = _service(["ins4"])
    transport = JournalTransport([
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "привіт"}},
    ])
    assert run_polling(transport, service, stop_after=1) == {}
    assert transport.calls == []


def test_stale_callback_answers_expired_rerun_check() -> None:
    service, _ = _service(["ins4"])
    transport = JournalTransport()
    handle_evidence("evidence:dead:0", "q9", service, transport, 123, {})
    kinds = [c[0] for c in transport.calls]
    assert kinds == ["answer_callback", "send_message"]
    assert "застарів" in transport.calls[1][1][1]
