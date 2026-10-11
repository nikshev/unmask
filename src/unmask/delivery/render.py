# impl: FR-004-05
"""Серверний статичний PNG-рендер документа відповіді (R-3, Pillow): справжній граф.

Розкладка детермінована й виводиться лише з даних: кластери йдуть у порядку документа,
вузли всередині кластера й джерела сортуються за адресою, жодного випадкового й жодної
залежності від порядку обходу множин. Кожен кластер — власне коло (м'яка підкладка в колі
кольору кластера); вузли-джерела, що не є членами кластера, сірі й стоять поза колом свого
кластера (першого за порядком, у чиїх доказах зустрілись). Ребра будуються з
`evidence[].source` (документ 004.1: `source = sorted(via ∪ wallets)`, напрямок
втрачено — ребра неорієнтовані):

| тип доказу                         | ребра                                                    |
|------------------------------------|----------------------------------------------------------|
| `shared_funder`, `recovered_edge`  | зірка: кожне джерело (не член кластера) -> кожен член     |
| `indirect_link`                    | зірка від усіх проміжних вершин (`C`, `M…`) до членів     |
| `direct_transfer`, `delegated_buy` | одне ребро між двома адресами `source`                    |
| `same_amounts`, `same_slot`        | шлях по відсортованих адресах (група, а не клік)          |

Тип ребра розрізняється кольором, товщиною й штрихом (`EDGE_STYLES`); легенда типів — поруч
із легендою кластерів. Ліміти (`_MAX_*`) не дають великому кластеру стати кашею: решта
вузлів/ребер/кластерів вказується підписом «+K» і лічильниками `GraphImage`.
Текст картинки — лише ASCII (адреси base58, числа, англійські підписи): бітмап-шрифт за
умовчанням. Розмір полотна, палітра й довжина скорочень — з `delivery.yaml`; решта показових
сталих — модульні константи; на висновок не впливають (FR-004-11).
"""

from __future__ import annotations

import colorsys
import io
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from unmask.delivery.config import DeliveryConfig

__all__ = ["EDGE_STYLES", "GraphImage", "GraphLayout", "LaidCluster", "LaidEdge", "LaidNode",
           "empty_caption", "layout_graph", "render_png"]

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
_RING_SINGLE_MAX = 12        # далі — два кільця
_RING_SPACING = 66           # мінімальний крок між вузлами кільця (місце під підпис)
_MIN_RING = 44
_DISC_PAD = 8
_EXTERNAL_GAP = 40
_CELL_GAP = 6
_SCALES = (2.0, 1.7, 1.45, 1.2, 1.0, 0.85, 0.7, 0.55, 0.4)
_PAIR_OFFSET = 3             # зсув ребер різних типів між тією самою парою
_CLUSTER_LEGEND_COL = 260
_EDGE_LEGEND_WIDTH = 300
_EDGE_LEGEND_COL = 150
_LEGEND_ROWS = 4

# тип доказу -> (колір, товщина, штрих (мазок, проміжок) | None); порядок — пріоритет ребер
EDGE_STYLES: dict[str, tuple[Color, int, tuple[int, int] | None]] = {
    "shared_funder": ((150, 0, 0), 3, None),
    "direct_transfer": ((200, 0, 140), 3, None),
    "recovered_edge": ((130, 80, 0), 2, None),
    "delegated_buy": ((0, 110, 110), 2, None),
    "indirect_link": ((120, 0, 200), 2, (7, 4)),
    "same_amounts": ((0, 0, 170), 2, (3, 3)),
    "same_slot": ((100, 100, 0), 2, (10, 4)),
}
_UNKNOWN_STYLE: tuple[Color, int, tuple[int, int] | None] = ((90, 90, 90), 2, (2, 4))
_PAIR_TYPES = frozenset({"direct_transfer", "delegated_buy"})
_HUB_TYPES = frozenset({"shared_funder", "recovered_edge", "indirect_link"})


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
    clusters: tuple[LaidCluster, ...] = ()
    edge_types: tuple[str, ...] = ()
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


def _style(kind: str) -> tuple[Color, int, tuple[int, int] | None]:
    return EDGE_STYLES.get(kind, _UNKNOWN_STYLE)


def _priority(kind: str) -> tuple[int, str]:
    order = list(EDGE_STYLES)
    return (order.index(kind) if kind in order else len(order), kind)


def _chain(addresses: list[str]) -> list[tuple[str, str]]:
    return [(addresses[i], addresses[i + 1]) for i in range(len(addresses) - 1)]


def _evidence_edges(kind: str, source: Any, members: set[str]) -> list[tuple[str, str]]:
    """Пари адрес для одного доказу (таблиця в шапці модуля). `source` уже без напрямку."""
    if not isinstance(source, list):
        return []
    src = sorted({a for a in source if isinstance(a, str)})
    if len(src) < 2:
        return []
    if kind in _PAIR_TYPES and len(src) == 2:
        return [(src[0], src[1])]
    if kind in _HUB_TYPES:
        outside = [a for a in src if a not in members]
        inside = [a for a in src if a in members]
        if outside and inside:
            return [(o, m) for o in outside for m in inside]
    return _chain(src)


def _text_box(font: Any, text: str) -> tuple[int, int, int, int]:
    """(ширина, висота, зсув X, зсув Y) тексту з 1 пікселем підкладки навколо."""
    left, top, right, bottom = font.getbbox(text)
    return (right - left + 2, bottom - top + 2, left - 1, top - 1)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


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
    placed: list[tuple[str, float, float, float]] = []
    for k, addr in enumerate(members):
        if k < n_out:
            ang = -math.pi / 2 + 2 * math.pi * k / n_out
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
    clusters = doc["clusters"]

    # --- склад: власники членів, джерела, унікальні ребра
    owner: dict[str, int] = {}
    members_of: list[list[str]] = []
    for i, cluster in enumerate(clusters):
        mine: list[str] = []
        for w in cluster.get("wallets", []):
            if isinstance(w, str) and w not in owner:
                owner[w] = i
                mine.append(w)
        members_of.append(mine)
    edges: dict[tuple[str, str, str], int] = {}
    ext_owner: dict[str, int] = {}
    for i, cluster in enumerate(clusters):
        member_set = set(cluster.get("wallets", []))
        for ev in cluster.get("evidence", []):
            kind = str(ev.get("type", ""))
            source = ev.get("source", [])
            for a in sorted({x for x in source if isinstance(x, str)}) if isinstance(source, list) else []:
                if a not in owner and a not in ext_owner:
                    ext_owner[a] = i
            for a, b in _evidence_edges(kind, source, member_set):
                edges.setdefault((kind, a, b), i)
    wallets_total = len(owner) + len(ext_owner)
    degree: dict[str, int] = {}
    for _, a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    # --- вибір показаних вузлів (топ за степенем, далі за порядком у документі)
    shown_members: list[list[str]] = []
    hidden_members: list[int] = []
    for mine in members_of:
        if len(mine) > _MAX_MEMBERS:
            ranked = sorted(range(len(mine)), key=lambda k: (-degree.get(mine[k], 0), k))
            keep = [mine[k] for k in ranked[:_MAX_MEMBERS]]
        else:
            keep = list(mine)
        shown_members.append(sorted(keep))
        hidden_members.append(len(mine) - len(keep))
    shown_ext: list[list[str]] = []
    for i in range(len(clusters)):
        mine = sorted(a for a, o in ext_owner.items() if o == i)
        mine = sorted(sorted(mine, key=lambda a: (-degree.get(a, 0), a))[:_MAX_EXTERNALS])
        shown_ext.append(mine)

    # --- комірки: найбільший масштаб, за якого все влізає; інакше відкидаємо хвіст
    label_w = max((_text_box(font, _short("W" * 64, config.address_prefix_len))[0], 1))
    label_h = _text_box(font, "Wg")[1]
    area: Box = (_MARGIN, _LEGEND_HEIGHT + 4, width - _MARGIN, height - _FOOTER_HEIGHT)
    order = [i for i in range(len(clusters)) if shown_members[i]]
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

    # --- абсолютні вузли й підписи
    placed: list[tuple[str, int, int, int, bool, float]] = []   # адреса, x, y, кластер, зовнішній, кут
    laid_clusters: list[LaidCluster] = []
    occupied: list[Box] = []
    for cell, (cx, cy) in zip(chosen, centers):
        for addr, lx, ly, ang in cell.members:
            placed.append((addr, round(cx + lx), round(cy + ly), cell.index, False, ang))
        for addr, lx, ly, ang in cell.externals:
            placed.append((addr, round(cx + lx), round(cy + ly), cell.index, True, ang))
    inside = [(a, x, y, ci, ext, ang) for a, x, y, ci, ext, ang in placed
              if _NODE_RADIUS <= x <= width - _NODE_RADIUS
              and _LEGEND_HEIGHT <= y - _NODE_RADIUS and y + _NODE_RADIUS <= height]
    for _, x, y, *_rest in inside:
        occupied.append((x - _NODE_RADIUS, y - _NODE_RADIUS, x + _NODE_RADIUS + 1, y + _NODE_RADIUS + 1))
    for cell, (cx, cy) in zip(chosen, centers):  # номер кластера: перший вільний кут над колом
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
    # --- ребра між показаними вузлами: пріоритет типу, ліміт на кластер
    pos = {a: (x, y) for a, x, y, *_r in inside}
    per_cluster: dict[int, int] = {}
    chosen_edges: list[tuple[str, str, str]] = []
    for key in sorted(edges, key=lambda k: (_priority(k[0]), k[1], k[2])):
        kind, a, b = key
        if a not in pos or b not in pos:
            continue
        ci = edges[key]
        if per_cluster.get(ci, 0) >= _MAX_EDGES_PER_CLUSTER:
            continue
        per_cluster[ci] = per_cluster.get(ci, 0) + 1
        chosen_edges.append(key)
    by_pair: dict[tuple[str, str], list[str]] = {}
    for kind, a, b in chosen_edges:
        by_pair.setdefault((a, b), []).append(kind)
    laid_edges: list[LaidEdge] = []
    for kind, a, b in chosen_edges:
        kinds = by_pair[(a, b)]
        shift = (kinds.index(kind) - (len(kinds) - 1) / 2) * _PAIR_OFFSET
        (x1, y1), (x2, y2) = pos[a], pos[b]
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        nx, ny = -(y2 - y1) / length * shift, (x2 - x1) / length * shift
        color, line_w, _ = _style(kind)
        laid_edges.append(LaidEdge(kind, a, b, (round(x1 + nx), round(y1 + ny)),
                                   (round(x2 + nx), round(y2 + ny)), color, line_w))
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
            for k, (bx, by) in enumerate(sides):
                if sides[k] in (right, left):
                    bx += extra if sides[k] == right else -extra
                else:
                    by += extra if sides[k] == below else -extra
                candidates.append((bx, by, bx + w, by + h))
        fitting = [c for c in candidates
                   if c[0] >= 0 and c[2] <= width and c[1] >= _LEGEND_HEIGHT and c[3] <= height]
        pool = fitting or candidates[:1]
        best = min(pool, key=lambda c: (sum(1 for o in occupied if _overlap(c, o)),
                                        sum(1 for e in laid_edges if _segment_hits(e.p, e.q, c))))
        if not fitting:  # зсув у полотно
            bx = min(max(best[0], 0), width - w)
            by = min(max(best[1], _LEGEND_HEIGHT), height - h)
            best = (bx, by, bx + w, by + h)
        occupied.append(best)
        nodes.append(LaidNode(
            address=addr, x=x, y=y, cluster=ci,
            color=_FUNDER if ext else _extended_palette(palette, ci),
            label=text, label_box=best, label_xy=(best[0] - ox, best[1] - oy), external=ext))

    edge_types = tuple(sorted({e.type for e in laid_edges}, key=_priority))
    return GraphLayout(
        nodes=tuple(nodes), edges=tuple(laid_edges), clusters=tuple(laid_clusters),
        edge_types=edge_types, hidden_nodes=wallets_total - len(nodes),
        hidden_edges=len(edges) - len(laid_edges),
        hidden_clusters=len(clusters) - len(laid_clusters), wallets_total=wallets_total)


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


def _dash_segments(p: tuple[int, int], q: tuple[int, int], on: int, off: int
                   ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Штрихи від середини ребра в обидва боки: середина завжди на мазку."""
    length = math.hypot(q[0] - p[0], q[1] - p[1])
    if length == 0:
        return []
    ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
    half = length / 2
    spans = [(half - on / 2, half + on / 2)]
    k = 0
    while half + on / 2 + off + k * (on + off) < length:
        start = half + on / 2 + off + k * (on + off)
        spans.append((start, start + on))
        spans.append((length - start - on, length - start))
        k += 1
    out = []
    for s0, s1 in spans:
        s0, s1 = max(0.0, s0), min(length, s1)
        if s1 > s0:
            out.append(((p[0] + ux * s0, p[1] + uy * s0), (p[0] + ux * s1, p[1] + uy * s1)))
    return out


def _draw_line(draw: ImageDraw.ImageDraw, p: tuple[int, int], q: tuple[int, int], color: Color,
               line_w: int, dash: tuple[int, int] | None) -> None:
    if dash is None:
        draw.line([p, q], fill=color, width=line_w)
        return
    for a, b in _dash_segments(p, q, *dash):
        draw.line([a, b], fill=color, width=line_w)


def _draw_legends(draw: ImageDraw.ImageDraw, doc_clusters: list[Any], palette: list[Color],
                  layout: GraphLayout, width: int, font: Any) -> None:
    edge_w = _EDGE_LEGEND_WIDTH if (layout.edge_types
                                    and width - 2 * _MARGIN - _EDGE_LEGEND_WIDTH >= _CLUSTER_LEGEND_COL) else 0
    cols = max(1, (width - 2 * _MARGIN - edge_w) // _CLUSTER_LEGEND_COL)
    for i, cluster in enumerate(doc_clusters):
        col, row = divmod(i, _LEGEND_ROWS)
        if col >= cols:
            draw.text((_MARGIN, _LEGEND_HEIGHT - 16), f"...{len(doc_clusters) - i} more clusters in legend",
                      fill=_INK, font=font)
            break
        x0, y0 = _MARGIN + col * _CLUSTER_LEGEND_COL, 34 + row * 22
        draw.rectangle([x0, y0, x0 + 14, y0 + 14], fill=_extended_palette(palette, i), outline=_INK)
        draw.text((x0 + 20, y0 - 2),
                  f"#{i + 1} share {cluster.get('supply_share', 0)} n={len(cluster.get('wallets', []))}",
                  fill=_INK, font=font)
    if edge_w:
        x_base = width - _MARGIN - edge_w
        draw.text((x_base, 12), "edge types", fill=_INK, font=font)
        for k, kind in enumerate(layout.edge_types):
            col, row = divmod(k, _LEGEND_ROWS)
            x0, y0 = x_base + col * _EDGE_LEGEND_COL, 34 + row * 22
            color, line_w, dash = _style(kind)
            _draw_line(draw, (x0, y0 + 7), (x0 + 28, y0 + 7), color, line_w, dash)
            draw.text((x0 + 34, y0 - 2), kind.replace("_", " "), fill=_INK, font=font)


def _tint(color: Color) -> Color:
    return tuple(round(c + (255 - c) * _TINT) for c in color)  # type: ignore[return-value]


def render_png(doc: dict[str, Any], config: DeliveryConfig) -> GraphImage:
    """PNG документа 004.1. Двічі на тому самому вході — ті самі байти."""
    if not isinstance(doc, dict) or not isinstance(doc.get("clusters"), list):
        raise TypeError("doc: expected API response dict with 'clusters' list")
    width, height = config.png_width, config.png_height
    palette = [_color(c) for c in config.cluster_palette]
    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    clusters = doc["clusters"]
    if not clusters:
        lines = empty_caption(doc)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        total = sum(box[3] - box[1] + 8 for box in boxes)
        y = (height - total) / 2
        for line, box in zip(lines, boxes):
            draw.text(((width - (box[2] - box[0])) / 2, y), line, fill=_INK, font=font)
            y += box[3] - box[1] + 8
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return GraphImage(png=buf.getvalue(), width=width, height=height,
                          empty=True, clusters_shown=0)

    layout = layout_graph(doc, config)
    draw.text((_MARGIN, 12), f"risk {doc.get('risk_score', 0)}/100 {doc.get('band', '')}",
              fill=_INK, font=font)
    _draw_legends(draw, clusters, palette, layout, width, font)

    for cl in layout.clusters:
        draw.ellipse([cl.cx - cl.radius, cl.cy - cl.radius, cl.cx + cl.radius, cl.cy + cl.radius],
                     fill=_tint(cl.color), outline=cl.color, width=2)
    for cl in layout.clusters:
        draw.rectangle([cl.tag_box[0], cl.tag_box[1], cl.tag_box[2] - 1, cl.tag_box[3] - 1],
                       fill=_BACKGROUND)
        draw.text(cl.tag_xy, cl.tag, fill=_INK, font=font)
    for edge in sorted(layout.edges, key=lambda e: (_priority(e.type), e.a, e.b), reverse=True):
        color, line_w, dash = _style(edge.type)
        _draw_line(draw, edge.p, edge.q, color, line_w, dash)
    for node in layout.nodes:
        draw.ellipse([node.x - _NODE_RADIUS, node.y - _NODE_RADIUS,
                      node.x + _NODE_RADIUS, node.y + _NODE_RADIUS], fill=node.color, outline=_INK)
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
                      empty=False, clusters_shown=max(1, len(layout.clusters)),
                      wallets_shown=len(layout.nodes), wallets_total=layout.wallets_total)
