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
    assert "54/100" in caption and "high concentration" in caption
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["text"] == "evidence"
    assert _check_id(transport) == f"evidence:{request_id}"


def test_evidence_callback_sends_full_proof_text() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    transport = JournalTransport()
    requests: dict[str, str] = {}
    handle_check(f"/check {mint}", service, transport, 123, requests, render=_render())
    callback_id = _check_id(transport)
    handle_evidence(callback_id, "q1", service, transport, 123, requests, limit=4000)
    kinds = [c[0] for c in transport.calls]
    # handle_check: 4 progress msgs + send_photo = 5 calls
    # handle_evidence: answer_callback + send_message (evidence) = 2 calls
    assert kinds[0] == "send_message"  # progress 1
    assert kinds[1] == "send_message"  # progress 2
    assert kinds[2] == "send_message"  # progress 3
    assert kinds[3] == "send_message"  # progress 4
    assert kinds[4] == "send_photo"    # photo from handle_check
    assert kinds[5] == "answer_callback"  # answer_callback from handle_evidence
    assert kinds[6] == "send_message"  # evidence text from handle_evidence
    texts = [c[1][1] for c in transport.calls if c[0] == "send_message"]
    assert len(texts) == 5  # 4 progress + 1 evidence
    doc = service.analyze(mint)
    for cluster in doc["clusters"]:
        for ev in cluster["evidence"]:
            assert ev["type"] in texts[-1]  # evidence is in the last send_message


def test_evidence_overflow_is_truncated_with_remainder_count() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    doc = service.analyze(mint)
    assert len(format_evidence(doc, 4000)) == 1
    chunks = format_evidence(doc, 50)
    assert len(chunks) > 1
    assert "more" in chunks[0]
    assert "".join(chunks).count("• ") >= sum(len(c["evidence"]) for c in doc["clusters"])


def test_no_clusters_message_says_so_without_evidence_button() -> None:
    service, _ = _service(["cln1"])
    mint, _ = _mint_of("cln1")
    transport = JournalTransport()
    handle_check(f"/check {mint}", service, transport, 123, {}, render=_render())
    photos = [c for c in transport.calls if c[0] == "send_photo"]
    assert len(photos) == 1
    _, (_, _, caption), kwargs = photos[0]
    assert "no related groups" in caption
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
    requests = run_polling(transport, service, evidence_limit=4000, stop_after=2)
    assert requests == {}
    texts = [c[1][1] for c in transport.calls if c[0] == "send_message"]
    # 1 progress + 1 error per check = 2 per check, 2 checks = 4 messages
    assert len(texts) == 4
    error_texts = [t for t in texts if "Solana address" in t]
    assert len(error_texts) == 2 and all("Solana address" in t for t in error_texts)


def test_unknown_command_is_ignored_silently() -> None:
    service, _ = _service(["ins4"])
    transport = JournalTransport([
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "привіт"}},
    ])
    assert run_polling(transport, service, evidence_limit=4000, stop_after=1) == {}
    assert transport.calls == []


def test_stale_callback_answers_expired_rerun_check() -> None:
    service, _ = _service(["ins4"])
    transport = JournalTransport()
    handle_evidence("evidence:dead:0", "q9", service, transport, 123, {}, limit=4000)
    kinds = [c[0] for c in transport.calls]
    assert kinds == ["answer_callback", "send_message"]
    assert "expired" in transport.calls[1][1][1]


class FlakyTransport(JournalTransport):
    """Перший get_updates кидає мережевий виняток, далі віддає чергу."""

    def __init__(self, updates):
        super().__init__(updates)
        self.failures = 1

    def get_updates(self, offset: int, timeout: int):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("network down")
        return super().get_updates(offset, timeout)


def test_polling_survives_get_updates_failure_with_backoff() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    transport = FlakyTransport([
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": f"/check {mint}"}},
    ])
    sleeps = []
    requests = run_polling(transport, service, evidence_limit=4000, stop_after=1,
                           render=_render(), sleep=sleeps.append)
    assert sleeps == [1.0]
    assert len(requests) == 1
    assert any(c[0] == "send_photo" for c in transport.calls)


def test_evidence_limit_comes_from_config_not_code_default() -> None:
    service, _ = _service(["ins4"])
    mint, _ = _mint_of("ins4")
    doc = service.analyze(mint)
    from unmask.delivery.bot import format_evidence
    assert format_evidence(doc, 4000) == format_evidence(doc, 4000)
    assert format_evidence(doc, 50) != format_evidence(doc, 4000)
    transport = JournalTransport()
    requests: dict[str, str] = {}
    handle_check(f"/check {mint}", service, transport, 123, requests, render=_render())
    callback_id = _check_id(transport)
    handle_evidence(callback_id, "q1", service, transport, 123, requests, limit=50)
    texts = [c[1][1] for c in transport.calls if c[0] == "send_message"]
    assert len(texts) > 1 and "more" in texts[0]


def test_insufficient_data_caption_names_band_and_never_clean() -> None:
    service, _ = _service(["cln2"])
    mint, _ = _mint_of("cln2")
    doc = service.analyze(mint)
    assert doc["band"] == "insufficient_data"
    transport = JournalTransport()
    handle_check(f"/check {mint}", service, transport, 123, {}, render=_render())
    caption = next(c[1][2] for c in transport.calls if c[0] == "send_photo")
    assert "insufficient data" in caption
    assert "clean" not in caption


def test_long_source_lists_show_remainder_count() -> None:
    from unmask.delivery.bot import format_evidence
    doc = {"clusters": [{"supply_share": 0.5, "confidence": 0.6,
                         "evidence": [{"type": "shared_funder",
                                       "source": [f"A{i}" + "1" * 31 for i in range(7)],
                                       "window": {"basis": "block_time", "start": 1, "end": 2}}]}]}
    (text,) = format_evidence(doc, 4000)
    assert "+3 more" in text


def test_request_registry_is_bounded() -> None:
    from unmask.delivery import bot as bot_module

    class Canned:
        def analyze(self, mint: str, progress=None):
            if progress:
                progress("🔄 Collecting data...")
                progress("🔄 Building funding graph...")
                progress("🔄 Finding clusters...")
                progress("🔄 Building report...")
            return {"mint": mint, "analyzed_at": 1, "wallets_analyzed": 1, "clusters": [],
                    "risk_score": 0, "band": "insufficient_data", "band_reasons": ["empty_input"],
                    "provenance": {}, "error": None}

    updates = [{"update_id": i, "message": {"message_id": i, "chat": {"id": 7},
                                            "text": f"/check MINT{i:04d}"}} for i in range(1010)]
    transport = JournalTransport(updates)
    requests = run_polling(transport, Canned(), evidence_limit=4000, stop_after=1010)
    assert len(requests) == bot_module._MAX_REQUESTS == 1000
