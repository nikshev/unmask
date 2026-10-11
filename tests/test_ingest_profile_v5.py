# verifies: FR-001-14, FR-007-14, FR-008-13
"""Робочий профіль сервісу — відкалібрований (ingest.yaml v5, принцип III).

Мережі немає: лише читання файлів репозиторію. Профіль 30 покупців / глибина 2 / 30 підписів / без SPL —
той, на якому відкалібровано hubs v3 / clusters v2 і отримано результати когортного експерименту. Повний
профіль (300/2/300/SPL) на ключах free-tier не завершується навіть за 1800 с. Тест фіксує:
- значення поставленого YAML (зміна будь-якого — червоний тест і вимога нового запису в журналі);
- запис `## 5` у config/CHANGELOG.md з обґрунтуванням і вірним sha256;
- README називає цей профіль, а числа в ньому збігаються з YAML (читаємо YAML, не вшиваємо);
- воркер експерименту дає той самий профіль, що поставлений YAML.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import yaml

from unmask.ingest.config import load_config

ROOT = Path(__file__).resolve().parents[1]
INGEST = ROOT / "config" / "ingest.yaml"
CHANGELOG = ROOT / "config" / "CHANGELOG.md"
README = ROOT / "README.md"
WORKER = ROOT / "scripts" / "screen_worker.py"

PROFILE = {
    "first_buyers_n": 30,
    "funding_depth": 2,
    "max_signatures_per_wallet": 30,
    "collect_spl_inbound": False,
    "time_budget_seconds": 1800,
}


def _ingest_section(text: str) -> str:
    """Текст розділу `# config/ingest.yaml` журналу — останнього в файлі."""
    m = re.search(r"^# config/ingest\.yaml[ \t]*$", text, re.MULTILINE)
    assert m, "у журналі немає розділу `# config/ingest.yaml`"
    return text[m.end():]


def _digest(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_shipped_ingest_is_the_calibrated_profile() -> None:
    cfg = load_config(INGEST)
    assert cfg.version == 5
    for key, expected in PROFILE.items():
        actual = getattr(cfg, key)
        assert actual == expected and type(actual) is type(expected), f"{key}: {actual!r} != {expected!r}"


def test_changelog_entry_5_explains_profile_and_carries_digest() -> None:
    section = _ingest_section(CHANGELOG.read_text(encoding="utf-8"))
    headers = re.findall(r"^## (\d+) — (\d{4}-\d{2}-\d{2})$", section, re.MULTILINE)
    assert [int(v) for v, _ in headers][-1] == 5, "останній запис розділу ingest має бути ## 5"
    entry = section[section.index("## 5 "):]
    for needle in ("first_buyers_n", "300", "30", "max_signatures_per_wallet", "collect_spl_inbound",
                   "846", "insufficient_data", "hubs v3", "clusters v2", "PRD"):
        assert needle in entry, f"запис ## 5 не згадує {needle!r}"
    last = [line for line in entry.splitlines() if line.strip()][-1]
    assert last == f"sha256: {_digest(INGEST)}"


def _readme_profile_paragraph() -> str:
    text = README.read_text(encoding="utf-8")
    m = re.search(r"^## Deploy[ \t]*$", text, re.MULTILINE)
    assert m, "у README немає розділу `## Deploy`"
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    deploy = rest[: nxt.start()] if nxt else rest
    p = re.search(r"The shipped ingest profile \(v(\d+)\) is the calibrated one:.*?(?:\n\n|\Z)", deploy, re.DOTALL)
    assert p, "README Deploy має містити абзац `The shipped ingest profile (vN) is the calibrated one: …`"
    return " ".join(p.group(0).split())


def test_readme_names_the_profile_and_numbers_match_yaml() -> None:
    data = yaml.safe_load(INGEST.read_text(encoding="utf-8"))
    paragraph = _readme_profile_paragraph()
    assert f"(v{data['version']})" in paragraph
    assert f"first {data['first_buyers_n']} buyers" in paragraph
    assert f"funding depth {data['funding_depth']}" in paragraph
    assert f"{data['max_signatures_per_wallet']} signatures per wallet" in paragraph
    assert ("no SPL inbound" in paragraph) == (data["collect_spl_inbound"] is False)
    assert f"in {data['time_budget_seconds']} s" in paragraph
    for needle in ("hubs v3 / clusters v2", "calibrated", "paid RPC throughput", "846 histories missing", "PRD"):
        assert needle in paragraph, needle


def test_screen_worker_profile_equals_shipped_yaml() -> None:
    """Перекриття в `screen_worker.py` (30/30/без SPL) надлишкові: воркер дає той самий профіль, що YAML."""
    import dataclasses
    import sys

    spec = importlib.util.spec_from_file_location("screen_worker_v5_profile", WORKER)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    shipped = load_config(INGEST)
    seen = {}

    class Stop(Exception):
        pass

    def fake_build(path, cfg, env, **kw):
        seen["cfg"] = cfg
        raise Stop

    worker.build_rpc_source = fake_build
    argv = sys.argv
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        mints = Path(tmp) / "m.jsonl"
        mints.write_text('{"mint": "M"}\n', encoding="utf-8")
        sys.argv = ["screen_worker.py", str(mints), str(Path(tmp) / "o.csv"), "0"]
        try:
            worker.main()
        except Stop:
            pass
        finally:
            sys.argv = argv
    assert dataclasses.asdict(seen["cfg"]) == dataclasses.asdict(shipped)
