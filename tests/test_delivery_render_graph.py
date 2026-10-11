# verifies: FR-004-05
"""Тести справжнього графа в PNG: розкладка за кластерами, ребра за доказами (T-099).

Перевірки — розбір пікселів і метаданих розкладки (`layout_graph`), без побайтової рівності
з еталоном; детермінованість — «двічі → ті самі байти», включно з іншим PYTHONHASHSEED.
Доки 004.1 не зберігають напрямку (`source` = відсортована `via ∪ wallets`), тому ребра
неорієнтовані.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from test_delivery_render import _cfg, _doc
from unmask.delivery import render as R
from unmask.delivery.render import EDGE_STYLES, layout_graph, render_png

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "tests" / "fixtures" / "api_docs"
WHITE = (255, 255, 255)
GRAY = (136, 136, 136)
LINK_TYPES = ("shared_funder", "direct_transfer", "delegated_buy", "recovered_edge")
ALL_TYPES = LINK_TYPES + ("indirect_link", "same_amounts", "same_slot")


def _a(tag: str, i: int) -> str:
    return f"{tag}{i:02d}" + "q" * 38


def _ev(kind: str, source: list[str]) -> dict:
    return {"type": kind, "source": sorted(set(source)),
            "window": {"basis": "block_time", "start": 1, "end": 2}}


def _cluster(i: int, members: list[str], evidence: list[dict], share: float = 0.3) -> dict:
    return {"cluster_id": f"c-{i:016d}", "wallets": list(members), "supply_share": share,
            "supply_share_denominator": "analyzed_buyers_received_amount", "confidence": 0.6,
            "evidence": evidence}


def _doc_of(clusters: list[dict]) -> dict:
    return {"mint": "1" * 32, "analyzed_at": 1, "wallets_analyzed": 10, "clusters": clusters,
            "risk_score": 40, "band": "suspicious", "band_reasons": ["x"],
            "provenance": {}, "error": None}


def _load(name: str) -> dict:
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def _pairs(layout) -> set[tuple[str, frozenset[str]]]:
    return {(e.type, frozenset((e.a, e.b))) for e in layout.edges}


def _png(doc: dict, cfg=None) -> Image.Image:
    img = render_png(doc, cfg or _cfg())
    opened = Image.open(io.BytesIO(img.png))
    opened.load()
    return opened.convert("RGB")


def _on_edge(png: Image.Image, edge) -> bool:
    """Середина ребра (з допуском в 1 піксель на округлення) пофарбована кольором ребра."""
    mx, my = (edge.p[0] + edge.q[0]) // 2, (edge.p[1] + edge.q[1]) // 2
    return any(png.getpixel((mx + dx, my + dy)) == edge.color
               for dx in (-1, 0, 1) for dy in (-1, 0, 1))


def _all_docs() -> list[tuple[str, dict]]:
    docs = [(f"live_{n}", _load(f"live_{n}.json")) for n in ("GnQUsLcy", "AKF3enKZ", "4vsZT8jo")]
    docs += [(f"real_{lbl}", _doc(lbl)) for lbl in ("ins4", "ins0", "cln4", "cln2")]
    m = [_a("M", i) for i in range(5)]
    docs.append(("synthetic", _doc_of([
        _cluster(0, m, [_ev("shared_funder", m + [_a("F", 0)]), _ev("same_amounts", m[:4])]),
        _cluster(1, [_a("N", 0), _a("N", 1)], [_ev("direct_transfer", [_a("N", 0), _a("N", 1)])]),
    ])))
    return docs


# ---------------------------------------------------------------- ребра за доказами

def test_shared_funder_is_a_star_from_the_funder_to_every_member() -> None:
    members = [_a("M", i) for i in range(4)]
    funder = _a("F", 0)
    doc = _doc_of([_cluster(0, members, [_ev("shared_funder", members + [funder])])])
    layout = layout_graph(doc, _cfg())
    assert _pairs(layout) == {("shared_funder", frozenset((funder, m))) for m in members}
    ext = [n for n in layout.nodes if n.external]
    assert [n.address for n in ext] == [funder] and ext[0].color == GRAY


def test_direct_transfer_and_recovered_edge_connect_the_source_addresses() -> None:
    m = [_a("M", i) for i in range(3)]
    doc = _doc_of([_cluster(0, m, [
        _ev("direct_transfer", [m[0], m[1]]),
        _ev("recovered_edge", [m[1], m[2], _a("P", 0)]),
    ])])
    layout = layout_graph(doc, _cfg())
    assert _pairs(layout) == {
        ("direct_transfer", frozenset((m[0], m[1]))),
        ("recovered_edge", frozenset((_a("P", 0), m[1]))),
        ("recovered_edge", frozenset((_a("P", 0), m[2]))),
    }


def test_group_evidence_is_a_path_not_a_clique() -> None:
    m = [_a("M", i) for i in range(6)]
    doc = _doc_of([_cluster(0, m, [_ev("same_amounts", m), _ev("same_slot", m[:4]),
                                   _ev("direct_transfer", [m[0], m[5]])])])
    layout = layout_graph(doc, _cfg())
    amounts = [e for e in layout.edges if e.type == "same_amounts"]
    slots = [e for e in layout.edges if e.type == "same_slot"]
    assert len(amounts) == 5 and len(slots) == 3  # n-1, а не n(n-1)/2
    for edges, nodes in ((amounts, m), (slots, m[:4])):
        touched = {x for e in edges for x in (e.a, e.b)}
        assert touched == set(nodes)  # шлях зв'язує всіх, ніхто не випав
        degrees = sorted(sum(1 for e in edges if x in (e.a, e.b)) for x in nodes)
        assert degrees[-1] <= 2


def test_indirect_link_goes_through_non_member_hops() -> None:
    m = [_a("M", i) for i in range(3)]
    c, hop = _a("C", 0), _a("H", 0)
    doc = _doc_of([_cluster(0, m, [_ev("indirect_link", m + [c, hop])])])
    layout = layout_graph(doc, _cfg())
    assert _pairs(layout) == {("indirect_link", frozenset((v, x))) for v in (c, hop) for x in m}
    assert not any(e.a in m and e.b in m for e in layout.edges)  # учасники між собою не з'єднані


def test_members_without_pair_evidence_get_no_edges() -> None:
    m = [_a("M", i) for i in range(4)]
    doc = _doc_of([_cluster(0, m, [_ev("shared_funder", [m[0], m[1], _a("F", 0)])])])
    layout = layout_graph(doc, _cfg())
    touched = {x for e in layout.edges for x in (e.a, e.b)}
    assert touched == {m[0], m[1], _a("F", 0)}
    assert len(layout.edges) == 2


@pytest.mark.parametrize("evidence", [
    [],
    [_ev("shared_funder", [_a("M", 0)])],         # один адрес — пари немає
    [_ev("same_amounts", [])],
])
def test_no_pairs_means_no_edges(evidence) -> None:
    m = [_a("M", i) for i in range(3)]
    doc = _doc_of([_cluster(0, m, evidence)])
    layout = layout_graph(doc, _cfg())
    assert layout.edges == () and layout.hidden_edges == 0
    assert len(layout.nodes) == 3  # вузли лишаються
    png = _png(doc)
    # жодного пікселя хроматичного кольору ребер у полотні графа
    colors = {style[0] for style in EDGE_STYLES.values() if len(set(style[0])) > 1}
    area = png.crop((0, R._LEGEND_HEIGHT, png.width, png.height))
    assert not (set(area.get_flattened_data()) & colors)


def test_edges_follow_evidence_on_real_documents() -> None:
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        sources = [(e["type"], set(e["source"])) for c in doc["clusters"] for e in c["evidence"]]
        for edge in layout.edges:
            assert any(t == edge.type and edge.a in s and edge.b in s for t, s in sources), name
        if any(c["evidence"] for c in doc["clusters"]):
            assert layout.edges, name


def test_edge_types_have_distinct_styles() -> None:
    assert set(ALL_TYPES) <= set(EDGE_STYLES)
    assert len(set(EDGE_STYLES.values())) == len(EDGE_STYLES)
    for color, width, dash in EDGE_STYLES.values():
        assert color != WHITE and width >= 1


# ---------------------------------------------------------------- ребра видно

def test_edge_midpoints_are_painted_in_the_edge_color() -> None:
    members = [_a("M", i) for i in range(4)]
    funder = _a("F", 0)
    doc = _doc_of([_cluster(0, members, [_ev("shared_funder", members + [funder]),
                                         _ev("direct_transfer", members[:2])])])
    layout = layout_graph(doc, _cfg())
    png = _png(doc)
    assert len(layout.edges) == 5
    for edge in layout.edges:
        assert _on_edge(png, edge)


@pytest.mark.parametrize("kind", ALL_TYPES)
def test_every_edge_type_is_drawn_with_its_style(kind: str) -> None:
    m = [_a("M", i) for i in range(3)]
    source = m + [_a("F", 0)] if kind in ("shared_funder", "recovered_edge", "indirect_link") else m[:2]
    doc = _doc_of([_cluster(0, m, [_ev(kind, source)])])
    layout = layout_graph(doc, _cfg())
    png = _png(doc)
    assert layout.edges and {e.type for e in layout.edges} == {kind}
    color, width, _ = EDGE_STYLES[kind]
    for edge in layout.edges:
        assert edge.color == color and edge.width == width
        assert _on_edge(png, edge)


def test_overlapping_types_on_one_pair_stay_both_visible() -> None:
    m = [_a("M", i) for i in range(2)]
    doc = _doc_of([_cluster(0, m, [_ev("direct_transfer", m), _ev("same_amounts", m + [_a("Z", 0)]),
                                   _ev("same_slot", m)])])
    layout = layout_graph(doc, _cfg())
    png = _png(doc)
    on_pair = [e for e in layout.edges if {e.a, e.b} == set(m)]
    assert {e.type for e in on_pair} >= {"direct_transfer", "same_amounts", "same_slot"}
    assert len({(e.p, e.q) for e in on_pair}) == len(on_pair)  # зміщені, не накладені
    for edge in on_pair:
        assert _on_edge(png, edge)


def test_nodes_are_drawn_above_edges() -> None:
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        png = _png(doc)
        for node in layout.nodes:
            assert png.getpixel((node.x, node.y)) == node.color, (name, node.address)


def test_edge_legend_lists_each_present_type_next_to_cluster_legend() -> None:
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        png = _png(doc)
        legend = png.crop((0, 0, png.width, R._LEGEND_HEIGHT))
        present = set(legend.get_flattened_data())
        assert set(layout.edge_types) == {e.type for e in layout.edges}, name
        for kind in layout.edge_types:
            assert EDGE_STYLES[kind][0] in present, (name, kind)
        # і кольори кластерів у легенді лишились
        assert (231, 76, 60) in present


# ---------------------------------------------------------------- розкладка

def test_clusters_are_separate_groups_and_sources_stay_outside_near_their_cluster() -> None:
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        by_index = {c.index: c for c in layout.clusters}
        for a, b in itertools.combinations(layout.clusters, 2):
            assert math.dist((a.cx, a.cy), (b.cx, b.cy)) >= a.radius + b.radius, name
        for node in layout.nodes:
            owner = by_index[node.cluster]
            d = math.dist((node.x, node.y), (owner.cx, owner.cy))
            if node.external:
                assert d > owner.radius, (name, node.address)         # поза колом
                nearest = min(layout.clusters, key=lambda c: math.dist((node.x, node.y), (c.cx, c.cy)) - c.radius)
                assert nearest.index == owner.index, (name, node.address)  # поряд зі своїм
                assert node.color == GRAY
            else:
                assert d <= owner.radius, (name, node.address)         # всередині кола
                assert node.color == R._extended_palette([R._color(c) for c in _cfg().cluster_palette],
                                                         node.cluster)


def test_nodes_of_different_clusters_never_overlap() -> None:
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        for a, b in itertools.combinations(layout.nodes, 2):
            assert math.dist((a.x, a.y), (b.x, b.y)) >= 2 * R._NODE_RADIUS + 2, (name, a.address, b.address)


def test_labels_fit_and_do_not_collide() -> None:
    for name, doc in _all_docs():
        cfg = _cfg()
        layout = layout_graph(doc, cfg)
        boxes = [n.label_box for n in layout.nodes]
        for n in layout.nodes:
            x0, y0, x1, y1 = n.label_box
            assert 0 <= x0 < x1 <= cfg.png_width and R._LEGEND_HEIGHT <= y0 < y1 <= cfg.png_height, name
            assert n.label == R._short(n.address, cfg.address_prefix_len)
            for m in layout.nodes:  # підпис не лягає на жоден вузол
                assert not (x0 < m.x + R._NODE_RADIUS and m.x - R._NODE_RADIUS < x1
                            and y0 < m.y + R._NODE_RADIUS and m.y - R._NODE_RADIUS < y1), (name, n.address)
        for a, b in itertools.combinations(boxes, 2):
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], name


def _crosses(edge, box) -> bool:
    for k in range(101):
        t = k / 100
        x = edge.p[0] + (edge.q[0] - edge.p[0]) * t
        y = edge.p[1] + (edge.q[1] - edge.p[1]) * t
        if box[0] <= x < box[2] and box[1] <= y < box[3]:
            return True
    return False


def test_edges_never_cover_label_text() -> None:
    crossings = 0
    for name, doc in _all_docs():
        layout = layout_graph(doc, _cfg())
        png = _png(doc)
        font = ImageFont.load_default()
        for node in layout.nodes:
            box = node.label_box
            crossings += sum(1 for e in layout.edges if _crosses(e, box))
            ref = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), WHITE)
            ImageDraw.Draw(ref).text((node.label_xy[0] - box[0], node.label_xy[1] - box[1]),
                                     node.label, fill=(0, 0, 0), font=font)
            assert list(png.crop(box).get_flattened_data()) == list(ref.get_flattened_data()), \
                (name, node.address)
    assert crossings > 0  # тест не порожній: ребра справді проходять крізь підписи


# ---------------------------------------------------------------- розмір, межі, правдивість

def test_png_has_config_size_and_counts_are_truthful() -> None:
    from dataclasses import replace
    for name, doc in _all_docs():
        for cfg in (_cfg(), replace(_cfg(), png_width=800, png_height=600)):
            img = render_png(doc, cfg)
            opened = Image.open(io.BytesIO(img.png))
            opened.load()
            assert opened.size == (cfg.png_width, cfg.png_height) == (img.width, img.height)
            layout = layout_graph(doc, cfg)
            members = {w for c in doc["clusters"] for w in c["wallets"]}
            sources = {a for c in doc["clusters"] for e in c["evidence"] for a in e["source"]}
            assert img.wallets_total == layout.wallets_total == len(members | sources), name
            assert img.wallets_shown == len(layout.nodes) <= img.wallets_total
            assert layout.hidden_nodes == img.wallets_total - img.wallets_shown
            assert 1 <= img.clusters_shown <= len(doc["clusters"])
            assert img.clusters_shown + layout.hidden_clusters == len(doc["clusters"])
            for n in layout.nodes:
                assert 0 <= n.x < cfg.png_width and R._LEGEND_HEIGHT <= n.y < cfg.png_height


def test_nothing_hidden_means_no_plus_note_and_everything_hidden_is_counted() -> None:
    doc = _load("live_AKF3enKZ.json")
    layout = layout_graph(doc, _cfg())
    assert (layout.hidden_nodes, layout.hidden_edges, layout.hidden_clusters) == (0, 0, 0)
    big = _doc_of([_cluster(0, [_a("M", i) for i in range(60)],
                            [_ev("shared_funder", [_a("M", i) for i in range(60)] + [_a("F", 0)]),
                             _ev("same_amounts", [_a("M", i) for i in range(60)])])])
    layout = layout_graph(big, _cfg())
    assert layout.hidden_nodes == 61 - len(layout.nodes) > 0
    assert len(layout.edges) + layout.hidden_edges == 60 + 59
    assert layout.hidden_edges > 0


def test_large_cluster_renders_fast_and_stays_readable() -> None:
    members = [_a("M", i) for i in range(60)]
    doc = _doc_of([_cluster(0, members, [_ev("shared_funder", members + [_a("F", 0), _a("F", 1)]),
                                         _ev("direct_transfer", members[:2]),
                                         _ev("same_slot", members)])])
    start = time.perf_counter()
    img = render_png(doc, _cfg())
    assert time.perf_counter() - start <= 2.0
    opened = Image.open(io.BytesIO(img.png))
    opened.load()
    assert opened.size == (1200, 800)
    assert img.wallets_total == 62 and img.wallets_shown < 62
    layout = layout_graph(doc, _cfg())
    assert len(layout.nodes) <= R._MAX_MEMBERS + R._MAX_EXTERNALS
    boxes = [n.label_box for n in layout.nodes]
    for a, b in itertools.combinations(boxes, 2):
        assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    per_pair = {(e.type, frozenset((e.a, e.b))) for e in layout.edges}
    assert len(per_pair) == len(layout.edges)  # без дублів


def test_many_clusters_are_packed_or_reported_not_dropped_silently() -> None:
    clusters = []
    for i in range(14):
        m = [_a(chr(65 + i), j) for j in range(4)]
        clusters.append(_cluster(i, m, [_ev("shared_funder", m + [_a(chr(97 + i), 0)])], 0.07))
    doc = _doc_of(clusters)
    layout = layout_graph(doc, _cfg())
    img = render_png(doc, _cfg())
    assert img.clusters_shown + layout.hidden_clusters == 14
    assert {n.cluster for n in layout.nodes if not n.external} == {c.index for c in layout.clusters}
    assert img.clusters_shown == len(layout.clusters) >= 1
    for a, b in itertools.combinations(layout.nodes, 2):
        assert math.dist((a.x, a.y), (b.x, b.y)) >= 2 * R._NODE_RADIUS + 2


# ---------------------------------------------------------------- детермінованість

def test_same_document_gives_same_bytes_and_ignores_input_order() -> None:
    for name, doc in _all_docs():
        assert render_png(doc, _cfg()).png == render_png(doc, _cfg()).png, name
    m = [_a("M", i) for i in range(7)]
    base = _doc_of([_cluster(0, m, [_ev("shared_funder", m + [_a("F", 0)]), _ev("same_amounts", m[:5]),
                                    _ev("direct_transfer", m[:2])])])
    shuffled = json.loads(json.dumps(base))
    rng = random.Random(7)
    rng.shuffle(shuffled["clusters"][0]["wallets"])
    rng.shuffle(shuffled["clusters"][0]["evidence"])
    assert render_png(base, _cfg()).png == render_png(shuffled, _cfg()).png


def test_layout_does_not_depend_on_hash_seed(tmp_path: Path) -> None:
    doc_path = tmp_path / "doc.json"
    doc_path.write_text(json.dumps(_load("live_AKF3enKZ.json")), encoding="utf-8")
    code = (
        "import hashlib, json, sys;"
        "from pathlib import Path;"
        "from unmask.delivery.config import load_delivery_config;"
        "from unmask.delivery.render import render_png;"
        "cfg = load_delivery_config(Path(sys.argv[2]));"
        "print(hashlib.sha256(render_png(json.load(open(sys.argv[1])), cfg).png).hexdigest())"
    )
    digests = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT / "src")}
        out = subprocess.run([sys.executable, "-c", code, str(doc_path), str(ROOT / "config" / "delivery.yaml")],
                             env=env, capture_output=True, text=True, timeout=60, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1 and len(next(iter(digests))) == 64
    assert hashlib.sha256(render_png(_load("live_AKF3enKZ.json"), _cfg()).png).hexdigest() in digests


# ---------------------------------------------------------------- незмінні частини

def test_empty_result_keeps_the_placeholder() -> None:
    doc = _doc_of([])
    img = render_png(doc, _cfg())
    assert img.empty and img.clusters_shown == 0 and (img.wallets_shown, img.wallets_total) == (0, 0)
    layout = layout_graph(doc, _cfg())
    assert layout.nodes == () and layout.edges == ()


def test_real_and_live_documents_render_without_errors() -> None:
    for name, doc in _all_docs():
        img = render_png(doc, _cfg())
        assert not img.empty and img.png[:8] == b"\x89PNG\r\n\x1a\n", name


def test_edge_cap_per_cluster_keeps_higher_priority_types_and_counts_the_rest() -> None:
    members = [_a("M", i) for i in range(24)]
    funders = [_a("F", i) for i in range(4)]
    doc = _doc_of([_cluster(0, members, [_ev("shared_funder", members + funders),
                                         _ev("same_slot", members)])])
    layout = layout_graph(doc, _cfg())
    assert len(layout.nodes) == 28 and layout.hidden_nodes == 0
    assert len(layout.edges) == R._MAX_EDGES_PER_CLUSTER == 60
    assert layout.hidden_edges == 24 * 4 + 23 - 60
    assert {e.type for e in layout.edges} == {"shared_funder"}  # слабший тип відсікається першим
    assert layout.edge_types == ("shared_funder",)


@pytest.mark.parametrize("n", [2, 3, 5, 8, 12, 13, 18, 24, 30])
@pytest.mark.parametrize("externals", [0, 2, 5])
def test_cluster_shapes_never_collide(n: int, externals: int) -> None:
    members = [_a("M", i) for i in range(n)]
    funders = [_a("F", i) for i in range(externals)]
    evidence = [_ev("shared_funder", members + funders)] if funders else [_ev("same_amounts", members)]
    layout = layout_graph(_doc_of([_cluster(0, members, evidence)]), _cfg())
    assert len(layout.nodes) == min(n, R._MAX_MEMBERS) + externals
    for a, b in itertools.combinations(layout.nodes, 2):
        assert math.dist((a.x, a.y), (b.x, b.y)) >= 2 * R._NODE_RADIUS + 2
        la, lb = a.label_box, b.label_box
        assert la[2] <= lb[0] or lb[2] <= la[0] or la[3] <= lb[1] or lb[3] <= la[1]
    for a in layout.nodes:
        for b in layout.nodes:
            x0, y0, x1, y1 = a.label_box
            assert not (x0 < b.x + R._NODE_RADIUS and b.x - R._NODE_RADIUS < x1
                        and y0 < b.y + R._NODE_RADIUS and b.y - R._NODE_RADIUS < y1)
