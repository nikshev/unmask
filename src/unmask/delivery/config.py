# impl: FR-004-11
"""Завантаження й валідація `config/delivery.yaml` (принцип III; specs/004-api-bot-delivery/data-model.md).

Лише показові й операційні сталі — жодна не змінює кластери, впевненість, `risk_score`
чи смугу (FR-004-11). Тихих умовчань немає: відсутнє чи невідоме поле та значення поза
межами дають `ConfigError` з назвою поля. Журнал не читається (перевірка — тест).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from unmask.hubs.config import ConfigError, content_digest

__all__ = ["DeliveryConfig", "load_delivery_config"]

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class DeliveryConfig:
    """Показові й операційні сталі доставки."""

    version: int
    http_port: int
    png_width: int
    png_height: int
    cluster_palette: tuple[str, ...]
    address_prefix_len: int
    evidence_preview_limit: int
    digest: str  # sha256 канонічного вмісту файла

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ConfigError("version: must be an int >= 1")
        for name, lo, hi in (("http_port", 1, 65535), ("png_width", 1, None),
                             ("png_height", 1, None), ("address_prefix_len", 1, None),
                             ("evidence_preview_limit", 1, None)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name}: expected int")
            if value < lo or (hi is not None and value > hi):
                bound = f"{lo}..{hi}" if hi is not None else f">= {lo}"
                raise ConfigError(f"{name}: must satisfy {bound}")
        if not isinstance(self.cluster_palette, tuple) or len(self.cluster_palette) == 0:
            raise ConfigError("cluster_palette: must be a non-empty tuple")
        for i, color in enumerate(self.cluster_palette):
            if not isinstance(color, str) or _COLOR_RE.match(color) is None:
                raise ConfigError(f"cluster_palette[{i}]: expected '#rrggbb'")
        if not isinstance(self.digest, str) or not self.digest:
            raise ConfigError("digest: must be a non-empty string")


def _check_keys(data: Any, expected: tuple[str, ...], where: str) -> dict:
    prefix = f"{where}." if where else ""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected a mapping")
    for key in expected:
        if key not in data:
            raise ConfigError(f"{prefix}{key}: missing required field")
    for key in data:
        if not (isinstance(key, str) and key in expected):
            label = key if isinstance(key, str) else f"<{type(key).__name__} key>"
            raise ConfigError(f"{prefix}{label}: unknown field")
    return data


def load_delivery_config(path: Path) -> DeliveryConfig:
    """Прочитати `config/delivery.yaml`; некоректне → `ConfigError`."""
    path = Path(path)
    try:
        digest = content_digest(path)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"{path}: invalid YAML ({type(exc).__name__})") from None
    data = _check_keys(raw, ("version", "http_port", "png_width", "png_height",
                             "cluster_palette", "address_prefix_len",
                             "evidence_preview_limit"), "")
    palette = data["cluster_palette"]
    if not isinstance(palette, list):
        raise ConfigError("cluster_palette: expected a list")
    return DeliveryConfig(
        version=data["version"],
        http_port=data["http_port"],
        png_width=data["png_width"],
        png_height=data["png_height"],
        cluster_palette=tuple(palette),
        address_prefix_len=data["address_prefix_len"],
        evidence_preview_limit=data["evidence_preview_limit"],
        digest=digest,
    )
