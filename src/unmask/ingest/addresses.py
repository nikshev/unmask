# impl: FR-001-11
"""Валідність адреси Solana і тип ключа. Без мережі."""

from solders.pubkey import Pubkey

from unmask.ingest.model import AddressType


def _parse(address: object) -> Pubkey | None:
    if not isinstance(address, str) or not address:
        return None
    try:
        return Pubkey.from_string(address)  # base58, рівно 32 байти
    except ValueError:
        return None


def is_valid_address(address: str) -> bool:
    """Чи є рядок base58-адресою рівно з 32 байтів."""
    return _parse(address) is not None


def address_type(address: str) -> AddressType:
    """`off_curve`, якщо ключ не лежить на кривій ed25519 (так влаштовані PDA), інакше `wallet`.

    Некоректна адреса — `ValueError`: тип визначається лише для валідної адреси.
    """
    key = _parse(address)
    if key is None:
        raise ValueError(f"некоректна адреса: {address!r}")
    return AddressType.WALLET if key.is_on_curve() else AddressType.OFF_CURVE
