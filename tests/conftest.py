# trace: ignore-file
"""Спільні фікстури. Гард мережі (принцип II): тести не ходять у зовнішні сервіси."""

import socket

import pytest


class NetworkForbidden(Exception):
    """Спроба мережевого з'єднання з тесту."""


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    def _blocked(self, *args, **kwargs):
        raise NetworkForbidden(f"network access is forbidden in tests: connect{args}")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
