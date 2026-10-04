# impl: FR-002-07, FR-002-14, FR-002-22
"""Критерії хаба й правило порогу (принцип VI; contracts/graph-service.md §4; research R-9, R-22).

`evaluate(node, config, *, ingest_counterparty_threshold) -> tuple[CriterionHit, ...]` — усі спрацьовані критерії
вершини (порожній кортеж — не хаб), незалежно від того, чи вершина покупець: захист покупців — у `hubs.prune`
(FR-002-10). Чиста функція: лише читає `node.measures` і пороги `config.thresholds`; констант порогів у коді немає
(принцип III).

Правило порогу (FR-002-14, R-9) — спільне для всіх критеріїв: **рівно поріг ніколи не спрацьовує, нерівність
строга**; напрямок — властивість критерію (R-22). Тут реалізовано:

- `degree` — `measures.degree > degree_threshold`;
- `one_off_senders` — передумова `unique_senders >= one_off_min_senders` (включно; не поріг хаба) **і**
  `one_off_share > one_off_senders_share`;
- `dust_fanout` (FR-002-22, R-22) — передумова `buyer_fanout >= dust_min_fanout` (включно) **і**
  `median_to_buyers < dust_amount_lamports` (строго менше: мало — пил). `buyer_fanout == 0` (`median_to_buyers is
  None`) — не застосовний. Перемикача немає: вимкнення — `dust_amount_lamports: 1` (медіана суми ребра ≥ 1).

Правило закодоване в типі: `CriterionHit` на порозі (чи по «неправильний» бік від нього) не конструюється —
`degree`/`one_off_senders`/`ingest_high_degree` ⇒ `measured > threshold`; `dust_fanout` ⇒ `measured < threshold`
(T-057). Критерії-джерела `known_list` (список / PDA) та `ingest_high_degree` у
`evaluate` додає T-034; форма їхніх хітів (data-model `CriterionHit`) перевіряється вже тут.

Хіти впорядковані за рядком `criterion` (`degree < dust_fanout < ingest_high_degree < known_list <
one_off_senders`). Залежності: `graph.model`, `hubs.config` (plan «Правило залежностей»; `graph.*` не імпортує
`hubs.*`, тож циклу немає).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unmask.graph.model import HubCriterion, Node
from unmask.hubs.config import ADDRESS_CATEGORIES, HubConfig

__all__ = ["CriterionHit", "evaluate"]

MEASURED = "measured"
DETAIL_HIGH_DEGREE = "unexpanded:high_degree"
DETAIL_OFF_CURVE = "address_type:off_curve"
_LIST_PREFIX = "list:"

# Критерії з порогом «строго більше» (багато — хаб) і тип їхнього `measured`/`threshold`.
_ABOVE: dict[HubCriterion, tuple[type, ...]] = {
    HubCriterion.DEGREE: (int,),
    HubCriterion.ONE_OFF_SENDERS: (int, float),
    HubCriterion.INGEST_HIGH_DEGREE: (int,),
}
# Критерії з порогом «строго менше» (мало — хаб; R-22) і тип їхнього `measured`/`threshold`.
_BELOW: dict[HubCriterion, tuple[type, ...]] = {
    HubCriterion.DUST_FANOUT: (int,),  # медіана сум ребер до покупців і поріг — лампорти
}
_DETAIL: dict[HubCriterion, str] = {
    HubCriterion.DEGREE: MEASURED,
    HubCriterion.ONE_OFF_SENDERS: MEASURED,
    HubCriterion.INGEST_HIGH_DEGREE: DETAIL_HIGH_DEGREE,
    HubCriterion.DUST_FANOUT: MEASURED,
}


def _number(name: str, value: Any, types: tuple[type, ...]) -> None:
    if isinstance(value, bool) or not isinstance(value, types):
        expected = " or ".join(t.__name__ for t in types)
        raise TypeError(f"criterion_hit.{name}: expected {expected}, got {type(value).__name__}")


@dataclass(frozen=True)
class CriterionHit:
    """Одне спрацювання критерію (data-model «CriterionHit»). Хіт на порозі не конструюється (R-9)."""

    criterion: HubCriterion
    measured: int | float | None
    threshold: int | float | None
    detail: str
    lists_version: int | None

    def __post_init__(self) -> None:
        criterion = self.criterion
        if not isinstance(criterion, HubCriterion):
            if not isinstance(criterion, str):
                raise TypeError(f"criterion_hit.criterion: expected HubCriterion, got {type(criterion).__name__}")
            criterion = HubCriterion(criterion)  # невідомий критерій (зокрема signature_cap) -> ValueError
            object.__setattr__(self, "criterion", criterion)
        if not isinstance(self.detail, str):
            raise TypeError(f"criterion_hit.detail: expected str, got {type(self.detail).__name__}")
        if self.lists_version is not None:
            if isinstance(self.lists_version, bool) or not isinstance(self.lists_version, int):
                raise TypeError(
                    f"criterion_hit.lists_version: expected int or None, got {type(self.lists_version).__name__}"
                )

        if criterion in _ABOVE:
            self._check_above(criterion)
        elif criterion in _BELOW:
            self._check_below(criterion)
        elif criterion is HubCriterion.KNOWN_LIST:
            self._check_known_list()
        else:  # новий член HubCriterion без правила порогу не конструюється мовчки
            raise ValueError(f"criterion_hit: criterion {criterion.value!r} has no threshold rule")

    def _check_measured_shape(self, criterion: HubCriterion, types: tuple[type, ...]) -> None:
        _number("measured", self.measured, types)
        _number("threshold", self.threshold, types)
        if self.detail != _DETAIL[criterion]:
            raise ValueError(
                f"criterion_hit: {criterion.value} requires detail {_DETAIL[criterion]!r}, got {self.detail!r}"
            )
        if self.lists_version is not None:
            raise ValueError(f"criterion_hit: {criterion.value} carries no lists_version")

    def _check_above(self, criterion: HubCriterion) -> None:
        self._check_measured_shape(criterion, _ABOVE[criterion])
        # `not (a > b)`, а не `a <= b`: NaN теж відхиляється (R-9: спрацювання — лише строго понад поріг).
        if not self.measured > self.threshold:
            raise ValueError(
                f"criterion_hit: {criterion.value} requires measured > threshold "
                f"(measured={self.measured!r}, threshold={self.threshold!r}); at threshold is not a hub"
            )

    def _check_below(self, criterion: HubCriterion) -> None:
        self._check_measured_shape(criterion, _BELOW[criterion])
        # Медіана сум ребер — ціле ≥ 1 (інваріант `NodeMeasures.median_to_buyers`).
        if self.measured < 1:
            raise ValueError(f"criterion_hit: {criterion.value} requires measured >= 1, got {self.measured!r}")
        # R-9/R-22: рівно поріг ніколи не спрацьовує; для «пилу» — лише строго нижче порогу.
        if not self.measured < self.threshold:
            raise ValueError(
                f"criterion_hit: {criterion.value} requires measured < threshold "
                f"(measured={self.measured!r}, threshold={self.threshold!r}); at threshold is not a hub"
            )

    def _check_known_list(self) -> None:
        if self.measured is not None or self.threshold is not None:
            raise ValueError("criterion_hit: known_list carries no measured/threshold")
        if self.detail.startswith(_LIST_PREFIX):
            category = self.detail[len(_LIST_PREFIX):]
            if category not in ADDRESS_CATEGORIES:
                raise ValueError(f"criterion_hit: unknown address list category in detail {self.detail!r}")
            if self.lists_version is None or self.lists_version < 1:
                raise ValueError("criterion_hit: known_list from a list requires lists_version >= 1")
        elif self.detail == DETAIL_OFF_CURVE:
            if self.lists_version is not None:
                raise ValueError("criterion_hit: known_list by address_type carries no lists_version")
        else:
            raise ValueError(f"criterion_hit: known_list detail must be 'list:<category>' or {DETAIL_OFF_CURVE!r}")


def _sort_key(hit: CriterionHit) -> str:
    return hit.criterion.value


def evaluate(node: Node, config: HubConfig, *, ingest_counterparty_threshold: int) -> tuple[CriterionHit, ...]:
    """Усі спрацьовані критерії вершини, упорядковані за рядком `criterion`; `()` — не хаб.

    `ingest_counterparty_threshold` — `metadata.counterparty_threshold` збору 001 (int ≥ 1); поріг хіта
    `ingest_high_degree` (критерій додає T-034).
    """
    if not isinstance(node, Node):
        raise TypeError(f"evaluate: expected Node, got {type(node).__name__}")
    if not isinstance(config, HubConfig):
        raise TypeError(f"evaluate: expected HubConfig, got {type(config).__name__}")
    if isinstance(ingest_counterparty_threshold, bool) or not isinstance(ingest_counterparty_threshold, int):
        raise TypeError(
            f"evaluate: ingest_counterparty_threshold: expected int, got {type(ingest_counterparty_threshold).__name__}"
        )
    if ingest_counterparty_threshold < 1:
        raise ValueError("evaluate: ingest_counterparty_threshold must be >= 1")

    t = config.thresholds
    m = node.measures
    hits: list[CriterionHit] = []

    if m.degree > t.degree_threshold:
        hits.append(CriterionHit(HubCriterion.DEGREE, m.degree, t.degree_threshold, MEASURED, None))

    # Передумова (включно) — розмір вибірки відправників, не поріг хаба (R-8, R-9). `one_off_share is None` ⇔
    # відправників немає — критерій не застосовний за будь-якої передумови (і за `HubThresholds`, зібраного в
    # пам'яті в обхід межі `one_off_min_senders >= 2` завантажувача).
    if (
        m.one_off_share is not None
        and m.unique_senders >= t.one_off_min_senders
        and m.one_off_share > t.one_off_senders_share
    ):
        hits.append(
            CriterionHit(HubCriterion.ONE_OFF_SENDERS, m.one_off_share, t.one_off_senders_share, MEASURED, None)
        )

    # Пилове роздавання (FR-002-22, R-22): передумова — кількість різних покупців, яким вершина надіслала SOL
    # (`buyer_fanout`, не `degree`), включно; поріг — верхня медіана сум цих ребер, строго менше. `median is None`
    # ⇔ `buyer_fanout == 0` — не застосовний (і за `HubThresholds` у пам'яті з `dust_min_fanout < 2`).
    if (
        m.median_to_buyers is not None
        and m.buyer_fanout >= t.dust_min_fanout
        and m.median_to_buyers < t.dust_amount_lamports
    ):
        hits.append(CriterionHit(HubCriterion.DUST_FANOUT, m.median_to_buyers, t.dust_amount_lamports, MEASURED, None))

    return tuple(sorted(hits, key=_sort_key))
