# verifies: FR-008-13
"""README не називає RPC-вендорів за іменем (FR-008-13).

Назви змінних середовища і провайдерів живуть у config/rpc_pool.yaml; README
описує їх нейтрально («archival provider», «free-tier keys»).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

VENDOR_RE = re.compile(r"helius", re.IGNORECASE)


def test_readme_names_no_rpc_vendor() -> None:
    assert README.is_file(), "README.md відсутній"
    text = README.read_text(encoding="utf-8")
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if VENDOR_RE.search(line)
    ]
    assert not hits, "README називає RPC-вендора за іменем:\n" + "\n".join(hits)
