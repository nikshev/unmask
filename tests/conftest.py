# trace: ignore-file
"""Спільні фікстури. Гард мережі (принцип II): тести не ходять у зовнішні сервіси."""

import json
import socket
from pathlib import Path

import pytest


class NetworkForbidden(Exception):
    """Спроба мережевого з'єднання з тесту."""


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    def _blocked(self, *args, **kwargs):
        raise NetworkForbidden(f"network access is forbidden in tests: connect{args}")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


GRAPH_FIXTURES = Path(__file__).parent / "fixtures" / "graph"


def load_ingest_fixture(name: str):
    """`tests/fixtures/graph/<name>/ingest.json` -> `IngestResult` (T-026, фіча 002).

    Єдиний шлях, яким фікстури 002 стають типами 001: `ingest.serialize.from_dict`. Відсутня
    фікстура — `FileNotFoundError`; фікстура, що не є результатом збору (Rejection), — `TypeError`.
    Імпорт тут відкладений: `conftest` не має залежати від коду, поки тести його не просять.
    """
    from unmask.ingest.model import IngestResult
    from unmask.ingest.serialize import from_dict

    path = GRAPH_FIXTURES / name / "ingest.json"
    result = from_dict(json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(result, IngestResult):
        raise TypeError(f"fixture {name!r} is {type(result).__name__}, not IngestResult")
    return result
