# verifies: FR-001-15
"""Гард мережі (принцип II): жоден тест не може відкрити сокет."""

import socket

import pytest


def test_socket_connect_is_forbidden():
    with pytest.raises(Exception) as excinfo:
        socket.create_connection(("127.0.0.1", 9))
    assert type(excinfo.value).__name__ == "NetworkForbidden"


def test_unmask_package_importable():
    import unmask
    import unmask.ingest
    import unmask.ingest.rpc

    assert unmask.ingest.rpc is not None
