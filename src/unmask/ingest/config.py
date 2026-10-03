# impl: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16
"""Завантаження й валідація config/ingest.yaml (принцип III, contracts/config-ingest.md).

Тихих умовчань немає: відсутнє чи невідоме поле та значення поза межами дають
ConfigError з назвою поля.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Некоректна конфігурація збору; повідомлення містить назву поля."""


@dataclass(frozen=True)
class RpcConfig:
    page_size: int
    request_timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    max_concurrency: int


@dataclass(frozen=True)
class IngestConfig:
    version: int
    first_buyers_n: int
    funding_depth: int
    counterparty_threshold: int
    max_signatures_per_wallet: int
    collect_spl_inbound: bool
    time_budget_seconds: float
    commitment: str
    rpc: RpcConfig


_COMMITMENTS = ("finalized", "confirmed")


def _int(data: dict, key: str, path: str, lo: int | None = None, hi: int | None = None) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: expected int, got {value!r}")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        bounds = f"{lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}"
        raise ConfigError(f"{path}: {value} is out of range {bounds}")
    return value


def _float(data: dict, key: str, path: str, *, positive: bool) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: expected number, got {value!r}")
    if (positive and value <= 0) or value < 0:
        raise ConfigError(f"{path}: {value} must be {'> 0' if positive else '>= 0'}")
    return value


def _check_keys(data: Any, expected: set[str], where: str) -> dict:
    prefix = f"{where}." if where else ""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected a mapping")
    for key in sorted(expected - data.keys()):
        raise ConfigError(f"{prefix}{key}: missing required field")
    for key in sorted(data.keys() - expected, key=str):
        raise ConfigError(f"{prefix}{key}: unknown field")
    return data


def load_config(path: Path) -> IngestConfig:
    """Прочитати YAML і повернути перевірену IngestConfig або кинути ConfigError."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc

    top = _check_keys(raw, set(IngestConfig.__dataclass_fields__), "")
    rpc = _check_keys(top["rpc"], set(RpcConfig.__dataclass_fields__), "rpc")

    version = _int(top, "version", "version", lo=1)
    first_buyers_n = _int(top, "first_buyers_n", "first_buyers_n", lo=1, hi=500)
    funding_depth = _int(top, "funding_depth", "funding_depth", lo=1, hi=3)
    counterparty_threshold = _int(top, "counterparty_threshold", "counterparty_threshold", lo=1)
    max_signatures = _int(top, "max_signatures_per_wallet", "max_signatures_per_wallet", lo=1)
    collect_spl = top["collect_spl_inbound"]
    if not isinstance(collect_spl, bool):
        raise ConfigError(f"collect_spl_inbound: expected bool, got {collect_spl!r}")
    budget = _float(top, "time_budget_seconds", "time_budget_seconds", positive=True)
    commitment = top["commitment"]
    if commitment not in _COMMITMENTS:
        raise ConfigError(f"commitment: {commitment!r} not in {list(_COMMITMENTS)}")

    return IngestConfig(
        version=version,
        first_buyers_n=first_buyers_n,
        funding_depth=funding_depth,
        counterparty_threshold=counterparty_threshold,
        max_signatures_per_wallet=max_signatures,
        collect_spl_inbound=collect_spl,
        time_budget_seconds=budget,
        commitment=commitment,
        rpc=RpcConfig(
            page_size=_int(rpc, "page_size", "rpc.page_size", lo=1, hi=1000),
            request_timeout_seconds=_float(
                rpc, "request_timeout_seconds", "rpc.request_timeout_seconds", positive=True
            ),
            max_retries=_int(rpc, "max_retries", "rpc.max_retries", lo=0),
            retry_backoff_seconds=_float(
                rpc, "retry_backoff_seconds", "rpc.retry_backoff_seconds", positive=False
            ),
            max_concurrency=_int(rpc, "max_concurrency", "rpc.max_concurrency", lo=1),
        ),
    )
