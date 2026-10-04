# impl: FR-002-04, FR-002-13, FR-002-21
"""Серіалізація результату фічі 002 за `contracts/graph-result.schema.json` (T-040; schema 002.1).

Що робить: `to_dict(result) -> dict` відображає `GraphResult` у структуру, валідну проти схеми контракту;
`to_json(result) -> str` — той самий вміст як канонічний JSON (`sort_keys=True`, `ensure_ascii=False`,
`separators=(",", ":")`, `allow_nan=False`). Вихід детермінований (FR-002-04, SC-004) і обертається без втрат:
`json.loads(to_json(r)) == to_dict(r)`.

Як користуватись: `to_json(GraphService(config).analyze(ingest))`. Версію схеми результату несе
`metadata.schema_version` (`GRAPH_SCHEMA_VERSION`, "002.1"); вона не залежить від схеми 001 (FR-002-21).

Правила відображення (явне, поле за полем — НЕ `dataclasses.asdict`: схема має `additionalProperties: false`,
а `GraphCompleteness` несе службове `_token` і виведені властивості `status`/`ingest_status`):
- `frozenset` ролей → відсортований список рядків (порядок `frozenset` залежить від `PYTHONHASHSEED`);
- `StrEnum` → рядкове значення, `Asset` → точний `str`, `tuple` → `list`;
- `None` → `null` лише там, де схема це дозволяє (`buyer_rank`, `unexpanded`, `one_off_share`, `median_to_buyers`,
  `address_lists_version`, `lists_version`, `buyers_reason`, `delegated_reason`, `measured`/`threshold` для
  `known_list`, `asset`/`amount`/`decimals`/час/`instruction_path` ребер); жодне інше значення не підміняється;
- частки (`*_share`, `warn_share`, `one_off_share`) завжди `float`: рівні результати (`1 == 1.0`) дають однакові
  байти; суми, лічильники й пилові `measured`/`threshold` — завжди `int` (FR-002-22: лампорти без десяткової частини);
- порядок елементів списків — канонічний порядок моделі (вершини за адресою, ребра за ключем, `refs`, `missing`,
  `pruned`, `buyer_flags`, критерії, попередження); тут нічого не пересортовується, окрім ролей;
  порядок ключів фіксує `to_json`.

Залежності: `graph.model` (за планом `graph.serialize` залежить лише від нього). `pruned`, `buyer_flags` і `report`
— типи `unmask.hubs`; їх читають за полями, а не за класами (як і `GraphResult`, що `hubs` не імпортує): відсутнє
поле, чужий тип перелічення чи не число там, де потрібне число, — `TypeError`, а не мовчазний пропуск чи `{}`.
`to_dict` не пише на диск і не читає годинника; значення NaN/inf пропускає лише `to_dict`, а `to_json`
відхиляє їх (`ValueError`, `allow_nan=False`): JSON їх не має.
"""

from __future__ import annotations

import json
from typing import Any

from unmask.graph.model import (
    Edge,
    EdgeRef,
    GraphCompleteness,
    GraphMetadata,
    GraphResult,
    GraphWarning,
    HubCriterion,
    MissingRef,
    Node,
    NodeMeasures,
    ThresholdsSnapshot,
    UnexpandedMark,
)

# Критерії, чиї `measured`/`threshold` — частки (float), а не лічильники чи лампорти (int).
_SHARE_CRITERIA = frozenset({HubCriterion.ONE_OFF_SENDERS})


# --- Читання полів і типи значень -----------------------------------------------------------


def _get(where: str, obj: Any, name: str) -> Any:
    """Поле обʼєкта `unmask.hubs`; відсутнє — `TypeError` (а не `AttributeError`)."""
    if not hasattr(obj, name):
        raise TypeError(f"{where}: expected an object with {name!r}, got {type(obj).__name__}")
    return getattr(obj, name)


def _int(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{where}: expected int, got {value!r}")
    return int(value)


def _opt_int(where: str, value: Any) -> int | None:
    return None if value is None else _int(where, value)


def _float(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{where}: expected number, got {value!r}")
    return float(value)


def _opt_float(where: str, value: Any) -> float | None:
    return None if value is None else _float(where, value)


def _bool(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{where}: expected bool, got {value!r}")
    return value


def _str(where: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{where}: expected str, got {value!r}")
    return str(value)  # точний `str`: `StrEnum`/`Asset` не просочуються


def _opt_str(where: str, value: Any) -> str | None:
    return None if value is None else _str(where, value)


def _seq(where: str, value: Any) -> tuple:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise TypeError(f"{where}: expected a collection, got {value!r}")
    return tuple(value)


def _enum_value(where: str, value: Any, enum: type) -> str:
    if not isinstance(value, enum):
        raise TypeError(f"{where}: expected {enum.__name__}, got {value!r}")
    return _str(where, value.value)


# --- Вершини, ребра, виміри -----------------------------------------------------------------


def _measures(measures: Any) -> dict[str, Any]:
    if not isinstance(measures, NodeMeasures):
        raise TypeError(f"measures: expected NodeMeasures, got {measures!r}")
    return {
        "degree": _int("measures.degree", measures.degree),
        "unique_senders": _int("measures.unique_senders", measures.unique_senders),
        "one_off_senders": _int("measures.one_off_senders", measures.one_off_senders),
        "one_off_share": _opt_float("measures.one_off_share", measures.one_off_share),
        "buyer_fanout": _int("measures.buyer_fanout", measures.buyer_fanout),
        "median_to_buyers": _opt_int("measures.median_to_buyers", measures.median_to_buyers),
    }


def _unexpanded(mark: UnexpandedMark | None) -> dict[str, Any] | None:
    if mark is None:
        return None
    if not isinstance(mark, UnexpandedMark):
        raise TypeError(f"node.unexpanded: expected UnexpandedMark, got {mark!r}")
    return {
        "reason": _str("unexpanded.reason", mark.reason.value),
        "counterparties_seen": _int("unexpanded.counterparties_seen", mark.counterparties_seen),
        "signatures_seen": _int("unexpanded.signatures_seen", mark.signatures_seen),
        "signatures_truncated": _bool("unexpanded.signatures_truncated", mark.signatures_truncated),
    }


def _node(node: Node) -> dict[str, Any]:
    if not isinstance(node, Node):
        raise TypeError(f"graph.nodes: expected Node, got {node!r}")
    return {
        "address": _str("node.address", node.address),
        # `frozenset` -> відсортований список: порядок множини залежить від PYTHONHASHSEED.
        "roles": sorted(_str("node.roles", role.value) for role in node.roles),
        "depth": _int("node.depth", node.depth),
        "buyer_rank": _opt_int("node.buyer_rank", node.buyer_rank),
        "address_type": _str("node.address_type", node.address_type.value),
        "unexpanded": _unexpanded(node.unexpanded),
        "measures": _measures(node.measures),
    }


def _ref(ref: EdgeRef) -> dict[str, Any]:
    return {
        "signature": _str("ref.signature", ref.signature),
        "slot": _int("ref.slot", ref.slot),
        "instruction_path": _opt_str("ref.instruction_path", ref.instruction_path),
    }


def _edge(edge: Edge) -> dict[str, Any]:
    if not isinstance(edge, Edge):
        raise TypeError(f"edge: expected Edge, got {edge!r}")
    return {
        "kind": _str("edge.kind", edge.kind.value),
        "sender": _str("edge.sender", edge.sender),
        "receiver": _str("edge.receiver", edge.receiver),
        "asset": None if edge.asset is None else str(edge.asset),  # `Asset` -> точний `str`
        "amount": _opt_int("edge.amount", edge.amount),
        "decimals": _opt_int("edge.decimals", edge.decimals),
        "count": _int("edge.count", edge.count),
        "first_slot": _int("edge.first_slot", edge.first_slot),
        "last_slot": _int("edge.last_slot", edge.last_slot),
        "first_time": _opt_int("edge.first_time", edge.first_time),
        "last_time": _opt_int("edge.last_time", edge.last_time),
        "refs": [_ref(r) for r in edge.refs],
    }


# --- Метадані й повнота ---------------------------------------------------------------------


def _thresholds(t: ThresholdsSnapshot) -> dict[str, Any]:
    if not isinstance(t, ThresholdsSnapshot):
        raise TypeError(f"metadata.thresholds: expected ThresholdsSnapshot, got {t!r}")
    return {
        "degree_threshold": _int("thresholds.degree_threshold", t.degree_threshold),
        "one_off_senders_share": _float("thresholds.one_off_senders_share", t.one_off_senders_share),
        "one_off_min_senders": _int("thresholds.one_off_min_senders", t.one_off_min_senders),
        "giant_component_warn_share": _float("thresholds.giant_component_warn_share", t.giant_component_warn_share),
        "prune_off_curve": _bool("thresholds.prune_off_curve", t.prune_off_curve),
        "prune_ingest_high_degree": _bool("thresholds.prune_ingest_high_degree", t.prune_ingest_high_degree),
        "dust_amount_lamports": _int("thresholds.dust_amount_lamports", t.dust_amount_lamports),
        "dust_min_fanout": _int("thresholds.dust_min_fanout", t.dust_min_fanout),
    }


def _metadata(m: GraphMetadata) -> dict[str, Any]:
    return {
        "mint": _str("metadata.mint", m.mint),
        "schema_version": _str("metadata.schema_version", m.schema_version),
        "ingest_analyzed_at": _int("metadata.ingest_analyzed_at", m.ingest_analyzed_at),
        "ingest_config_version": _int("metadata.ingest_config_version", m.ingest_config_version),
        "ingest_source": _str("metadata.ingest_source", m.ingest_source),
        "wallets_analyzed": _int("metadata.wallets_analyzed", m.wallets_analyzed),
        "hub_config_version": _int("metadata.hub_config_version", m.hub_config_version),
        "address_lists_version": _opt_int("metadata.address_lists_version", m.address_lists_version),
        "lists_applied": _bool("metadata.lists_applied", m.lists_applied),
        "thresholds": _thresholds(m.thresholds),
        "nodes_total": _int("metadata.nodes_total", m.nodes_total),
        "edges_total": _int("metadata.edges_total", m.edges_total),
    }


def _missing(item: MissingRef) -> dict[str, Any]:
    return {
        "wallet": _str("missing.wallet", item.wallet),
        "depth": _int("missing.depth", item.depth),
        "reason": _str("missing.reason", item.reason.value),
        "detail": _str("missing.detail", item.detail),
    }


def _completeness(c: GraphCompleteness) -> dict[str, Any]:
    # `status` і `ingest_status` — властивості, не поля моделі: у вихід кладуться явно.
    return {
        "status": _str("completeness.status", c.status.value),
        "ingest_status": _str("completeness.ingest_status", c.ingest_status.value),
        "missing": [_missing(m) for m in c.missing],
        "buyers_complete": _bool("completeness.buyers_complete", c.buyers_complete),
        "buyers_reason": _opt_str("completeness.buyers_reason", c.buyers_reason),
        "delegated_complete": _bool("completeness.delegated_complete", c.delegated_complete),
        "delegated_reason": _opt_str("completeness.delegated_reason", c.delegated_reason),
    }


# --- Записи відсікання, позначки, звіт (типи `unmask.hubs`: за полями) -----------------------


def _hit(hit: Any) -> dict[str, Any]:
    where = "criterion_hit"
    criterion = _get(where, hit, "criterion")
    name = _enum_value(f"{where}.criterion", criterion, HubCriterion)
    raw_measured, raw_threshold = _get(where, hit, "measured"), _get(where, hit, "threshold")
    if criterion in _SHARE_CRITERIA:  # частка: float, щоб 1 і 1.0 не давали різних байтів
        measured, threshold = _opt_float(f"{where}.measured", raw_measured), _opt_float(f"{where}.threshold", raw_threshold)
    else:  # лічильники й лампорти — int; `known_list` не несе чисел (`None`)
        measured, threshold = _opt_int(f"{where}.measured", raw_measured), _opt_int(f"{where}.threshold", raw_threshold)
    return {
        "criterion": name,
        "measured": measured,
        "threshold": threshold,
        "detail": _str(f"{where}.detail", _get(where, hit, "detail")),
        "lists_version": _opt_int(f"{where}.lists_version", _get(where, hit, "lists_version")),
    }


def _hits(where: str, record: Any) -> list[dict[str, Any]]:
    return [_hit(h) for h in _seq(f"{where}.criteria", _get(where, record, "criteria"))]


def _prune_record(record: Any) -> dict[str, Any]:
    where = "prune_record"
    return {
        "address": _str(f"{where}.address", _get(where, record, "address")),
        "criteria": _hits(where, record),
        "incident_edges": [_edge(e) for e in _seq(f"{where}.incident_edges", _get(where, record, "incident_edges"))],
        "measures": _measures(_get(where, record, "measures")),
        "config_version": _int(f"{where}.config_version", _get(where, record, "config_version")),
        "lists_version": _opt_int(f"{where}.lists_version", _get(where, record, "lists_version")),
    }


def _buyer_flag(flag: Any) -> dict[str, Any]:
    where = "buyer_flag"
    return {
        "address": _str(f"{where}.address", _get(where, flag, "address")),
        "buyer_rank": _int(f"{where}.buyer_rank", _get(where, flag, "buyer_rank")),
        "criteria": _hits(where, flag),
        "measures": _measures(_get(where, flag, "measures")),
    }


def _snapshot(where: str, snap: Any) -> dict[str, Any]:
    return {
        "nodes": _int(f"{where}.nodes", _get(where, snap, "nodes")),
        "edges": _int(f"{where}.edges", _get(where, snap, "edges")),
        "components": _int(f"{where}.components", _get(where, snap, "components")),
        "buyers_total": _int(f"{where}.buyers_total", _get(where, snap, "buyers_total")),
        "buyers_in_largest_component": _int(
            f"{where}.buyers_in_largest_component", _get(where, snap, "buyers_in_largest_component")),
        "largest_component_buyer_share": _float(
            f"{where}.largest_component_buyer_share", _get(where, snap, "largest_component_buyer_share")),
        "isolated_buyers": _int(f"{where}.isolated_buyers", _get(where, snap, "isolated_buyers")),
    }


def _report(report: Any) -> dict[str, Any]:
    where = "report"
    return {
        "before": _snapshot("report.before", _get(where, report, "before")),
        "after": _snapshot("report.after", _get(where, report, "after")),
        "pruned_nodes": _int("report.pruned_nodes", _get(where, report, "pruned_nodes")),
        "pruned_edges": _int("report.pruned_edges", _get(where, report, "pruned_edges")),
        "warn_share": _float("report.warn_share", _get(where, report, "warn_share")),
        "warnings": [_enum_value("report.warnings", w, GraphWarning)
                     for w in _seq("report.warnings", _get(where, report, "warnings"))],
    }


# --- Публічний інтерфейс --------------------------------------------------------------------


def to_dict(result: GraphResult) -> dict[str, Any]:
    """`GraphResult` → словник за схемою контракту (нова незалежна структура при кожному виклику)."""
    if not isinstance(result, GraphResult):
        raise TypeError(f"serialize: expected GraphResult, got {type(result).__name__}")
    return {
        "metadata": _metadata(result.metadata),
        "completeness": _completeness(result.completeness),
        "graph": {
            "nodes": [_node(n) for n in result.graph.nodes],
            "edges": [_edge(e) for e in result.graph.edges],
        },
        "pruned": [_prune_record(r) for r in _seq("result.pruned", result.pruned)],
        "buyer_flags": [_buyer_flag(f) for f in _seq("result.buyer_flags", result.buyer_flags)],
        "report": _report(result.report),
    }


def to_json(result: GraphResult) -> str:
    """Канонічний JSON: ключі відсортовані, кирилиця не екранується, без зайвих пробілів, без NaN/inf."""
    return json.dumps(
        to_dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
