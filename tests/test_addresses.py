# verifies: FR-001-11
"""Валідність адреси і тип ключа без мережі."""

import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from unmask.ingest.addresses import address_type, is_valid_address
from unmask.ingest.model import AddressType

SYSTEM_PROGRAM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
BASIC_EXPECTED = Path(__file__).parent / "fixtures" / "scenarios" / "basic" / "expected.json"

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    """Base58 довільної довжини (solders кодує лише 32 байти)."""
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _basic_buyers() -> list[dict]:
    return json.loads(BASIC_EXPECTED.read_text(encoding="utf-8"))["buyers"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param(SYSTEM_PROGRAM[:-1] + "0", id="char-0"),
        pytest.param(SYSTEM_PROGRAM[:-1] + "O", id="char-O"),
        pytest.param(SYSTEM_PROGRAM[:-1] + "I", id="char-I"),
        pytest.param(SYSTEM_PROGRAM[:-1] + "l", id="char-l"),
        pytest.param(_b58(bytes([7]) * 31), id="31-bytes"),
        pytest.param(_b58(bytes([7]) * 33), id="33-bytes"),
    ],
)
def test_invalid_strings_rejected(value):
    assert is_valid_address(value) is False


def test_non_string_rejected():
    assert is_valid_address(None) is False  # type: ignore[arg-type]


def test_system_program_and_fixture_wallets_are_on_curve():
    # Системна програма й гаманці-покупці фікстури `basic`, які фікстура сама позначає wallet.
    wallets = [b["wallet"] for b in _basic_buyers() if b["address_type"] == "wallet"]
    assert wallets, "фікстура basic має містити on-curve покупців"
    for addr in [SYSTEM_PROGRAM, *wallets]:
        assert is_valid_address(addr) is True
        assert address_type(addr) is AddressType.WALLET


def test_fixture_off_curve_owner_is_off_curve():
    off = [b["wallet"] for b in _basic_buyers() if b["address_type"] == "off_curve"]
    assert off, "фікстура basic має містити off-curve власника (P5)"
    for addr in off:
        assert address_type(addr) is AddressType.OFF_CURVE


def test_find_program_address_result_is_off_curve():
    program = Pubkey.from_string(SYSTEM_PROGRAM)
    pda, _bump = Pubkey.find_program_address([b"unmask", b"seed-1"], program)
    addr = str(pda)
    assert is_valid_address(addr) is True
    assert address_type(addr) is AddressType.OFF_CURVE
