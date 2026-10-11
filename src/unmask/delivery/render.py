# impl: FR-004-05
"""Серверний статичний PNG-рендер документа відповіді (R-3, Pillow): справжній граф.

Розкладка детермінована й виводиться лише з даних: вузли всередині кластера й джерела
сортуються за адресою, жодного випадкового й жодної залежності від порядку обходу множин.
Кожен кластер — власне коло (м'яка підкладка кольору кластера); вузли-джерела, що не є
членами кластера, сірі й стоять поза колом свого кластера (першого за порядком, у чиїх
доказах зустрілись). Картинка не стверджує зв'язків, яких немає в доказах (принцип V).
Документ 004.1 несе `source = sorted(via ∪ wallets)`: напрямок і порядок втрачено, тому

| тип доказу                         | зображення                                                    |
|------------------------------------|---------------------------------------------------------------|
| `shared_funder`, `recovered_edge`  | РЕБРА: кожне джерело (не член кластера) -> кожен член          |
| `direct_transfer`, `delegated_buy` | РЕБРО між двома адресами `source`                              |
| `indirect_link`, `same_amounts`,   | ГРУПА: ромб-маркер кольору типу без адреси з тонкими лініями   |
| `same_slot`, невідомі типи         | до кожної адреси `source` («ці адреси в одному доказі», а не   |
|                                    | «переказ між ними»); у легенді — «(group)»                    |

Ребра неорієнтовані, не проходять крізь чужі вершини (обхід дугою за детермінованим
перебором вигинів) і лежать під вузлами й підписами. Колір/товщина — `EDGE_STYLES`;
легенда типів — поруч із легендою кластерів. Ліміти (`_MAX_*`) не дають графу стати кашею:
розкладаються найбільші `_MAX_CLUSTERS` кластерів за часткою (рендер O(1) за кількістю
кластерів), решта вузлів/ребер/кластерів вказується підписом «+K» і лічильниками
`GraphImage`. Структурно зіпсований документ не кидає винятку: некоректні елементи
пропускаються. Текст картинки — лише ASCII (адреси base58, числа, англійські підписи) і
шрифт за умовчанням Pillow (`ImageFont.load_default()`; у Pillow 12 це вбудований
FreeType-шрифт зі скромним набором гліфів). Розмір полотна, палітра й довжина скорочень — з
`delivery.yaml`; решта показових сталих — модульні константи; на висновок не впливають
(FR-004-11).
"""

from __future__ import annotations

import colorsys
import io
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from unmask.delivery.config import DeliveryConfig

__all__ = ["EDGE_STYLES", "GraphImage", "GraphLayout", "LaidCluster", "LaidEdge", "LaidMarker",
           "LaidNode", "edge_legend", "empty_caption", "layout_graph", "render_png"]

Color = tuple[int, int, int]
Box = tuple[int, int, int, int]

_BACKGROUND = (255, 255, 255)
_INK = (0, 0, 0)
_FUNDER = (136, 136, 136)
_TINT = 0.88                 # частка білого в підкладці кола кластера
_LEGEND_HEIGHT = 132
_FOOTER_HEIGHT = 28
_MARGIN = 24
_NODE_RADIUS = 14
_MAX_MEMBERS = 24            # вузлів-членів на кластер; решта — «+K»
_MAX_EXTERNALS = 5           # сірих джерел на кластер
_MAX_EDGES_PER_CLUSTER = 60
_MAX_MARKERS_PER_CLUSTER = 12
_MAX_CLUSTERS = 16           # розкладаються найбільші за часткою; решта — «+K clusters»
_RING_SINGLE_MAX = 12        # далі — два кільця
_RING_SPACING = 66           # мінімальний крок між вузлами кільця (місце під підпис)
_MIN_RING = 44
_DISC_PAD = 8
_EXTERNAL_GAP = 40
_CELL_GAP = 6
_SCALES = (2.0, 1.7, 1.45, 1.2, 1.0, 0.85, 0.7, 0.55, 0.4)
_PAIR_OFFSET = 3             # зсув ребер різних типів між тією самою парою
_MARKER_RADIUS = 6
_ROUTE_STEPS = 24            # відрізків у дузі обходу
_BENDS = (0,) + tuple(sign * mag for mag in (*range(5, 60, 5), *range(60, 130, 10), 150, 180, 220, 280, 340)
                      for sign in (1, -1))  # прогини дуги, перебір по порядку (за модулем)
_CLUSTER_LEGEND_COL = 260
_EDGE_LEGEND_WIDTH = 350
_EDGE_LEGEND_COL = 175
_LEGEND_ROWS = 4

# тип доказу -> (колір, товщина ребра); порядок — пріоритет
EDGE_STYLES: dict[str, tuple[Color, int]] = {
    "shared_funder": ((150, 0, 0), 3),
    "direct_transfer": ((200, 0, 140), 3),
    "recovered_edge": ((130, 80, 0), 2),
    "delegated_buy": ((0, 110, 110), 2),
    "indirect_link": ((120, 0, 200), 2),
    "same_amounts": ((0, 0, 170), 2),
    "same_slot": ((100, 100, 0), 2),
}
_UNKNOWN_STYLE: tuple[Color, int] = ((90, 90, 90), 2)
_PAIR_TYPES = frozenset({"direct_transfer", "delegated_buy"})   # реальна пара адрес
_STAR_TYPES = frozenset({"shared_funder", "recovered_edge"})    # джерело -> кожен член


@dataclass(frozen=True)
class LaidNode:
    """Вершина графа з координатами й підписом (усі координати — цілі пікселі)."""

    address: str
    x: int
    y: int
    cluster: int | None          # індекс кластера в документі (для джерела — його «власник»)
    color: Color
    label: str
    label_box: Box               # [x0, y0, x1, y1) разом з білою підкладкою
    label_xy: tuple[int, int]    # початок тексту для `ImageDraw.text`
    external: bool = False


@dataclass(frozen=True)
class LaidEdge:
    type: str
    a: str
    b: str
    p: tuple[int, int]
    q: tuple[int, int]
    color: Color
    width: int
    points: tuple[tuple[int, int], ...] = ()   # ламана (пряма — 2 точки, обхід дугою — більше)
    mid: tuple[int, int] = (0, 0)              # точка на середині ламаної


@dataclass(frozen=True)
class LaidMarker:
    """Маркер групового доказу: ромб без адреси з тонкими лініями до адрес `source`."""

    type: str
    cluster: int
    x: int
    y: int
    members: tuple[str, ...]     # показані адреси, до яких ведуть лінії
    spokes: tuple[tuple[int, int], ...]
    color: Color


@dataclass(frozen=True)
class LaidCluster:
    index: int
    cx: int
    cy: int
    radius: int
    color: Color
    tag: str = ""
    tag_box: Box = (0, 0, 0, 0)
    tag_xy: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class GraphLayout:
    """Розкладка графа: те, що буде намальовано, і лічильники непоказаного."""

    nodes: tuple[LaidNode, ...] = ()
    edges: tuple[LaidEdge, ...] = ()
    markers: tuple[LaidMarker, ...] = ()
    clusters: tuple[LaidCluster, ...] = ()
    edge_types: tuple[str, ...] = ()
    legend_items: tuple[tuple[str, str], ...] = ()   # (тип, "edge" | "group") у порядку легенди
    hidden_nodes: int = 0
    hidden_edges: int = 0
    hidden_clusters: int = 0
    wallets_total: int = 0


@dataclass(frozen=True)
class GraphImage:
    """PNG-байтівки з розмірами й описом показаного."""

    png: bytes
    width: int
    height: int
    empty: bool
    clusters_shown: int
    wallets_shown: int = 0
    wallets_total: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.png, (bytes, bytearray)) or len(self.png) == 0:
            raise TypeError("png: expected non-empty bytes")
        for name in ("width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}: must be an int >= 1")
        if not isinstance(self.empty, bool):
            raise TypeError("empty: expected bool")
        if isinstance(self.clusters_shown, bool) or not isinstance(self.clusters_shown, int) \
                or self.clusters_shown < 0:
            raise ValueError("clusters_shown: must be an int >= 0")
        if self.empty != (self.clusters_shown == 0):
            raise ValueError("empty must match clusters_shown == 0")
        for name in ("wallets_shown", "wallets_total"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}: must be an int >= 0")
        if self.wallets_shown > self.wallets_total:
            raise ValueError("wallets_shown must not exceed wallets_total")


def _extended_palette(configured: list[tuple[int, int, int]], index: int) -> tuple[int, int, int]:
    """Колір i-го кластера: перші — з `delivery.yaml`, далі — детерміноване коло HSL.

    Модуль за довжиною палітри мовчки зливав би 9-й кластер із 1-м.
    """
    if index < len(configured):
        return configured[index]
    hue = (index * 0.618033988749895) % 1.0  # золоте січення — сусіди максимально далекі
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.9)
    return (int(r * 255), int(g * 255), int(b * 255))


def _short(address: str, prefix_len: int) -> str:
    return address[:prefix_len] + "..." if len(address) > prefix_len else address


def _color(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def empty_caption(doc: dict[str, Any]) -> list[str]:
    """Рядки заглушки порожнього результату: ризик/смуга + підпис (FR-004-12).

    Окрема чиста функція, щоб текст смуги тестувався без OCR: картинка малює рівно ці рядки.
    """
    return [
        f"risk {doc.get('risk_score', 0)}/100 {doc.get('band', '')}",
        "no clusters found",
    ]


def _style(kind: str) -> tuple[Color, int]:
    return EDGE_STYLES.get(kind, _UNKNOWN_STYLE)


def _priority(kind: str) -> tuple[int, str]:
    order = list(EDGE_STYLES)
    return (order.index(kind) if kind in order else len(order), kind)


def edge_legend(items: tuple[tuple[str, str], ...]) -> tuple[str, list[str]]:
    """Заголовок і підписи легенди типів ребер; групові доказі помічені «(group)»."""
    has_group = any(shape == "group" for _, shape in items)
    title = "edge types (diamond = one evidence, not transfers)" if has_group else "edge types"
    labels = [kind.replace("_", " ") + (" (group)" if shape == "group" else "")
              for kind, shape in items]
    return title, labels


Evidence = list[tuple[str, list[str]]]
Normalized = list[tuple[Any, list[str], Evidence]]


def _normalize(clusters: list[Any]) -> Normalized:
    """Кластери без структурних вад: некоректні елементи пропускаються, а не кидають виняток."""
    out: Normalized = []
    for c in clusters:
        if not isinstance(c, dict):
            out.append((0, [], []))
            continue
        raw = c.get("wallets")
        wallets = [w for w in raw if isinstance(w, str)] if isinstance(raw, list) else []
        evidence: Evidence = []
        raw_ev = c.get("evidence")
        if isinstance(raw_ev, list):
            for e in raw_ev:
                if not isinstance(e, dict):
                    continue
                src = e.get("source")
                evidence.append((str(e.get("type", "")),
                                 [a for a in src if isinstance(a, str)] if isinstance(src, list) else []))
        out.append((c.get("supply_share", 0), wallets, evidence))
    return out


def _share_value(share: Any) -> float:
    return float(share) if isinstance(share, (int, float)) and not isinstance(share, bool) \
        and math.isfinite(share) else 0.0


def _evidence_shape(kind: str, source: list[str], members: set[str]
                    ) -> tuple[list[tuple[str, str]], tuple[str, ...] | None]:
    """Реальні пари адрес (ребра) або група адрес (маркер): таблиця в шапці модуля."""
    src = sorted(set(source))
    if len(src) < 2:
        return [], None
    if kind in _PAIR_TYPES and len(src) == 2:
        return [(src[0], src[1])], None
    if kind in _STAR_TYPES:
        outside = [a for a in src if a not in members]
        inside = [a for a in src if a in members]
        if outside and inside:
            return [(o, m) for o in outside for m in inside], None
    return [], tuple(src)


def _text_box(font: Any, text: str) -> tuple[int, int, int, int]:
    """(ширина, висота, зсув X, зсув Y) тексту з 1 пікселем підкладки навколо."""
    left, top, right, bottom = font.getbbox(text)
    return (right - left + 2, bottom - top + 2, left - 1, top - 1)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _seg_dist(px: float, py: float, a: tuple[int, int], b: tuple[int, int]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / length2))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def _bezier(p: tuple[int, int], q: tuple[int, int], bend: float) -> list[tuple[int, int]]:
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    length = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
    cx = mx + -(q[1] - p[1]) / length * bend
    cy = my + (q[0] - p[0]) / length * bend
    pts = []
    for k in range(_ROUTE_STEPS + 1):
        t = k / _ROUTE_STEPS
        pts.append((round((1 - t) ** 2 * p[0] + 2 * (1 - t) * t * cx + t * t * q[0]),
                    round((1 - t) ** 2 * p[1] + 2 * (1 - t) * t * cy + t * t * q[1])))
    return pts


def _route(p: tuple[int, int], q: tuple[int, int], others: list[tuple[int, int]],
           bounds: Box) -> list[tuple[int, int]]:
    """Ламана p-q, що не торкається чужих вершин: пряма, інакше перший вигин без перетину.

    `others` — центри вершин, які не є кінцями ребра. Перебір фіксований і впорядкований
    (`_BENDS`), тож результат детермінований; якщо чисто не виходить ніде — найменше перетинів.
    """
    clearance = _NODE_RADIUS + 2
    reach = max(abs(b) for b in _BENDS) / 2 + clearance + 4
    near = [o for o in others if _seg_dist(o[0], o[1], p, q) <= reach]
    best: tuple[tuple[int, float], list[tuple[int, int]]] | None = None
    for bend in _BENDS:
        pts = [p, q] if bend == 0 else _bezier(p, q, bend)
        if bend != 0 and not all(bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]
                                 for x, y in pts):
            continue
        gaps = [min(_seg_dist(o[0], o[1], a, b) for a, b in zip(pts, pts[1:])) for o in near]
        bad = sum(1 for g in gaps if g < clearance)
        if bad == 0:
            return pts
        score = (bad, -min(gaps))  # менше перетинів, далі — більший найменший зазор
        if best is None or score < best[0]:
            best = (score, pts)
    return best[1] if best else [p, q]


@dataclass
class _Local:
    index: int
    members: list[tuple[str, float, float, float]]   # адреса, x, y, кут назовні (відносно центра)
    externals: list[tuple[str, float, float, float]]
    disc: float
    tag: str
    tag_size: tuple[int, int, int, int]
    extent: tuple[float, float, float, float]        # minx, miny, maxx, maxy відносно центра


def _local_layout(index: int, members: list[str], externals: list[str], hidden_members: int,
                  scale: float, label_w: int, label_h: int, font: Any) -> _Local:
    n = len(members)
    n_out = n if n <= _RING_SINGLE_MAX else math.ceil(n * 0.6)
    n_in = n - n_out
    r_out = 0.0 if n == 1 else max(_MIN_RING, n_out * _RING_SPACING / (2 * math.pi)) * scale
    # півкроку зсуву: 2 члени лежать ліворуч/праворуч, а не на одній прямій із джерелом знизу
    start = -math.pi / 2 + math.pi / n_out
    placed: list[tuple[str, float, float, float]] = []
    for k, addr in enumerate(members):
        if k < n_out:
            ang = start + 2 * math.pi * k / n_out
            r = r_out
        else:
            j = k - n_out
            ang = -math.pi / 2 + math.pi / n_in + 2 * math.pi * j / n_in
            r = r_out * 0.5
        placed.append((addr, r * math.cos(ang), r * math.sin(ang), ang))
    disc = r_out + _NODE_RADIUS + _DISC_PAD
    ext_placed: list[tuple[str, float, float, float]] = []
    m = len(externals)
    for k, addr in enumerate(externals):
        ang = -math.pi / 2 + 2 * math.pi * (k + 0.5) / m
        r = disc + _EXTERNAL_GAP * max(1.0, scale)
        ext_placed.append((addr, r * math.cos(ang), r * math.sin(ang), ang))
    tag = f"#{index + 1}" + (f" +{hidden_members}" if hidden_members else "")
    tag_size = _text_box(font, tag)
    minx, miny, maxx, maxy = -disc, -disc - tag_size[1] - 3, disc, disc
    for _, x, y, ang in placed + ext_placed:
        ux, uy = math.cos(ang), math.sin(ang)
        x0, x1 = x - _NODE_RADIUS, x + _NODE_RADIUS
        y0, y1 = y - _NODE_RADIUS, y + _NODE_RADIUS
        if abs(ux) >= 0.5:
            reach = _NODE_RADIUS + 3 + label_w
            x0, x1 = (x0, x + reach) if ux > 0 else (x - reach, x1)
            y0, y1 = min(y0, y - label_h / 2), max(y1, y + label_h / 2)
        else:
            reach = _NODE_RADIUS + 2 + label_h
            y0, y1 = (y0, y + reach) if uy > 0 else (y - reach, y1)
            x0, x1 = min(x0, x - label_w / 2), max(x1, x + label_w / 2)
        minx, miny, maxx, maxy = min(minx, x0), min(miny, y0), max(maxx, x1), max(maxy, y1)
    return _Local(index, placed, ext_placed, disc, tag, tag_size, (minx, miny, maxx, maxy))


def _pack(cells: list[_Local], area: Box) -> list[tuple[float, float]] | None:
    """Полицеве укладання комірок; центри кіл або `None`, якщо не влізло."""
    left, top, right, bottom = area
    area_w, area_h = right - left, bottom - top
    rows: list[list[int]] = []
    cur: list[int] = []
    cur_w = 0.0
    sizes = [(c.extent[2] - c.extent[0], c.extent[3] - c.extent[1]) for c in cells]
    for i, (w, _) in enumerate(sizes):
        if w > area_w:
            return None
        if cur and cur_w + w + _CELL_GAP > area_w:
            rows.append(cur)
            cur, cur_w = [], 0.0
        cur.append(i)
        cur_w += w + _CELL_GAP
    if cur:
        rows.append(cur)
    row_h = [max(sizes[i][1] for i in row) for row in rows]
    sum_h = sum(row_h)
    if sum_h + _CELL_GAP * (len(rows) - 1) > area_h:
        return None
    v_gap = min(30.0, (area_h - sum_h) / (len(rows) + 1))
    y = top + (area_h - (sum_h + v_gap * (len(rows) - 1))) / 2
    centers: list[tuple[float, float]] = [(0.0, 0.0)] * len(cells)
    for row, height in zip(rows, row_h):
        sum_w = sum(sizes[i][0] for i in row)
        h_gap = min(30.0, (area_w - sum_w) / (len(row) + 1))
        x = left + (area_w - (sum_w + h_gap * (len(row) - 1))) / 2
        for i in row:
            w, h = sizes[i]
            ext = cells[i].extent
            centers[i] = (x - ext[0], y + (height - h) / 2 - ext[1])
            x += w + h_gap
        y += height + v_gap
    return centers


def layout_graph(doc: dict[str, Any], config: DeliveryConfig) -> GraphLayout:
    """Розкладка графа документа (чиста, детермінована); див. шапку модуля."""
    if not isinstance(doc, dict) or not isinstance(doc.get("clusters"), list):
        raise TypeError("doc: expected API response dict with 'clusters' list")
    width, height = config.png_width, config.png_height
    palette = [_color(c) for c in config.cluster_palette]
    font = ImageFont.load_default()
    norm = _normalize(doc["clusters"])

    # --- склад: власники членів, джерела, унікальні ребра й групові докази
    owner: dict[str, int] = {}
    members_of: list[list[str]] = []
    for i, (_, wallets, _ev) in enumerate(norm):
        mine: list[str] = []
        for w in wallets:
            if w not in owner:
                owner[w] = i
                mine.append(w)
        members_of.append(mine)
    pairs: dict[tuple[str, str, str], int] = {}
    groups: dict[tuple[str, tuple[str, ...]], int] = {}
    ext_owner: dict[str, int] = {}
    for i, (_, wallets, evidence) in enumerate(norm):
        member_set = set(wallets)
        for kind, source in evidence:
            for a in sorted(set(source)):
                if a not in owner and a not in ext_owner:
                    ext_owner[a] = i
            found, group = _evidence_shape(kind, source, member_set)
            for a, b in found:
                pairs.setdefault((kind, a, b), i)
            if group is not None:
                groups.setdefault((kind, group), i)
    wallets_total = len(owner) + len(ext_owner)
    degree: dict[str, int] = {}
    for _, a, b in pairs:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    for _, group in groups:
        for a in group:
            degree[a] = degree.get(a, 0) + 1
    ext_by_owner: dict[int, list[str]] = {}
    for a, o in ext_owner.items():
        ext_by_owner.setdefault(o, []).append(a)

    # --- які кластери розкладаємо: найбільші за часткою (O(1) за кількістю кластерів)
    candidates = [i for i in range(len(norm)) if members_of[i]]
    ranked = sorted(candidates, key=lambda i: (-_share_value(norm[i][0]), i))[:_MAX_CLUSTERS]
    order = sorted(ranked)
    shown_members: dict[int, list[str]] = {}
    hidden_members: dict[int, int] = {}
    shown_ext: dict[int, list[str]] = {}
    for i in order:
        mine = members_of[i]
        if len(mine) > _MAX_MEMBERS:  # топ за степенем, далі за порядком у документі
            top = sorted(range(len(mine)), key=lambda k: (-degree.get(mine[k], 0), k))[:_MAX_MEMBERS]
            keep = [mine[k] for k in top]
        else:
            keep = list(mine)
        shown_members[i] = sorted(keep)
        hidden_members[i] = len(mine) - len(keep)
        exts = sorted(ext_by_owner.get(i, []), key=lambda a: (-degree.get(a, 0), a))
        shown_ext[i] = sorted(exts[:_MAX_EXTERNALS])

    # --- комірки: найбільший масштаб, за якого все влізає; інакше відкидаємо хвіст
    label_w = _text_box(font, _short("W" * 64, config.address_prefix_len))[0]
    label_h = _text_box(font, "Wg")[1]
    area: Box = (_MARGIN, _LEGEND_HEIGHT + 4, width - _MARGIN, height - _FOOTER_HEIGHT)
    chosen: list[_Local] = []
    centers: list[tuple[float, float]] = []
    for count in range(len(order), 0, -1):
        for scale in _SCALES:
            cells = [_local_layout(i, shown_members[i], shown_ext[i], hidden_members[i], scale,
                                   label_w, label_h, font) for i in order[:count]]
            packed = _pack(cells, area)
            if packed is not None:
                chosen, centers = cells, packed
                break
        if chosen:
            break
    if not chosen and order:  # навіть один кластер не влазить — малюємо стиснутим, що вийшло
        cell = _local_layout(order[0], shown_members[order[0]], shown_ext[order[0]],
                             hidden_members[order[0]], _SCALES[-1], label_w, label_h, font)
        chosen = [cell]
        centers = [(area[0] - cell.extent[0], area[1] - cell.extent[1])]

    # --- абсолютні вузли
    placed: list[tuple[str, int, int, int, bool, float]] = []   # адреса, x, y, кластер, зовнішній, кут
    for cell, (cx, cy) in zip(chosen, centers):
        for addr, lx, ly, ang in cell.members:
            placed.append((addr, round(cx + lx), round(cy + ly), cell.index, False, ang))
        for addr, lx, ly, ang in cell.externals:
            placed.append((addr, round(cx + lx), round(cy + ly), cell.index, True, ang))
    inside = [(a, x, y, ci, ext, ang) for a, x, y, ci, ext, ang in placed
              if _NODE_RADIUS <= x <= width - _NODE_RADIUS
              and _LEGEND_HEIGHT <= y - _NODE_RADIUS and y + _NODE_RADIUS <= height]
    occupied: list[Box] = [(x - _NODE_RADIUS, y - _NODE_RADIUS, x + _NODE_RADIUS + 1,
                            y + _NODE_RADIUS + 1) for _, x, y, *_r in inside]
    pos = {a: (x, y) for a, x, y, *_r in inside}

    # --- ребра між показаними вузлами: пріоритет типу, ліміт на кластер, обхід чужих вершин
    per_cluster: dict[int, int] = {}
    chosen_edges: list[tuple[str, str, str]] = []
    for key in sorted(pairs, key=lambda k: (_priority(k[0]), k[1], k[2])):
        kind, a, b = key
        if a not in pos or b not in pos:
            continue
        ci = pairs[key]
        if per_cluster.get(ci, 0) >= _MAX_EDGES_PER_CLUSTER:
            continue
        per_cluster[ci] = per_cluster.get(ci, 0) + 1
        chosen_edges.append(key)
    by_pair: dict[tuple[str, str], list[str]] = {}
    for kind, a, b in chosen_edges:
        by_pair.setdefault((a, b), []).append(kind)
    bounds: Box = (0, _LEGEND_HEIGHT, width, height - _FOOTER_HEIGHT)
    segments: dict[int, list[tuple[tuple[int, int], tuple[int, int]]]] = {}
    laid_edges: list[LaidEdge] = []
    for kind, a, b in chosen_edges:
        kinds = by_pair[(a, b)]
        shift = (kinds.index(kind) - (len(kinds) - 1) / 2) * _PAIR_OFFSET
        (x1, y1), (x2, y2) = pos[a], pos[b]
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        nx, ny = -(y2 - y1) / length * shift, (x2 - x1) / length * shift
        p, q = (round(x1 + nx), round(y1 + ny)), (round(x2 + nx), round(y2 + ny))
        others = [xy for addr, xy in pos.items() if addr != a and addr != b]
        pts = _route(p, q, others, bounds)
        color, line_w = _style(kind)
        mid = pts[len(pts) // 2] if len(pts) > 2 else ((p[0] + q[0]) // 2, (p[1] + q[1]) // 2)
        laid_edges.append(LaidEdge(kind, a, b, p, q, color, line_w, tuple(pts), mid))
        segments.setdefault(pairs[(kind, a, b)], []).extend(zip(pts, pts[1:]))

    # --- маркери групових доказів: поряд із центром мас показаних адрес, подалі від вузлів і ребер
    markers: list[LaidMarker] = []
    marker_count: dict[int, int] = {}
    node_xy = list(pos.values())
    for key in sorted(groups, key=lambda k: (_priority(k[0]), k[1])):
        kind, group = key
        ci = groups[key]
        shown = [a for a in group if a in pos]
        if len(shown) < 2 or marker_count.get(ci, 0) >= _MAX_MARKERS_PER_CLUSTER:
            continue
        gx = sum(pos[a][0] for a in shown) / len(shown)
        gy = sum(pos[a][1] for a in shown) / len(shown)
        spots = [(gx, gy)] + [(gx + r * math.cos(k * math.pi / 4), gy + r * math.sin(k * math.pi / 4))
                              for r in (16, 32, 48) for k in range(8)]
        near_nodes = [xy for xy in node_xy if math.hypot(xy[0] - gx, xy[1] - gy) <= 100]
        mine_segments = segments.get(ci, [])
        spot = spots[0]
        for cand in spots:
            x, y = round(cand[0]), round(cand[1])
            if not (_MARGIN <= x <= width - _MARGIN and _LEGEND_HEIGHT + 8 <= y <= height - _FOOTER_HEIGHT):
                continue
            if any(math.hypot(nx_ - x, ny_ - y) < _NODE_RADIUS + _MARKER_RADIUS + 3 for nx_, ny_ in near_nodes):
                continue
            if any(math.hypot(m.x - x, m.y - y) < 2 * _MARKER_RADIUS + 4 for m in markers):
                continue
            if any(_seg_dist(x, y, a_, b_) < _MARKER_RADIUS + 4 for a_, b_ in mine_segments):
                continue
            spot = cand
            break
        mx, my = round(spot[0]), round(spot[1])
        markers.append(LaidMarker(kind, ci, mx, my, tuple(shown), tuple(pos[a] for a in shown),
                                  _style(kind)[0]))
        marker_count[ci] = marker_count.get(ci, 0) + 1
        occupied.append((mx - _MARKER_RADIUS - 1, my - _MARKER_RADIUS - 1,
                         mx + _MARKER_RADIUS + 2, my + _MARKER_RADIUS + 2))
        segments.setdefault(ci, []).extend(((mx, my), pos[a]) for a in shown)

    # --- номери кластерів: перший вільний кут над колом
    laid_clusters: list[LaidCluster] = []
    drawn_clusters = {ci for _, _, _, ci, _, _ in inside}
    for cell, (cx, cy) in zip(chosen, centers):
        if cell.index not in drawn_clusters:
            continue
        tw, th, ox, oy = cell.tag_size
        top = round(cy - cell.disc) - th - 3
        spots = [(round(cx - cell.disc), top), (round(cx + cell.disc) - tw, top),
                 (round(cx - tw / 2), top), (round(cx - tw / 2), round(cy - th / 2))]
        box = next(((x, y, x + tw, y + th) for x, y in spots
                    if not any(_overlap((x, y, x + tw, y + th), o) for o in occupied)),
                   (spots[0][0], spots[0][1], spots[0][0] + tw, spots[0][1] + th))
        occupied.append(box)
        laid_clusters.append(LaidCluster(
            index=cell.index, cx=round(cx), cy=round(cy), radius=math.ceil(cell.disc),
            color=_extended_palette(palette, cell.index), tag=cell.tag, tag_box=box,
            tag_xy=(box[0] - ox, box[1] - oy)))

    # --- підписи: назовні від центру кластера, без колізій; ребра — лише м'який штраф
    nodes: list[LaidNode] = []
    for addr, x, y, ci, ext, ang in inside:
        text = _short(addr, config.address_prefix_len)
        w, h, ox, oy = _text_box(font, text)
        ux, uy = math.cos(ang), math.sin(ang)
        right = (x + _NODE_RADIUS + 3, round(y - h / 2))
        left = (x - _NODE_RADIUS - 3 - w, round(y - h / 2))
        below = (round(x - w / 2), y + _NODE_RADIUS + 2)
        above = (round(x - w / 2), y - _NODE_RADIUS - 2 - h)
        if abs(ux) >= 0.5:
            sides = [right if ux > 0 else left, below if uy >= 0 else above,
                     above if uy >= 0 else below, left if ux > 0 else right]
        else:
            sides = [below if uy > 0 else above, right if ux >= 0 else left,
                     left if ux >= 0 else right, above if uy > 0 else below]
        candidates: list[Box] = []
        for extra in (0, 8, 16):
            for side in sides:
                bx, by = side
                if side in (right, left):
                    bx += extra if side == right else -extra
                else:
                    by += extra if side == below else -extra
                candidates.append((bx, by, bx + w, by + h))
        fitting = [c for c in candidates
                   if c[0] >= 0 and c[2] <= width and c[1] >= _LEGEND_HEIGHT and c[3] <= height]
        pool = fitting or candidates[:1]
        mine = segments.get(ci, [])
        best = min(pool, key=lambda c: (sum(1 for o in occupied if _overlap(c, o)),
                                        sum(1 for a_, b_ in mine if _segment_hits(a_, b_, c))))
        if not fitting:  # зсув у полотно
            bx = min(max(best[0], 0), width - w)
            by = min(max(best[1], _LEGEND_HEIGHT), height - h)
            best = (bx, by, bx + w, by + h)
        occupied.append(best)
        nodes.append(LaidNode(
            address=addr, x=x, y=y, cluster=ci,
            color=_FUNDER if ext else _extended_palette(palette, ci),
            label=text, label_box=best, label_xy=(best[0] - ox, best[1] - oy), external=ext))

    items: list[tuple[str, str]] = []
    for kind in sorted({e.type for e in laid_edges} | {m.type for m in markers}, key=_priority):
        if any(e.type == kind for e in laid_edges):
            items.append((kind, "edge"))
        if any(m.type == kind for m in markers):
            items.append((kind, "group"))
    return GraphLayout(
        nodes=tuple(nodes), edges=tuple(laid_edges), markers=tuple(markers),
        clusters=tuple(laid_clusters),
        edge_types=tuple(sorted({k for k, _ in items}, key=_priority)), legend_items=tuple(items),
        hidden_nodes=wallets_total - len(nodes),
        hidden_edges=len(pairs) + len(groups) - len(laid_edges) - len(markers),
        hidden_clusters=len(norm) - len(laid_clusters), wallets_total=wallets_total)


def _segment_hits(p: tuple[int, int], q: tuple[int, int], box: Box) -> bool:
    """Чи перетинає відрізок p-q прямокутник [x0, x1) x [y0, y1) (Ліанг-Барські)."""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for delta, lo, hi, start in ((dx, box[0], box[2], p[0]), (dy, box[1], box[3], p[1])):
        if delta == 0:
            if not (lo <= start < hi):
                return False
            continue
        a, b = (lo - start) / delta, (hi - start) / delta
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
        if t0 > t1:
            return False
    return True


def _draw_diamond(draw: ImageDraw.ImageDraw, x: int, y: int, color: Color) -> None:
    r = _MARKER_RADIUS
    draw.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], fill=color, outline=_INK)


def _draw_legends(draw: ImageDraw.ImageDraw, clusters: Normalized, palette: list[Color],
                  layout: GraphLayout, width: int, font: Any) -> None:
    edge_w = _EDGE_LEGEND_WIDTH if (layout.legend_items
                                    and width - 2 * _MARGIN - _EDGE_LEGEND_WIDTH >= _CLUSTER_LEGEND_COL) else 0
    cols = max(1, (width - 2 * _MARGIN - edge_w) // _CLUSTER_LEGEND_COL)
    for i, (share, wallets, _ev) in enumerate(clusters):
        col, row = divmod(i, _LEGEND_ROWS)
        if col >= cols:
            draw.text((_MARGIN, _LEGEND_HEIGHT - 16), f"...{len(clusters) - i} more clusters in legend",
                      fill=_INK, font=font)
            break
        x0, y0 = _MARGIN + col * _CLUSTER_LEGEND_COL, 34 + row * 22
        draw.rectangle([x0, y0, x0 + 14, y0 + 14], fill=_extended_palette(palette, i), outline=_INK)
        draw.text((x0 + 20, y0 - 2), f"#{i + 1} share {share} n={len(wallets)}", fill=_INK, font=font)
    if edge_w:
        x_base = width - _MARGIN - edge_w
        title, labels = edge_legend(layout.legend_items)
        draw.text((x_base, 12), title, fill=_INK, font=font)
        for k, ((kind, shape), label) in enumerate(zip(layout.legend_items, labels)):
            col, row = divmod(k, _LEGEND_ROWS)
            x0, y0 = x_base + col * _EDGE_LEGEND_COL, 34 + row * 22
            color, line_w = _style(kind)
            if shape == "group":
                draw.line([(x0, y0 + 7), (x0 + 28, y0 + 7)], fill=color, width=1)
                _draw_diamond(draw, x0 + 14, y0 + 7, color)
            else:
                draw.line([(x0, y0 + 7), (x0 + 28, y0 + 7)], fill=color, width=line_w)
            draw.text((x0 + 34, y0 - 2), label, fill=_INK, font=font)


def _tint(color: Color) -> Color:
    return tuple(round(c + (255 - c) * _TINT) for c in color)  # type: ignore[return-value]


def _placeholder(image: Image.Image, lines: list[str], font: Any, wallets_total: int = 0) -> GraphImage:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    total = sum(box[3] - box[1] + 8 for box in boxes)
    y = (height - total) / 2
    for line, box in zip(lines, boxes):
        draw.text(((width - (box[2] - box[0])) / 2, y), line, fill=_INK, font=font)
        y += box[3] - box[1] + 8
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return GraphImage(png=buf.getvalue(), width=width, height=height, empty=True,
                      clusters_shown=0, wallets_shown=0, wallets_total=wallets_total)


def render_png(doc: dict[str, Any], config: DeliveryConfig) -> GraphImage:
    """PNG документа 004.1. Двічі на тому самому вході — ті самі байти."""
    if not isinstance(doc, dict) or not isinstance(doc.get("clusters"), list):
        raise TypeError("doc: expected API response dict with 'clusters' list")
    width, height = config.png_width, config.png_height
    palette = [_color(c) for c in config.cluster_palette]
    image = Image.new("RGB", (width, height), _BACKGROUND)
    font = ImageFont.load_default()

    clusters = doc["clusters"]
    if not clusters:
        return _placeholder(image, empty_caption(doc), font)

    layout = layout_graph(doc, config)
    if not layout.clusters:  # кластери є, але жодного не вдалося намалювати: не «порожньо»
        return _placeholder(image, [f"risk {doc.get('risk_score', 0)}/100 {doc.get('band', '')}",
                                    f"{len(clusters)} clusters, none drawable"], font,
                            layout.wallets_total)
    draw = ImageDraw.Draw(image)
    draw.text((_MARGIN, 12), f"risk {doc.get('risk_score', 0)}/100 {doc.get('band', '')}",
              fill=_INK, font=font)
    _draw_legends(draw, _normalize(clusters), palette, layout, width, font)

    for cl in layout.clusters:
        draw.ellipse([cl.cx - cl.radius, cl.cy - cl.radius, cl.cx + cl.radius, cl.cy + cl.radius],
                     fill=_tint(cl.color), outline=cl.color, width=2)
    for marker in layout.markers:  # тонкі лінії груп під ребрами
        for target in marker.spokes:
            draw.line([(marker.x, marker.y), target], fill=marker.color, width=1)
    for edge in sorted(layout.edges, key=lambda e: (_priority(e.type), e.a, e.b), reverse=True):
        draw.line(list(edge.points), fill=edge.color, width=edge.width)
    for marker in layout.markers:
        _draw_diamond(draw, marker.x, marker.y, marker.color)
    for node in layout.nodes:
        draw.ellipse([node.x - _NODE_RADIUS, node.y - _NODE_RADIUS,
                      node.x + _NODE_RADIUS, node.y + _NODE_RADIUS], fill=node.color, outline=_INK)
    for cl in layout.clusters:  # номери — після ребер, щоб їх не перекреслювало
        draw.rectangle([cl.tag_box[0], cl.tag_box[1], cl.tag_box[2] - 1, cl.tag_box[3] - 1],
                       fill=_BACKGROUND)
        draw.text(cl.tag_xy, cl.tag, fill=_INK, font=font)
    for node in layout.nodes:
        x0, y0, x1, y1 = node.label_box
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=_BACKGROUND)
        draw.text(node.label_xy, node.label, fill=_INK, font=font)
    notes = [f"+{count} {what}" for count, what in (
        (layout.hidden_nodes, "nodes"), (layout.hidden_edges, "edges"),
        (layout.hidden_clusters, "clusters")) if count]
    if notes:
        draw.text((_MARGIN, height - 20), "hidden: " + " ".join(notes), fill=_INK, font=font)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return GraphImage(png=buf.getvalue(), width=width, height=height,
                      empty=False, clusters_shown=len(layout.clusters),
                      wallets_shown=len(layout.nodes), wallets_total=layout.wallets_total)
