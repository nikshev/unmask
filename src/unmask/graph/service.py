# impl: FR-002-04, FR-002-13, FR-002-19, FR-002-20, FR-002-21
"""Публічний вхід фічі 002 (contracts/graph-service.md §7; data-model «GraphMetadata», «GraphResult»).

```
GraphService(config: HubConfig).analyze(result: IngestResult) -> GraphResult
```

Кроки: перевірка входу проти власних метаданих збору → `build_graph` (повний граф, виміри) → `prune_hubs` з порогом
збору `result.metadata.counterparty_threshold` (поріг, за яким 001 зупинив розгортання, — а не значення з
`config/ingest.yaml`: прогін міг бути з іншою версією конфігу збору) → `GraphCompleteness.derive` → `effect_report`
з `lists_applied=outcome.lists_applied` (те саме значення, що йде в метадані, — не обчислюється вдруге) і
`delegated_complete` з повноти → `GraphMetadata` → `GraphResult` (інваріанти результату — у типі, `graph.model`).

Принцип III (FR-002-13): метадані несуть обидві версії (`hub_config_version` — `hubs.yaml`,
`address_lists_version` — `hub_addresses.yaml` або `None`), `lists_applied` і знімок УСІХ восьми порогів `hubs.yaml`
крім `version` (включно з пиловими `dust_amount_lamports`/`dust_min_fanout`, R-22), а також версію конфігу й
джерело збору 001 — два результати з різними версіями розрізняються без читання коду.

FR-002-20: усі дані — лише з `IngestResult`; сервіс не читає файлів, не дивиться на годинник (`ingest_analyzed_at` —
з входу), не ходить у мережу й не тримає стану між викликами (FR-002-04: `analyze(r)` двічі — рівні результати).

Винятки — лише дефекти: `TypeError` (не `IngestResult`, зокрема `Rejection` — відмову споживач обробляє сам; не
`HubConfig`) і `GraphInputError` (вхід суперечить контракту 001). Перевірки входу тут — ті, що звіряють записи з
МЕТАДАНИМИ того самого результату (побудова графа їх не бачить):

- глибини: `funding_depth` у межах контракту 001 (1..3), `transfer.depth`, `missing.depth`, `unexpanded.depth` —
  не глибше `funding_depth` (межа схеми `graph-result.schema.json`: `node.depth`, `missingRef.depth` ≤ 3);
- позначка `signature_cap` — лише з обрізаною історією й не понад поріг збору (інакше 001 позначив би
  `high_degree`): без цього ручний `ingest.json` тихо дав би вершину з `signature_cap`, яку критерій
  `ingest_high_degree` не відсік би. Позначку `high_degree` не понад поріг (`counterparties_seen <=
  counterparty_threshold`; 001 пише `поріг + 1`) відхиляє `hubs.criteria` (T-034) — теж `GraphInputError`, а не
  голий `ValueError` з `CriterionHit`; тест сервісу закріплює це на вході `analyze`.
"""

from __future__ import annotations

from unmask.graph.build import build_graph
from unmask.graph.model import (
    GRAPH_SCHEMA_VERSION,
    MAX_FUNDING_DEPTH,
    GraphCompleteness,
    GraphInputError,
    GraphMetadata,
    GraphResult,
    ThresholdsSnapshot,
)
from unmask.hubs.config import HubConfig, HubThresholds
from unmask.hubs.prune import prune_hubs
from unmask.hubs.report import effect_report
from unmask.ingest.model import IngestResult, UnexpandedReason


def _snapshot(t: HubThresholds) -> ThresholdsSnapshot:
    """Усі пороги `hubs.yaml`, крім `version`, — поле за полем (новий поріг без нового поля знімка ловить тест)."""
    return ThresholdsSnapshot(
        degree_threshold=t.degree_threshold,
        one_off_senders_share=t.one_off_senders_share,
        one_off_min_senders=t.one_off_min_senders,
        giant_component_warn_share=t.giant_component_warn_share,
        prune_off_curve=t.prune_off_curve,
        prune_ingest_high_degree=t.prune_ingest_high_degree,
        dust_amount_lamports=t.dust_amount_lamports,
        dust_min_fanout=t.dust_min_fanout,
    )


def _check_input(result: IngestResult) -> None:
    """Записи результату узгоджені з його власними метаданими збору (контракт 001); інакше `GraphInputError`."""
    meta = result.metadata
    max_depth = meta.funding_depth
    if not 1 <= max_depth <= MAX_FUNDING_DEPTH:
        raise GraphInputError(f"metadata.funding_depth={max_depth} outside contract 001 range 1..{MAX_FUNDING_DEPTH}")
    for t in result.transfers:
        if t.depth > max_depth:
            raise GraphInputError(f"transfer {t.signature}/{t.instruction_path}: depth={t.depth} > "
                                  f"metadata.funding_depth={max_depth}")
    for m in result.completeness.missing:
        if m.depth > max_depth:
            raise GraphInputError(f"missing {m.wallet}: depth={m.depth} > metadata.funding_depth={max_depth}")
    threshold = meta.counterparty_threshold
    for u in result.unexpanded:
        if u.depth > max_depth:
            raise GraphInputError(f"unexpanded {u.wallet}: depth={u.depth} > metadata.funding_depth={max_depth}")
        # `high_degree` не понад поріг ловить `hubs.criteria` на кожній вершині (T-034: `GraphInputError`, незалежно
        # від перемикача критерію; кожна позначка — вершина графа, це гарантує `build_graph`). Тут — лише те, чого
        # критерій не бачить: `signature_cap` критерієм не є, тож його суперечність інакше пройшла б тихо.
        if u.reason is UnexpandedReason.SIGNATURE_CAP:
            if not u.signatures_truncated:
                raise GraphInputError(f"unexpanded {u.wallet}: signature_cap mark without truncated signatures")
            if u.counterparties_seen > threshold:
                raise GraphInputError(
                    f"unexpanded {u.wallet}: signature_cap mark with counterparties_seen={u.counterparties_seen} above "
                    f"metadata.counterparty_threshold={threshold}; collection 001 would have marked high_degree"
                )


class GraphService:
    """Граф фінансування з відсіченими хабами над результатом збору 001. Без стану між викликами."""

    def __init__(self, config: HubConfig) -> None:
        if not isinstance(config, HubConfig):
            raise TypeError(f"GraphService: expected HubConfig, got {type(config).__name__}")
        self._config = config

    @property
    def config(self) -> HubConfig:
        return self._config

    def analyze(self, result: IngestResult) -> GraphResult:
        if not isinstance(result, IngestResult):  # зокрема `Rejection`: відмову споживач обробляє сам
            raise TypeError(f"GraphService.analyze: expected IngestResult (a collection result), "
                            f"got {type(result).__name__}")
        _check_input(result)
        config = self._config
        meta = result.metadata

        full = build_graph(result)
        outcome = prune_hubs(full, config, ingest_counterparty_threshold=meta.counterparty_threshold)
        completeness = GraphCompleteness.derive(result)
        report = effect_report(full, outcome.graph, config, lists_applied=outcome.lists_applied,
                               delegated_complete=completeness.delegated_complete)
        metadata = GraphMetadata(
            mint=meta.mint,
            schema_version=GRAPH_SCHEMA_VERSION,
            ingest_analyzed_at=meta.analyzed_at,
            ingest_config_version=meta.config_version,
            ingest_source=meta.source,
            wallets_analyzed=meta.wallets_analyzed,
            hub_config_version=outcome.config_version,
            address_lists_version=outcome.lists_version,
            lists_applied=outcome.lists_applied,
            thresholds=_snapshot(config.thresholds),
            nodes_total=len(full.nodes),
            edges_total=len(full.edges),
        )
        return GraphResult(
            metadata=metadata,
            completeness=completeness,
            graph=outcome.graph,
            pruned=outcome.records,
            buyer_flags=outcome.buyer_flags,
            report=report,
        )
