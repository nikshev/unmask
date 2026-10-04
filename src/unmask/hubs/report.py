# impl: FR-002-11, FR-002-12
"""Звіт ефекту відсікання хабів (FR-002-11; contracts/graph-service.md §6; data-model «EffectSnapshot»,
«EffectReport»; research R-10, R-16, R-22).

`effect_report(before, after, config, *, lists_applied, delegated_complete) -> EffectReport` — чиста функція:
знімок повного графа (`before`) і графа кластеризації після відсікання (`after`) і попередження. Звіт складається
лише з чисел до/після й `warnings[]`; поля «ok»/«clean» немає й не буде (принцип V: звіт ніколи не каже «чисто»).

Знімок (`EffectSnapshot`, R-10):
- компоненти — слабка зв'язність по ребрах обох видів; рахує їх існуючий union-find `graph.components` (T-027),
  тут власного обходу немає;
- «найбільша компонента серед покупців» — компонента з найбільшою кількістю ПОКУПЦІВ, не вершин (компонента з 50
  джерел і одним покупцем нічого не склеює); на частку правило нічиєї не впливає — лише на те, яку компоненту
  названо, а звіт її не називає;
- `largest_component_buyer_share = buyers_in_largest_component / buyers_total`; знаменник — УСІ покупці графа,
  включно з ізольованими (`wallets_analyzed`), а не лише ті, що мають ребра; 0 покупців → `0.0`;
- `isolated_buyers` — покупці без жодного ребра (будь-якого виду, у будь-якому напрямку).

Попередження (порядок — за рядковим значенням, без дублів):
- `giant_component` ⇔ `after.largest_component_buyer_share > config.thresholds.giant_component_warn_share`
  (СТРОГО більше, FR-002-14; поріг — з версіонованого конфігу, принцип III);
- `delegated_incomplete` ⇔ `not delegated_complete` (аналіз делегованих купівель неповний — частка може бути
  заниженою, бо частини ребер `delegated_buy` немає);
- `address_lists_not_applied` ⇔ `not lists_applied` (FR-002-12): списки адрес не застосовано (`config.lists is
  None`), тож відсутність спрацювань `list:*` — не «хабів немає», і звіт каже це явно (принцип V). Порожні
  категорії при завантаженому файлі — `lists_applied=True`, попередження немає. `lists_applied` — аргумент, а не
  поле `EffectReport`, тож інваріант «`metadata.lists_applied == False` ⇒ попередження» перевіряє `GraphResult`
  (T-039) і схема; тут його єдине джерело.
`empty_graph`/`all_sources_pruned` додає T-038 (R-16).

`after` має бути відсіканням `before`: ті самі покупці, вершини й ребра — підмножини. Інакше «до/після» не
порівнювані й різниці (`pruned_*`) брешуть — гучна `ValueError`, а не тихий звіт.

Складність O((V + E)·α(V)) на граф (R-17): компоненти, одна множина кінців ребер, перевірка підмножин.

Залежності: `graph.model`, `graph.components`, `hubs.config` (plan «Правило залежностей»); жодного `ingest`-коду,
мережі, файлів чи годинника.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from unmask.graph.components import components as graph_components
from unmask.graph.model import FundingGraph, GraphWarning, NodeRole
from unmask.hubs.config import HubConfig

__all__ = ["EffectReport", "EffectSnapshot", "effect_report"]


# --- Перевірки -------------------------------------------------------------------


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _int(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: expected int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name}: {value} < 0")


def _bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name}: expected bool, got {type(value).__name__}")


def _share_of(buyers_in_largest: int, buyers_total: int) -> float:
    """Єдине місце обчислення частки — і для побудови, і для перевірки інваріанта знімка."""
    return buyers_in_largest / buyers_total if buyers_total else 0.0


def _warn_order(w: GraphWarning) -> str:
    return w.value


# --- Типи ------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectSnapshot:
    """Знімок графа для звіту (R-10). Частка зберігається поруч із чисельником і знаменником і звіряється з ними."""

    nodes: int
    edges: int
    components: int
    buyers_total: int
    buyers_in_largest_component: int
    largest_component_buyer_share: float
    isolated_buyers: int

    def __post_init__(self) -> None:
        for name in ("nodes", "edges", "components", "buyers_total", "buyers_in_largest_component",
                     "isolated_buyers"):
            _int(f"snapshot.{name}", getattr(self, name))
        if self.components > self.nodes or (self.components == 0) != (self.nodes == 0):
            raise ValueError(f"snapshot: components={self.components} inconsistent with nodes={self.nodes}")
        if self.buyers_total > self.nodes:
            raise ValueError(f"snapshot: buyers_total={self.buyers_total} > nodes={self.nodes}")
        if self.buyers_in_largest_component > self.buyers_total:
            raise ValueError(f"snapshot: buyers_in_largest_component={self.buyers_in_largest_component} "
                             f"> buyers_total={self.buyers_total}")
        if (self.buyers_in_largest_component == 0) != (self.buyers_total == 0):
            raise ValueError("snapshot: the largest buyer component has buyers iff there are buyers")
        if self.isolated_buyers > self.buyers_total:
            raise ValueError(f"snapshot: isolated_buyers={self.isolated_buyers} > buyers_total={self.buyers_total}")
        share = self.largest_component_buyer_share
        if not isinstance(share, float):
            raise TypeError(f"snapshot.largest_component_buyer_share: expected float, got {type(share).__name__}")
        if share != _share_of(self.buyers_in_largest_component, self.buyers_total):
            raise ValueError(f"snapshot.largest_component_buyer_share: {share} != "
                             f"{self.buyers_in_largest_component}/{self.buyers_total}")


@dataclass(frozen=True)
class EffectReport:
    """Звіт ефекту відсікання (FR-002-11): лише числа до/після й попередження. Поля «ok»/«clean» немає."""

    before: EffectSnapshot
    after: EffectSnapshot
    pruned_nodes: int
    pruned_edges: int
    warn_share: float
    warnings: tuple[GraphWarning, ...]

    def __post_init__(self) -> None:
        for name in ("before", "after"):
            if not isinstance(getattr(self, name), EffectSnapshot):
                raise TypeError(f"report.{name}: expected EffectSnapshot, got {type(getattr(self, name)).__name__}")
        _int("report.pruned_nodes", self.pruned_nodes)
        _int("report.pruned_edges", self.pruned_edges)
        if self.pruned_nodes != self.before.nodes - self.after.nodes:
            raise ValueError(f"report.pruned_nodes={self.pruned_nodes} != "
                             f"before.nodes - after.nodes = {self.before.nodes - self.after.nodes}")
        if self.pruned_edges != self.before.edges - self.after.edges:
            raise ValueError(f"report.pruned_edges={self.pruned_edges} != "
                             f"before.edges - after.edges = {self.before.edges - self.after.edges}")
        if self.before.buyers_total != self.after.buyers_total:
            raise ValueError(f"report: buyers_total changed {self.before.buyers_total} -> "
                             f"{self.after.buyers_total}; buyers are never pruned (FR-002-10)")

        ws = self.warn_share
        if isinstance(ws, bool) or not isinstance(ws, (int, float)):
            raise TypeError(f"report.warn_share: expected number, got {type(ws).__name__}")
        if not 0 < ws <= 1:
            raise ValueError(f"report.warn_share: {ws} out of (0, 1]")

        raw = self.warnings
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            raise TypeError(f"report.warnings: expected a collection, got {type(raw).__name__}")
        items = []
        for w in raw:
            if not isinstance(w, str):
                raise TypeError(f"report.warnings: expected GraphWarning, got {w!r}")
            items.append(GraphWarning(w))  # невідоме попередження -> ValueError
        keys = [_warn_order(w) for w in items]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise ValueError("report.warnings: must be strictly ordered by value without duplicates")
        warnings = tuple(items)
        _set(self, "warnings", warnings)

        giant = self.after.largest_component_buyer_share > ws
        if giant != (GraphWarning.GIANT_COMPONENT in warnings):
            raise ValueError(f"report: giant_component must be present iff after share "
                             f"{self.after.largest_component_buyer_share} > warn_share {ws}")


# --- Обчислення ------------------------------------------------------------------


def _snapshot(graph: FundingGraph) -> EffectSnapshot:
    comps = graph_components(graph)
    touched = {e.sender for e in graph.edges}
    touched.update(e.receiver for e in graph.edges)
    buyers = [n.address for n in graph.nodes if NodeRole.BUYER in n.roles]
    in_largest = comps.buyers_in_largest_component()  # компонента з найбільшою кількістю ПОКУПЦІВ (R-10)
    return EffectSnapshot(
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        components=comps.count,
        buyers_total=len(buyers),  # знаменник — усі покупці, включно з ізольованими
        buyers_in_largest_component=in_largest,
        largest_component_buyer_share=_share_of(in_largest, len(buyers)),
        isolated_buyers=sum(1 for b in buyers if b not in touched),
    )


def _check_pruning(before: FundingGraph, after: FundingGraph) -> None:
    """`after` — відсікання `before`: ті самі покупці; вершини й ребра (за ключем) — підмножини."""
    before_nodes = {n.address: n for n in before.nodes}
    extra = sorted(n.address for n in after.nodes if n.address not in before_nodes)
    if extra:
        raise ValueError(f"effect_report: after has nodes absent from before: {extra[:5]}")
    before_buyers = {a for a, n in before_nodes.items() if NodeRole.BUYER in n.roles}
    after_buyers = {n.address for n in after.nodes if NodeRole.BUYER in n.roles}
    if before_buyers != after_buyers:
        raise ValueError(f"effect_report: buyers differ between before and after: "
                         f"{sorted(before_buyers ^ after_buyers)[:5]} (buyers are never pruned, FR-002-10)")
    before_edges = {e.key for e in before.edges}
    extra_edges = [e.key for e in after.edges if e.key not in before_edges]
    if extra_edges:
        raise ValueError(f"effect_report: after has edges absent from before: {extra_edges[:5]}")


def effect_report(before: FundingGraph, after: FundingGraph, config: HubConfig, *, lists_applied: bool,
                  delegated_complete: bool) -> EffectReport:
    """Звіт ефекту відсікання `before` → `after` за порогом `config`. Чиста функція."""
    for name, graph in (("before", before), ("after", after)):
        if not isinstance(graph, FundingGraph):
            raise TypeError(f"effect_report: {name}: expected FundingGraph, got {type(graph).__name__}")
    if not isinstance(config, HubConfig):
        raise TypeError(f"effect_report: config: expected HubConfig, got {type(config).__name__}")
    _bool("effect_report: lists_applied", lists_applied)
    _bool("effect_report: delegated_complete", delegated_complete)
    _check_pruning(before, after)

    snap_before, snap_after = _snapshot(before), _snapshot(after)
    warn_share = config.thresholds.giant_component_warn_share

    warnings: list[GraphWarning] = []
    if not lists_applied:  # FR-002-12: відсутність списків видима, а не схожа на «хабів немає»
        warnings.append(GraphWarning.ADDRESS_LISTS_NOT_APPLIED)
    if snap_after.largest_component_buyer_share > warn_share:  # строго більше (FR-002-14)
        warnings.append(GraphWarning.GIANT_COMPONENT)
    if not delegated_complete:
        warnings.append(GraphWarning.DELEGATED_INCOMPLETE)

    return EffectReport(
        before=snap_before,
        after=snap_after,
        pruned_nodes=snap_before.nodes - snap_after.nodes,
        pruned_edges=snap_before.edges - snap_after.edges,
        warn_share=warn_share,
        warnings=tuple(sorted(warnings, key=_warn_order)),
    )
