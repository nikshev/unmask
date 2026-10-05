# impl: FR-003-04, FR-003-10, FR-003-13, FR-003-18
"""Завантаження й валідація `config/clusters.yaml` (принцип III; specs/003-wallet-clusters-risk/contracts/config-clusters.md).

Тихих умовчань немає: відсутнє чи невідоме поле та значення поза межами дають `ConfigError` з назвою поля.

Правило порогу — як у всьому проєкті (002 R-9): нерівність строга, рівно поріг не змінює рішення в бік
«спрацювало». Напрямок — властивість поля: *_min_* — зв'язок при «>=» (рівно поріг — зв'язок; строго менше — ні);
*_natural_max — доказ при «>» (рівно поріг — природний збіг, не доказ); artifact_buyer_share — «>».

`load_cluster_config` спочатку викликає `hubs.config.content_digest(path)` — обмежений розбір (без анкерів/аліасів/тегів,
ліміти розміру й глибини) відкидає будь-яку ваду файла `ConfigError`, — і лише потім читає значення (research R-11).
З `unmask.hubs.config` імпортуються лише чотири публічні імена (T-083): ConfigError, content_digest,
changelog_entries, check_changelog.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from unmask.hubs.config import ConfigError, content_digest

__all__ = [
    "EvidenceWeights",
    "ClusterConfig",
    "load_cluster_config",
]

_EVIDENCE_TYPES = (
    "shared_funder",
    "direct_transfer",
    "delegated_buy",
    "recovered_edge",
    "indirect_link",
    "same_amounts",
    "same_slot",
)


@dataclass(frozen=True)
class EvidenceWeights:
    """Вага кожного типу доказу для noisy-OR (FR-003-11). Усі значення в (0, 1)."""

    shared_funder: float
    direct_transfer: float
    delegated_buy: float
    recovered_edge: float
    indirect_link: float
    same_amounts: float
    same_slot: float


@dataclass(frozen=True)
class ClusterConfig:
    """Версіонована конфігурація кластеризації й оцінки ризику (контракт config-clusters.md)."""

    version: int
    link_min_amount_lamports: int
    funding_window_seconds: int
    seconds_per_slot: float
    link_through_flagged_buyers: bool
    link_assets: tuple[str, ...]
    indirect_enabled: bool
    same_amount_natural_max: int
    same_slot_natural_max: int
    same_slot_window_slots: int
    evidence_weights: EvidenceWeights
    slot_fallback_multiplier: float
    artifact_buyer_share: float
    artifact_confidence_multiplier: float
    band_clean_max: int
    band_suspicious_max: int
    digest: str  # sha256 канонічного вмісту файла

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ConfigError("version: must be an int >= 1")

        if isinstance(self.link_min_amount_lamports, bool) or not isinstance(self.link_min_amount_lamports, int):
            raise ConfigError("link_min_amount_lamports: expected int")
        if self.link_min_amount_lamports < 1:
            raise ConfigError("link_min_amount_lamports: must be >= 1")

        if isinstance(self.funding_window_seconds, bool) or not isinstance(self.funding_window_seconds, int):
            raise ConfigError("funding_window_seconds: expected int")
        if self.funding_window_seconds < 1:
            raise ConfigError("funding_window_seconds: must be >= 1")

        if isinstance(self.seconds_per_slot, bool) or not isinstance(self.seconds_per_slot, (int, float)):
            raise ConfigError("seconds_per_slot: expected number")
        try:
            val = float(self.seconds_per_slot)
        except OverflowError:
            raise ConfigError("seconds_per_slot: integer out of range")
        if math.isnan(val) or val <= 0:
            raise ConfigError("seconds_per_slot: must be > 0 and not NaN")

        if not isinstance(self.link_through_flagged_buyers, bool):
            raise ConfigError("link_through_flagged_buyers: expected bool")

        if not isinstance(self.link_assets, tuple) or len(self.link_assets) == 0:
            raise ConfigError("link_assets: must be a non-empty tuple")
        seen = set()
        for i, asset in enumerate(self.link_assets):
            if not isinstance(asset, str):
                raise ConfigError(f"link_assets[{i}]: expected str")
            if asset == "sol":
                pass
            elif asset.startswith("spl:"):
                mint = asset[4:]
                if len(mint) < 32 or len(mint) > 44:
                    raise ConfigError(f"link_assets[{i}]: SPL mint must be 32..44 base58 chars")
            else:
                raise ConfigError(f"link_assets[{i}]: must be 'sol' or 'spl:<mint>'")
            if asset in seen:
                raise ConfigError(f"link_assets[{i}]: duplicate asset")
            seen.add(asset)

        if not isinstance(self.indirect_enabled, bool):
            raise ConfigError("indirect_enabled: expected bool")

        if isinstance(self.same_amount_natural_max, bool) or not isinstance(self.same_amount_natural_max, int):
            raise ConfigError("same_amount_natural_max: expected int")
        if self.same_amount_natural_max < 1:
            raise ConfigError("same_amount_natural_max: must be >= 1")

        if isinstance(self.same_slot_natural_max, bool) or not isinstance(self.same_slot_natural_max, int):
            raise ConfigError("same_slot_natural_max: expected int")
        if self.same_slot_natural_max < 1:
            raise ConfigError("same_slot_natural_max: must be >= 1")

        if isinstance(self.same_slot_window_slots, bool) or not isinstance(self.same_slot_window_slots, int):
            raise ConfigError("same_slot_window_slots: expected int")
        if self.same_slot_window_slots < 0:
            raise ConfigError("same_slot_window_slots: must be >= 0")

        if not isinstance(self.evidence_weights, EvidenceWeights):
            raise ConfigError("evidence_weights: expected EvidenceWeights")
        for field_name in _EVIDENCE_TYPES:
            value = getattr(self.evidence_weights, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"evidence_weights.{field_name}: expected number")
            try:
                w = float(value)
            except OverflowError:
                raise ConfigError(f"evidence_weights.{field_name}: integer out of range")
            if math.isnan(w) or w <= 0 or w >= 1:
                raise ConfigError(f"evidence_weights.{field_name}: must satisfy 0 < w < 1")
        if not (self.evidence_weights.indirect_link < self.evidence_weights.shared_funder):
            raise ConfigError("evidence_weights.indirect_link: must be < evidence_weights.shared_funder")

        if isinstance(self.slot_fallback_multiplier, bool) or not isinstance(self.slot_fallback_multiplier, (int, float)):
            raise ConfigError("slot_fallback_multiplier: expected number")
        try:
            m = float(self.slot_fallback_multiplier)
        except OverflowError:
            raise ConfigError("slot_fallback_multiplier: integer out of range")
        if math.isnan(m) or m <= 0 or m > 1:
            raise ConfigError("slot_fallback_multiplier: must satisfy 0 < m <= 1")

        if isinstance(self.artifact_buyer_share, bool) or not isinstance(self.artifact_buyer_share, (int, float)):
            raise ConfigError("artifact_buyer_share: expected number")
        try:
            x = float(self.artifact_buyer_share)
        except OverflowError:
            raise ConfigError("artifact_buyer_share: integer out of range")
        if math.isnan(x) or x <= 0 or x > 1:
            raise ConfigError("artifact_buyer_share: must satisfy 0 < x <= 1")

        if isinstance(self.artifact_confidence_multiplier, bool) or not isinstance(self.artifact_confidence_multiplier, (int, float)):
            raise ConfigError("artifact_confidence_multiplier: expected number")
        try:
            m = float(self.artifact_confidence_multiplier)
        except OverflowError:
            raise ConfigError("artifact_confidence_multiplier: integer out of range")
        if math.isnan(m) or m <= 0 or m > 1:
            raise ConfigError("artifact_confidence_multiplier: must satisfy 0 < m <= 1")

        if isinstance(self.band_clean_max, bool) or not isinstance(self.band_clean_max, int):
            raise ConfigError("band_clean_max: expected int")
        if self.band_clean_max < 0:
            raise ConfigError("band_clean_max: must be >= 0")

        if isinstance(self.band_suspicious_max, bool) or not isinstance(self.band_suspicious_max, int):
            raise ConfigError("band_suspicious_max: expected int")
        if self.band_suspicious_max > 100:
            raise ConfigError("band_suspicious_max: must be <= 100")

        if not (self.band_clean_max < self.band_suspicious_max):
            raise ConfigError("band_clean_max: must be < band_suspicious_max")

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


def _parse_evidence_weights(data: dict[str, Any]) -> EvidenceWeights:
    weights_data = _check_keys(data, _EVIDENCE_TYPES, "evidence_weights")
    return EvidenceWeights(**{k: float(weights_data[k]) for k in _EVIDENCE_TYPES})


def _parse_link_assets(data: list[Any]) -> tuple[str, ...]:
    if not isinstance(data, list) or len(data) == 0:
        raise ConfigError("link_assets: must be a non-empty list")
    seen = set()
    result = []
    for i, asset in enumerate(data):
        if not isinstance(asset, str):
            raise ConfigError(f"link_assets[{i}]: expected str")
        if asset == "sol":
            pass
        elif asset.startswith("spl:"):
            mint = asset[4:]
            if len(mint) < 32 or len(mint) > 44:
                raise ConfigError(f"link_assets[{i}]: SPL mint must be 32..44 base58 chars")
        else:
            raise ConfigError(f"link_assets[{i}]: must be 'sol' or 'spl:<mint>'")
        if asset in seen:
            raise ConfigError(f"link_assets[{i}]: duplicate asset")
        seen.add(asset)
        result.append(asset)
    return tuple(result)


def load_cluster_config(path: Path) -> ClusterConfig:
    """Прочитати `config/clusters.yaml`; некоректне → `ConfigError`.

    Спершу `hubs.config.content_digest(path)` — обмежений розбір відкидає будь-яку ваду файла;
    лише потім читаються значення. Журнал змін не читається; дайджест — з того самого вмісту.
    """
    path = Path(path)
    try:
        digest = content_digest(path)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except Exception as exc:
        raise ConfigError(f"{path}: invalid YAML ({type(exc).__name__})") from None

    expected_fields = (
        "version",
        "link_min_amount_lamports",
        "funding_window_seconds",
        "seconds_per_slot",
        "link_through_flagged_buyers",
        "link_assets",
        "indirect_enabled",
        "same_amount_natural_max",
        "same_slot_natural_max",
        "same_slot_window_slots",
        "evidence_weights",
        "slot_fallback_multiplier",
        "artifact_buyer_share",
        "artifact_confidence_multiplier",
        "band_clean_max",
        "band_suspicious_max",
    )
    data = _check_keys(raw, expected_fields, "")

    return ClusterConfig(
        version=data["version"],
        link_min_amount_lamports=data["link_min_amount_lamports"],
        funding_window_seconds=data["funding_window_seconds"],
        seconds_per_slot=float(data["seconds_per_slot"]),
        link_through_flagged_buyers=data["link_through_flagged_buyers"],
        link_assets=_parse_link_assets(data["link_assets"]),
        indirect_enabled=data["indirect_enabled"],
        same_amount_natural_max=data["same_amount_natural_max"],
        same_slot_natural_max=data["same_slot_natural_max"],
        same_slot_window_slots=data["same_slot_window_slots"],
        evidence_weights=_parse_evidence_weights(data["evidence_weights"]),
        slot_fallback_multiplier=float(data["slot_fallback_multiplier"]),
        artifact_buyer_share=float(data["artifact_buyer_share"]),
        artifact_confidence_multiplier=float(data["artifact_confidence_multiplier"]),
        band_clean_max=data["band_clean_max"],
        band_suspicious_max=data["band_suspicious_max"],
        digest=digest,
    )
