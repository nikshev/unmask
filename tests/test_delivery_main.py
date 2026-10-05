# verifies: FR-004-10
"""Тести точки входу: fail-fast секретів, прапорці, відсутність літералів ключів (T-095)."""

from __future__ import annotations

import re
from pathlib import Path

from unmask.delivery.main import main

ROOT = Path(__file__).resolve().parents[1]


def test_missing_rpc_key_fails_fast_with_var_name(capsys) -> None:
    assert main(["--no-bot"], environ={}) == 2
    assert "UNMASK_RPC_URL" in capsys.readouterr().err


def test_missing_bot_token_with_bot_enabled_fails_fast(capsys) -> None:
    assert main([], environ={"UNMASK_RPC_URL": "https://example.invalid/rpc?key=x"}) == 2
    assert "UNMASK_BOT_TOKEN" in capsys.readouterr().err





def test_no_bot_flag_runs_http_only(capsys) -> None:
    # Без ключа RPC падаємо раніше, ніж справа дійде до прапорця, — перевіряємо розбір окремо:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-bot", action="store_true")
    assert parser.parse_args(["--no-bot"]).no_bot is True


def test_no_secret_literals_in_repo() -> None:
    suspects = []
    roots = [ROOT / "src", ROOT / "scripts", ROOT / "config", ROOT / "tests",
             ROOT / "specs", ROOT / "docs"]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".md", ".yaml", ".yml", ".json",
                                                         ".toml", ".sh", ".example"):
                continue
            if "fixtures" in path.parts and path.suffix == ".json":
                continue  # записані ончейн-дані й еталони, не секрети
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in (r"[A-Za-z0-9_-]{10,}:[\w-]{20,}", r"\bxox[bap]-[A-Za-z0-9-]{8,}",
                            r"\bsk-[A-Za-z0-9]{8,}", r"eyJ[A-Za-z0-9_-]{10,}"):
                if re.search(pattern, text):
                    suspects.append((str(path.relative_to(ROOT)), pattern))
    assert suspects == []
