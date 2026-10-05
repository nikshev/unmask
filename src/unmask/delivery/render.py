# impl: FR-004-05
"""Серверний статичний PNG-рендер документа відповіді (R-3, Pillow).

Розкладка детермінована й виводиться лише з даних: вершини сортуються, сітка —
з індексу, колір кластера — з його позиції в документі. Текст картинки — лише ASCII
(адреси base58, числа, англійські підписи): бітмап-шрифт за умовчанням.
Показові сталі (розміри, палітра, довжина скорочень) — з `delivery.yaml`; на висновок
не впливають (FR-004-11).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from unmask.delivery.config import DeliveryConfig

__all__ = ["GraphImage", "render_png"]

_BACKGROUND = (255, 255, 255)
_INK = (0, 0, 0)
_FUNDER = (136, 136, 136)
_LEGEND_HEIGHT = 110
_MARGIN = 24
_NODE_RADIUS = 14
_CELL = 120


@dataclass(frozen=True)
class GraphImage:
    """PNG-байтівки з розмірами й описом показаного."""

    png: bytes
    width: int
    height: int
    empty: bool
    clusters_shown: int

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


def _short(address: str, prefix_len: int) -> str:
    return address[:prefix_len] + "..." if len(address) > prefix_len else address


def _color(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


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
        text = "no clusters found"
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2),
                  text, fill=_INK, font=font)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return GraphImage(png=buf.getvalue(), width=width, height=height,
                          empty=True, clusters_shown=0)

    draw.text((_MARGIN, 12), f"risk {doc.get('risk_score', 0)}/100 {doc.get('band', '')}",
              fill=_INK, font=font)
    for i, cluster in enumerate(clusters):
        color = palette[i % len(palette)]
        x0 = _MARGIN + i * 260
        draw.rectangle([x0, 34, x0 + 14, 48], fill=color, outline=_INK)
        wallets = cluster.get("wallets", [])
        draw.text((x0 + 20, 32),
                  f"#{i + 1} share {cluster.get('supply_share', 0)} n={len(wallets)}",
                  fill=_INK, font=font)

    nodes: list[tuple[str, tuple[int, int, int]]] = []
    for i, cluster in enumerate(clusters):
        color = palette[i % len(palette)]
        for wallet in sorted(cluster.get("wallets", [])):
            nodes.append((wallet, color))
    member_set = {w for w, _ in nodes}
    funders = set()
    for cluster in clusters:
        for ev in cluster.get("evidence", []):
            for addr in list(ev.get("via", [])):
                if addr not in member_set:
                    funders.add(addr)
    for addr in sorted(funders):
        nodes.append((addr, _FUNDER))

    per_row = max(1, (width - 2 * _MARGIN) // _CELL)
    top = _LEGEND_HEIGHT
    for idx, (wallet, color) in enumerate(nodes):
        row, col = divmod(idx, per_row)
        cx = _MARGIN + col * _CELL + _CELL // 2
        cy = top + row * 64 + 24
        if cy + _NODE_RADIUS > height:
            break
        draw.ellipse([cx - _NODE_RADIUS, cy - _NODE_RADIUS, cx + _NODE_RADIUS, cy + _NODE_RADIUS],
                     fill=color, outline=_INK)
        label = _short(wallet, config.address_prefix_len)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text((cx - (box[2] - box[0]) / 2, cy + _NODE_RADIUS + 2), label, fill=_INK, font=font)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return GraphImage(png=buf.getvalue(), width=width, height=height,
                      empty=False, clusters_shown=len(clusters))
