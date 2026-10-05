# impl: FR-003-07, FR-003-09, FR-003-11, FR-003-12, FR-003-14, FR-003-15
"""Модель даних кластерів: перелічення, вікно, доказ, кластер, діагностика, повнота, метадані, результат.

Усі сутності — незмінні дата-класи (`frozen=True`), що перевіряють себе при побудові
(некоректний стан — `TypeError`/`ValueError` гучно, стиль `graph/model.py` 002).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from unmask.graph.model import (
    EdgeRef,
    GraphCompletenessStatus,
    GraphWarning,
    ref_sort_key,
)
from unmask.ingest.model import CompletenessStatus

__all__ = [
    "EvidenceType",
    "TimeBasis",
    "RiskBand",
    "ClusterWarning",
    "DiagnosticKind",
    "LINK_TYPES",
    "BEHAVIORAL_TYPES",
    "CLUSTER_SCHEMA_VERSION",
    "SHARE_DENOMINATOR_KIND",
    "ClusterInputError",
    "Window",
    "Evidence",
    "ClusterMember",
    "Cluster",
    "DiagnosticSignal",
    "ClusterCompleteness",
    "ThresholdsSnapshot",
    "ClusterMetadata",
    "ClusterResult",
    "cluster_id",
    "cluster_sort_key",
    "evidence_sort_key",
    "diagnostic_sort_key",
]


class EvidenceType(StrEnum):
    SHARED_FUNDER = "shared_funder"
    DIRECT_TRANSFER = "direct_transfer"
    DELEGATED_BUY = "delegated_buy"
    RECOVERED_EDGE = "recovered_edge"
    INDIRECT_LINK = "indirect_link"
    SAME_AMOUNTS = "same_amounts"
    SAME_SLOT = "same_slot"


class TimeBasis(StrEnum):
    BLOCK_TIME = "block_time"
    SLOT = "slot"


class RiskBand(StrEnum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    HIGH_CONCENTRATION = "high_concentration"
    INSUFFICIENT_DATA = "insufficient_data"


class ClusterWarning(StrEnum):
    POSSIBLE_PRUNING_ARTIFACT = "possible_pruning_artifact"
    SLOT_TIME_FALLBACK = "slot_time_fallback"


class DiagnosticKind(StrEnum):
    SAME_AMOUNTS_UNLINKED = "same_amounts_unlinked"
    SAME_SLOT_UNLINKED = "same_slot_unlinked"
    FLAGGED_BUYERS_EXCLUDED = "flagged_buyers_excluded"


LINK_TYPES = frozenset({
    EvidenceType.SHARED_FUNDER,
    EvidenceType.DIRECT_TRANSFER,
    EvidenceType.DELEGATED_BUY,
    EvidenceType.RECOVERED_EDGE,
    EvidenceType.INDIRECT_LINK,
})

BEHAVIORAL_TYPES = frozenset({
    EvidenceType.SAME_AMOUNTS,
    EvidenceType.SAME_SLOT,
})

CLUSTER_SCHEMA_VERSION = "003.1"
SHARE_DENOMINATOR_KIND = "analyzed_buyers_received_amount"


class ClusterInputError(Exception):
    """Неузгоджені входи `ClusterService.analyze` — дефект викликача, не дані."""


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class Window:
    basis: TimeBasis
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.basis, TimeBasis):
            if not isinstance(self.basis, str):
                raise TypeError(f"basis: expected TimeBasis, got {type(self.basis).__name__}")
            _set(self, "basis", TimeBasis(self.basis))
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TypeError(f"start: expected int, got {type(self.start).__name__}")
        if isinstance(self.end, bool) or not isinstance(self.end, int):
            raise TypeError(f"end: expected int, got {type(self.end).__name__}")
        if self.start < 0:
            raise ValueError("start: must be >= 0")
        if self.end < self.start:
            raise ValueError("end: must be >= start")


def evidence_sort_key(ev: Evidence) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[int, str, tuple[int, ...]]]:
    """Ключ порядку доказів — `(type, via, wallets, refs[0])`, лише з даних."""
    return (ev.type.value, ev.via, ev.wallets, ref_sort_key(ev.refs[0]))


@dataclass(frozen=True)
class Evidence:
    type: EvidenceType
    wallets: tuple[str, ...]
    via: tuple[str, ...]
    refs: tuple[EdgeRef, ...]
    window: Window
    basis: TimeBasis
    value: int | None
    detail: str | None
    weight: float

    def __post_init__(self) -> None:
        if not isinstance(self.type, EvidenceType):
            if not isinstance(self.type, str):
                raise TypeError(f"type: expected EvidenceType, got {type(self.type).__name__}")
            _set(self, "type", EvidenceType(self.type))

        if not isinstance(self.wallets, tuple):
            raise TypeError(f"wallets: expected tuple, got {type(self.wallets).__name__}")
        if len(self.wallets) < 2:
            raise ValueError("wallets: must have at least 2 elements")
        if len(set(self.wallets)) != len(self.wallets):
            raise ValueError("wallets: must be unique")
        if tuple(sorted(self.wallets)) != self.wallets:
            raise ValueError("wallets: must be sorted")
        for i, w in enumerate(self.wallets):
            if not isinstance(w, str) or not w:
                raise TypeError(f"wallets[{i}]: expected non-empty str")

        if not isinstance(self.via, tuple):
            raise TypeError(f"via: expected tuple, got {type(self.via).__name__}")
        if len(set(self.via)) != len(self.via):
            raise ValueError("via: must be unique")
        if tuple(sorted(self.via)) != self.via:
            raise ValueError("via: must be sorted")
        for i, v in enumerate(self.via):
            if not isinstance(v, str) or not v:
                raise TypeError(f"via[{i}]: expected non-empty str")
        if self.type in (EvidenceType.INDIRECT_LINK, EvidenceType.SHARED_FUNDER, EvidenceType.RECOVERED_EDGE):
            if len(self.via) == 0:
                raise ValueError(f"via: must be non-empty for {self.type.value}")
        elif self.type in (EvidenceType.DIRECT_TRANSFER, EvidenceType.DELEGATED_BUY,
                           EvidenceType.SAME_AMOUNTS, EvidenceType.SAME_SLOT):
            if len(self.via) != 0:
                raise ValueError(f"via: must be empty for {self.type.value}")
        if set(self.wallets) & set(self.via):
            raise ValueError("wallets and via must be disjoint")

        if not isinstance(self.refs, tuple):
            raise TypeError(f"refs: expected tuple, got {type(self.refs).__name__}")
        if len(self.refs) == 0:
            raise ValueError("refs: must be non-empty")
        for i, ref in enumerate(self.refs):
            if not isinstance(ref, EdgeRef):
                raise TypeError(f"refs[{i}]: expected EdgeRef, got {type(ref).__name__}")
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("refs: must be unique")
        if tuple(sorted(self.refs, key=ref_sort_key)) != self.refs:
            raise ValueError("refs: must be sorted by ref_sort_key")

        if not isinstance(self.window, Window):
            raise TypeError(f"window: expected Window, got {type(self.window).__name__}")
        if not isinstance(self.basis, TimeBasis):
            if not isinstance(self.basis, str):
                raise TypeError(f"basis: expected TimeBasis, got {type(self.basis).__name__}")
            _set(self, "basis", TimeBasis(self.basis))
        if self.basis != self.window.basis:
            raise ValueError("basis must equal window.basis")

        if self.type in BEHAVIORAL_TYPES:
            if self.basis != TimeBasis.SLOT:
                raise ValueError(f"basis: must be slot for behavioral {self.type.value}")
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise TypeError(f"value: expected int for {self.type.value}")
            if self.value < 0:
                raise ValueError("value: must be >= 0")
            if self.type == EvidenceType.SAME_AMOUNTS:
                if self.detail not in ("funding", "first_buy_spent"):
                    raise ValueError("detail: must be 'funding' or 'first_buy_spent' for same_amounts")
            elif self.detail is not None:
                raise ValueError(f"detail: must be None for {self.type.value}")
        else:
            if self.value is not None:
                raise ValueError(f"value: must be None for {self.type.value}")
            if self.detail is not None:
                raise ValueError(f"detail: must be None for {self.type.value}")

        null_path = (
            self.type == EvidenceType.DELEGATED_BUY
            or self.type == EvidenceType.SAME_SLOT
            or (self.type == EvidenceType.SAME_AMOUNTS and self.detail == "first_buy_spent")
        )
        for i, ref in enumerate(self.refs):
            if null_path and ref.instruction_path is not None:
                raise ValueError(f"refs[{i}]: instruction_path must be None for {self.type.value}")
            if not null_path and not isinstance(ref.instruction_path, str):
                raise ValueError(f"refs[{i}]: instruction_path must be str for {self.type.value}")

        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise TypeError(f"weight: expected float, got {type(self.weight).__name__}")
        w = float(self.weight)
        if math.isnan(w) or not (0 < w < 1):
            raise ValueError("weight: must satisfy 0 < weight < 1")
        if w != round(w, 4):
            raise ValueError("weight: must be rounded to 4 decimal places")
        _set(self, "weight", w)


@dataclass(frozen=True)
class ClusterMember:
    wallet: str
    buyer_rank: int
    received_amount: int

    def __post_init__(self) -> None:
        if not isinstance(self.wallet, str) or not self.wallet:
            raise TypeError(f"wallet: expected non-empty str")
        if isinstance(self.buyer_rank, bool) or not isinstance(self.buyer_rank, int):
            raise TypeError(f"buyer_rank: expected int")
        if self.buyer_rank < 1:
            raise ValueError("buyer_rank: must be >= 1")
        if isinstance(self.received_amount, bool) or not isinstance(self.received_amount, int):
            raise TypeError(f"received_amount: expected int")
        if self.received_amount < 1:
            raise ValueError("received_amount: must be >= 1")


def cluster_id(wallets: Iterable[str]) -> str:
    """Детермінований ідентифікатор: `"c-" + sha256(",".join(sorted(wallets)))[:16]`."""
    joined = ",".join(sorted(wallets))
    return "c-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def cluster_sort_key(cluster: Cluster) -> tuple[int, str]:
    """Ключ порядку кластерів — `(−share_numerator, cluster_id)`."""
    return (-cluster.share_numerator, cluster.cluster_id)


def _noisy_or(weights: Iterable[float]) -> float:
    prod = 1.0
    for w in weights:
        prod *= 1.0 - w
    return round(1.0 - prod, 4)


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    members: tuple[ClusterMember, ...]
    share_numerator: int
    share_denominator: int
    share: float
    evidence: tuple[Evidence, ...]
    confidence: float
    warnings: tuple[ClusterWarning, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cluster_id, str) or not self.cluster_id:
            raise TypeError("cluster_id: expected non-empty str")
        if not isinstance(self.members, tuple):
            raise TypeError(f"members: expected tuple, got {type(self.members).__name__}")
        if len(self.members) < 2:
            raise ValueError("members: must have at least 2 elements")
        for i, m in enumerate(self.members):
            if not isinstance(m, ClusterMember):
                raise TypeError(f"members[{i}]: expected ClusterMember")
        wallets = [m.wallet for m in self.members]
        if len(set(wallets)) != len(wallets):
            raise ValueError("members: wallet addresses must be unique")
        if [m.buyer_rank for m in self.members] != sorted(m.buyer_rank for m in self.members):
            raise ValueError("members: must be sorted by buyer_rank")
        if self.cluster_id != cluster_id(wallets):
            raise ValueError("cluster_id: must equal cluster_id(members' wallets)")

        if isinstance(self.share_numerator, bool) or not isinstance(self.share_numerator, int):
            raise TypeError("share_numerator: expected int")
        expected_num = sum(m.received_amount for m in self.members)
        if self.share_numerator != expected_num:
            raise ValueError("share_numerator: must equal sum of members' received_amount")
        if isinstance(self.share_denominator, bool) or not isinstance(self.share_denominator, int):
            raise TypeError("share_denominator: expected int")
        if self.share_denominator < 1:
            raise ValueError("share_denominator: must be >= 1")
        if self.share_denominator < self.share_numerator:
            raise ValueError("share_denominator: must be >= share_numerator")
        if isinstance(self.share, bool) or not isinstance(self.share, (int, float)):
            raise TypeError("share: expected float")
        if self.share != round(self.share_numerator / self.share_denominator, 4):
            raise ValueError("share: must equal round(share_numerator/share_denominator, 4)")
        if not (0 < self.share <= 1):
            raise ValueError("share: must satisfy 0 < share <= 1")

        if not isinstance(self.evidence, tuple):
            raise TypeError(f"evidence: expected tuple, got {type(self.evidence).__name__}")
        if len(self.evidence) == 0:
            raise ValueError("evidence: must be non-empty")
        for i, e in enumerate(self.evidence):
            if not isinstance(e, Evidence):
                raise TypeError(f"evidence[{i}]: expected Evidence")
        if not any(e.type in LINK_TYPES for e in self.evidence):
            raise ValueError("evidence: must have at least one LINK_TYPES evidence")
        member_set = set(wallets)
        for i, e in enumerate(self.evidence):
            if not set(e.wallets).issubset(member_set):
                raise ValueError(f"evidence[{i}].wallets: must be subset of cluster members")
        if tuple(sorted(self.evidence, key=evidence_sort_key)) != self.evidence:
            raise ValueError("evidence: must be sorted by evidence_sort_key")

        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence: expected float")
        if not (0 < self.confidence < 1):
            raise ValueError("confidence: must satisfy 0 < confidence < 1")
        if self.confidence != round(float(self.confidence), 4):
            raise ValueError("confidence: must be rounded to 4 decimal places")
        plain = _noisy_or(e.weight for e in self.evidence)
        if ClusterWarning.POSSIBLE_PRUNING_ARTIFACT in (self.warnings if isinstance(self.warnings, tuple) else ()):
            if not (0 < self.confidence <= plain):
                raise ValueError("confidence: with possible_pruning_artifact must satisfy 0 < confidence <= noisy-OR")
        elif self.confidence != plain:
            raise ValueError(f"confidence: must equal noisy-OR {plain}")

        if not isinstance(self.warnings, tuple):
            raise TypeError(f"warnings: expected tuple, got {type(self.warnings).__name__}")
        for i, w in enumerate(self.warnings):
            if not isinstance(w, ClusterWarning):
                if not isinstance(w, str):
                    raise TypeError(f"warnings[{i}]: expected ClusterWarning")
                _set(self, "warnings", tuple(ClusterWarning(x) if isinstance(x, str) else x for x in self.warnings))
                break
        if len(set(self.warnings)) != len(self.warnings):
            raise ValueError("warnings: must be unique")
        if tuple(sorted(self.warnings, key=lambda w: w.value)) != self.warnings:
            raise ValueError("warnings: must be sorted lexicographically")
        has_slot = any(e.basis == TimeBasis.SLOT for e in self.evidence if e.type in LINK_TYPES)
        if has_slot != (ClusterWarning.SLOT_TIME_FALLBACK in self.warnings):
            raise ValueError("slot_time_fallback warning must match presence of slot-basis link evidence")


def diagnostic_sort_key(d: DiagnosticSignal) -> tuple[str, int, tuple[str, ...]]:
    return (d.kind.value, -1 if d.value is None else d.value, d.wallets)


@dataclass(frozen=True)
class DiagnosticSignal:
    kind: DiagnosticKind
    wallets: tuple[str, ...]
    value: int | None
    count: int
    detail: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DiagnosticKind):
            if not isinstance(self.kind, str):
                raise TypeError(f"kind: expected DiagnosticKind, got {type(self.kind).__name__}")
            _set(self, "kind", DiagnosticKind(self.kind))
        if not isinstance(self.wallets, tuple) or len(self.wallets) == 0:
            raise ValueError("wallets: must be a non-empty tuple")
        if len(set(self.wallets)) != len(self.wallets):
            raise ValueError("wallets: must be unique")
        if tuple(sorted(self.wallets)) != self.wallets:
            raise ValueError("wallets: must be sorted")
        for i, w in enumerate(self.wallets):
            if not isinstance(w, str) or not w:
                raise TypeError(f"wallets[{i}]: expected non-empty str")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ValueError("count: must be an int >= 1")
        if not isinstance(self.detail, tuple):
            raise TypeError(f"detail: expected tuple, got {type(self.detail).__name__}")
        for i, d in enumerate(self.detail):
            if not isinstance(d, str):
                raise TypeError(f"detail[{i}]: expected str")
        if self.kind == DiagnosticKind.FLAGGED_BUYERS_EXCLUDED:
            if self.value is not None:
                raise ValueError("value: must be None for flagged_buyers_excluded")
            if len(self.detail) == 0:
                raise ValueError("detail: must be non-empty for flagged_buyers_excluded")
            for entry in self.detail:
                addr, _, criterion = entry.partition(":")
                if not addr or criterion not in ("known_list", "degree", "one_off_senders",
                                                 "ingest_high_degree", "dust_fanout"):
                    raise ValueError(f"detail: malformed flagged entry {entry!r}")
            if self.count != len(self.wallets):
                raise ValueError("count: must equal len(wallets) for flagged_buyers_excluded")
        elif self.kind == DiagnosticKind.SAME_AMOUNTS_UNLINKED:
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise TypeError("value: expected int for same_amounts_unlinked")
            if len(self.detail) != 1 or self.detail[0] not in ("funding", "first_buy_spent"):
                raise ValueError("detail: must be exactly one of funding|first_buy_spent")
        else:  # SAME_SLOT_UNLINKED
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise TypeError("value: expected int for same_slot_unlinked")
            if len(self.detail) != 0:
                raise ValueError("detail: must be empty for same_slot_unlinked")


@dataclass(frozen=True)
class ClusterCompleteness:
    graph_status: GraphCompletenessStatus
    ingest_status: CompletenessStatus
    missing_count: int
    delegated_complete: bool
    delegated_reason: str | None
    graph_warnings: tuple[GraphWarning, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.graph_status, GraphCompletenessStatus):
            if not isinstance(self.graph_status, str):
                raise TypeError("graph_status: expected GraphCompletenessStatus")
            _set(self, "graph_status", GraphCompletenessStatus(self.graph_status))
        if not isinstance(self.ingest_status, CompletenessStatus):
            if not isinstance(self.ingest_status, str):
                raise TypeError("ingest_status: expected CompletenessStatus")
            _set(self, "ingest_status", CompletenessStatus(self.ingest_status))
        if self.graph_status == GraphCompletenessStatus.COMPLETE:
            if self.ingest_status != CompletenessStatus.COMPLETE:
                raise ValueError("graph complete requires ingest complete")
            if self.missing_count != 0:
                raise ValueError("graph complete requires missing_count == 0")
            if not self.delegated_complete:
                raise ValueError("graph complete requires delegated_complete")
            if self.delegated_reason is not None:
                raise ValueError("graph complete requires delegated_reason None")
        if isinstance(self.missing_count, bool) or not isinstance(self.missing_count, int) or self.missing_count < 0:
            raise ValueError("missing_count: must be an int >= 0")
        if not isinstance(self.delegated_complete, bool):
            raise TypeError("delegated_complete: expected bool")
        if self.delegated_complete != (GraphWarning.DELEGATED_INCOMPLETE not in self.graph_warnings):
            raise ValueError("delegated_complete must match absence of delegated_incomplete warning")
        if self.delegated_complete and self.delegated_reason is not None:
            raise ValueError("delegated_reason: must be None when delegated_complete")
        if not self.delegated_complete and not isinstance(self.delegated_reason, str):
            raise TypeError("delegated_reason: expected str when not delegated_complete")
        if self.ingest_status == CompletenessStatus.INCOMPLETE and self.graph_status != GraphCompletenessStatus.INCOMPLETE:
            raise ValueError("ingest incomplete requires graph incomplete")
        if not isinstance(self.graph_warnings, tuple):
            raise TypeError("graph_warnings: expected tuple")
        for i, w in enumerate(self.graph_warnings):
            if not isinstance(w, GraphWarning):
                if not isinstance(w, str):
                    raise TypeError(f"graph_warnings[{i}]: expected GraphWarning")
                _set(self, "graph_warnings", tuple(GraphWarning(x) if isinstance(x, str) else x for x in self.graph_warnings))
                break

    def can_be_clean(self, wallets_analyzed: int) -> bool:
        return self.graph_status == GraphCompletenessStatus.COMPLETE and wallets_analyzed >= 1


@dataclass(frozen=True)
class ThresholdsSnapshot:
    """Знімок усіх значень `clusters.yaml`, крім `version` — ті самі межі, що й у `ClusterConfig`."""

    link_min_amount_lamports: int
    funding_window_seconds: int
    seconds_per_slot: float
    link_through_flagged_buyers: bool
    link_assets: tuple[str, ...]
    indirect_enabled: bool
    same_amount_natural_max: int
    same_slot_natural_max: int
    same_slot_window_slots: int
    evidence_weights: Any  # dict[str, float] у знімку (серіалізується напряму)
    slot_fallback_multiplier: float
    artifact_buyer_share: float
    artifact_confidence_multiplier: float
    band_clean_max: int
    band_suspicious_max: int

    def __post_init__(self) -> None:
        if isinstance(self.link_min_amount_lamports, bool) or not isinstance(self.link_min_amount_lamports, int) or self.link_min_amount_lamports < 1:
            raise ValueError("link_min_amount_lamports: must be int >= 1")
        if isinstance(self.funding_window_seconds, bool) or not isinstance(self.funding_window_seconds, int) or self.funding_window_seconds < 1:
            raise ValueError("funding_window_seconds: must be int >= 1")
        if isinstance(self.seconds_per_slot, bool) or not isinstance(self.seconds_per_slot, (int, float)):
            raise TypeError("seconds_per_slot: expected number")
        sps = float(self.seconds_per_slot)
        if math.isnan(sps) or sps <= 0:
            raise ValueError("seconds_per_slot: must be > 0")
        if not isinstance(self.link_through_flagged_buyers, bool):
            raise TypeError("link_through_flagged_buyers: expected bool")
        if not isinstance(self.link_assets, tuple) or not self.link_assets:
            raise ValueError("link_assets: must be a non-empty tuple")
        if len(set(self.link_assets)) != len(self.link_assets):
            raise ValueError("link_assets: must be unique")
        for a in self.link_assets:
            if a != "sol" and not (isinstance(a, str) and a.startswith("spl:")):
                raise ValueError(f"link_assets: bad asset {a!r}")
        if not isinstance(self.indirect_enabled, bool):
            raise TypeError("indirect_enabled: expected bool")
        if isinstance(self.same_amount_natural_max, bool) or not isinstance(self.same_amount_natural_max, int) or self.same_amount_natural_max < 1:
            raise ValueError("same_amount_natural_max: must be int >= 1")
        if isinstance(self.same_slot_natural_max, bool) or not isinstance(self.same_slot_natural_max, int) or self.same_slot_natural_max < 1:
            raise ValueError("same_slot_natural_max: must be int >= 1")
        if isinstance(self.same_slot_window_slots, bool) or not isinstance(self.same_slot_window_slots, int) or self.same_slot_window_slots < 0:
            raise ValueError("same_slot_window_slots: must be int >= 0")
        weights = self.evidence_weights
        if not isinstance(weights, dict):
            raise TypeError("evidence_weights: expected dict")
        expected_keys = ("shared_funder", "direct_transfer", "delegated_buy", "recovered_edge",
                         "indirect_link", "same_amounts", "same_slot")
        if tuple(sorted(weights.keys())) != tuple(sorted(expected_keys)):
            raise ValueError("evidence_weights: must have exactly the seven type keys")
        for k in expected_keys:
            v = weights[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise TypeError(f"evidence_weights.{k}: expected number")
            if math.isnan(float(v)) or not (0 < float(v) < 1):
                raise ValueError(f"evidence_weights.{k}: must satisfy 0 < w < 1")
        if not (float(weights["indirect_link"]) < float(weights["shared_funder"])):
            raise ValueError("evidence_weights.indirect_link: must be < shared_funder")
        for name in ("slot_fallback_multiplier", "artifact_buyer_share", "artifact_confidence_multiplier"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise TypeError(f"{name}: expected number")
            fv = float(v)
            lo_ok = fv > 0 and (fv <= 1)
            if math.isnan(fv) or not lo_ok:
                raise ValueError(f"{name}: must satisfy 0 < m <= 1")
        if isinstance(self.band_clean_max, bool) or not isinstance(self.band_clean_max, int) or self.band_clean_max < 0:
            raise ValueError("band_clean_max: must be int >= 0")
        if isinstance(self.band_suspicious_max, bool) or not isinstance(self.band_suspicious_max, int) or self.band_suspicious_max > 100:
            raise ValueError("band_suspicious_max: must be int <= 100")
        if not (self.band_clean_max < self.band_suspicious_max):
            raise ValueError("band_clean_max: must be < band_suspicious_max")


@dataclass(frozen=True)
class ClusterMetadata:
    mint: str
    schema_version: str
    cluster_config_version: int
    thresholds: ThresholdsSnapshot
    graph_schema_version: str
    hub_config_version: int
    address_lists_version: int | None
    lists_applied: bool
    ingest_config_version: int
    ingest_analyzed_at: int
    ingest_source: str
    wallets_analyzed: int
    share_denominator: int
    share_denominator_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.mint, str) or not self.mint:
            raise TypeError("mint: expected non-empty str")
        if self.schema_version != CLUSTER_SCHEMA_VERSION:
            raise ValueError(f"schema_version: must be {CLUSTER_SCHEMA_VERSION!r}")
        if isinstance(self.cluster_config_version, bool) or not isinstance(self.cluster_config_version, int) or self.cluster_config_version < 1:
            raise ValueError("cluster_config_version: must be int >= 1")
        if not isinstance(self.thresholds, ThresholdsSnapshot):
            raise TypeError("thresholds: expected ThresholdsSnapshot")
        if self.graph_schema_version != "002.1":
            raise ValueError("graph_schema_version: must be '002.1'")
        if isinstance(self.hub_config_version, bool) or not isinstance(self.hub_config_version, int) or self.hub_config_version < 1:
            raise ValueError("hub_config_version: must be int >= 1")
        if self.address_lists_version is not None and (isinstance(self.address_lists_version, bool) or not isinstance(self.address_lists_version, int) or self.address_lists_version < 1):
            raise ValueError("address_lists_version: must be int >= 1 or None")
        if not isinstance(self.lists_applied, bool):
            raise TypeError("lists_applied: expected bool")
        if (self.address_lists_version is None) == self.lists_applied:
            raise ValueError("address_lists_version is None iff not lists_applied")
        if isinstance(self.ingest_config_version, bool) or not isinstance(self.ingest_config_version, int) or self.ingest_config_version < 1:
            raise ValueError("ingest_config_version: must be int >= 1")
        if isinstance(self.ingest_analyzed_at, bool) or not isinstance(self.ingest_analyzed_at, int) or self.ingest_analyzed_at < 0:
            raise ValueError("ingest_analyzed_at: must be int >= 0")
        if not isinstance(self.ingest_source, str) or not self.ingest_source:
            raise TypeError("ingest_source: expected non-empty str")
        if isinstance(self.wallets_analyzed, bool) or not isinstance(self.wallets_analyzed, int) or self.wallets_analyzed < 0:
            raise ValueError("wallets_analyzed: must be int >= 0")
        if isinstance(self.share_denominator, bool) or not isinstance(self.share_denominator, int) or self.share_denominator < 0:
            raise ValueError("share_denominator: must be int >= 0")
        if (self.share_denominator == 0) != (self.wallets_analyzed == 0):
            raise ValueError("share_denominator == 0 iff wallets_analyzed == 0")
        if self.share_denominator_kind != SHARE_DENOMINATOR_KIND:
            raise ValueError(f"share_denominator_kind: must be {SHARE_DENOMINATOR_KIND!r}")


@dataclass(frozen=True)
class ClusterResult:
    metadata: ClusterMetadata
    completeness: ClusterCompleteness
    clusters: tuple[Cluster, ...]
    diagnostics: tuple[DiagnosticSignal, ...]
    risk_score: int
    computed_band: RiskBand
    band: RiskBand
    band_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ClusterMetadata):
            raise TypeError("metadata: expected ClusterMetadata")
        if not isinstance(self.completeness, ClusterCompleteness):
            raise TypeError("completeness: expected ClusterCompleteness")
        if not isinstance(self.clusters, tuple):
            raise TypeError("clusters: expected tuple")
        for i, c in enumerate(self.clusters):
            if not isinstance(c, Cluster):
                raise TypeError(f"clusters[{i}]: expected Cluster")
            if c.share_denominator != self.metadata.share_denominator:
                raise ValueError("clusters: every share_denominator must equal metadata.share_denominator")
        seen: set[str] = set()
        for c in self.clusters:
            for m in c.members:
                if m.wallet in seen:
                    raise ValueError("clusters: members must not overlap")
                seen.add(m.wallet)
        if tuple(sorted(self.clusters, key=cluster_sort_key)) != self.clusters:
            raise ValueError("clusters: must be sorted by cluster_sort_key")
        if not isinstance(self.diagnostics, tuple):
            raise TypeError("diagnostics: expected tuple")
        for i, d in enumerate(self.diagnostics):
            if not isinstance(d, DiagnosticSignal):
                raise TypeError(f"diagnostics[{i}]: expected DiagnosticSignal")
        if tuple(sorted(self.diagnostics, key=diagnostic_sort_key)) != self.diagnostics:
            raise ValueError("diagnostics: must be sorted by diagnostic_sort_key")

        if isinstance(self.risk_score, bool) or not isinstance(self.risk_score, int) or not (0 <= self.risk_score <= 100):
            raise ValueError("risk_score: must be an int in 0..100")
        expected_score = math.floor(100 * sum(c.share * c.confidence for c in self.clusters) + 0.5)
        if self.risk_score != expected_score:
            raise ValueError(f"risk_score: must equal floor(100*sum(share*confidence)+0.5) = {expected_score}")
        for name in ("computed_band", "band"):
            v = getattr(self, name)
            if not isinstance(v, RiskBand):
                if not isinstance(v, str):
                    raise TypeError(f"{name}: expected RiskBand")
                _set(self, name, RiskBand(v))

        th = self.metadata.thresholds
        if self.risk_score <= th.band_clean_max:
            expected_computed = RiskBand.CLEAN
        elif self.risk_score <= th.band_suspicious_max:
            expected_computed = RiskBand.SUSPICIOUS
        else:
            expected_computed = RiskBand.HIGH_CONCENTRATION
        if self.computed_band != expected_computed:
            raise ValueError("computed_band: must match risk_score and thresholds")

        if not isinstance(self.band_reasons, tuple) or len(self.band_reasons) == 0:
            raise ValueError("band_reasons: must be a non-empty tuple")
        for r in self.band_reasons:
            if not isinstance(r, str) or not r:
                raise TypeError("band_reasons: must be non-empty strings")

        wallets_analyzed = self.metadata.wallets_analyzed
        can_clean = self.completeness.can_be_clean(wallets_analyzed)
        if self.band == RiskBand.CLEAN:
            if not (self.computed_band == RiskBand.CLEAN and can_clean):
                raise ValueError("band clean requires computed clean and complete data")
            if len(self.clusters) == 0:
                if self.band_reasons != ("no_clusters_on_complete_data",):
                    raise ValueError("band_reasons: clean without clusters must be ('no_clusters_on_complete_data',)")
            elif self.band_reasons != ("clusters_within_clean_threshold",):
                raise ValueError("band_reasons: clean with clusters must be ('clusters_within_clean_threshold',)")
        elif self.band == RiskBand.INSUFFICIENT_DATA:
            if wallets_analyzed == 0:
                if "empty_input" not in self.band_reasons:
                    raise ValueError("band_reasons: empty input must contain 'empty_input'")
                if self.clusters or self.risk_score != 0:
                    raise ValueError("empty input requires no clusters and risk_score 0")
            else:
                if self.computed_band == RiskBand.CLEAN and can_clean:
                    raise ValueError("band insufficient_data requires incomplete data or empty input")
                if self.computed_band != RiskBand.CLEAN and can_clean:
                    raise ValueError("band: suspicious/high on complete data must stay computed")
                expected: set[str] = set()
                if self.completeness.ingest_status != CompletenessStatus.COMPLETE:
                    expected.add("ingest_incomplete")
                if self.completeness.missing_count > 0:
                    expected.add(f"missing_histories:{self.completeness.missing_count}")
                if not self.completeness.delegated_complete:
                    expected.add("delegated_incomplete")
                if not expected.issubset(set(self.band_reasons)):
                    raise ValueError(f"band_reasons: must contain {sorted(expected)}")
                if "empty_input" in self.band_reasons:
                    raise ValueError("band_reasons: 'empty_input' only for empty input")
        else:
            if self.computed_band in (RiskBand.SUSPICIOUS, RiskBand.HIGH_CONCENTRATION):
                if self.band != self.computed_band:
                    raise ValueError("band: suspicious/high is a lower bound and must stay computed")
            if self.band_reasons != ("clusters_share_weighted",):
                raise ValueError("band_reasons: non-clean complete bands must be ('clusters_share_weighted',)")
