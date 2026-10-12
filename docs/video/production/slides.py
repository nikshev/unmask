# -*- coding: utf-8 -*-
"""Render every `kind == "slide"` segment to slides/<id>.png at 1920x1080.

Legibility thresholds are the ones the channel learned the hard way
([[video-mobile-legibility]]): title >= 46, body >= 26, labels inside a figure >= 28,
mono >= 28. `audit()` measures each centred line against its own box, because the
defect that actually ships is text touching the border of its panel, not the frame.

`check_glyphs()` is a hard guard: SFNS/Arial carry no dingbats or emoji, and a missing
codepoint renders as an empty box with nothing in the pipeline complaining.

Every number and every address on these slides is copied from the live API response in
`api_GnQUsLcy.json` or from the graph PNGs the bot itself sent. Nothing is mocked up.
"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG = (13, 15, 20); PANEL = (23, 27, 34); PANEL2 = (30, 34, 42)
FG = (234, 236, 240); MUT = (156, 163, 176); DIM = (112, 119, 132)
RED = (232, 86, 86); ORG = (240, 150, 80); YEL = (238, 203, 104)
GRN = (96, 206, 140); BLU = (118, 190, 232); VIO = (170, 140, 230)
BORD = (62, 70, 84)

HERE = Path(__file__).parent
STILLS = HERE.parent / "stills"
OUT = HERE / "slides"; OUT.mkdir(exist_ok=True)
API = json.loads((HERE / "api_GnQUsLcy.json").read_text())

MIN_BODY, MIN_MONO = 26, 28


# ---------- fonts ------------------------------------------------------------
def _f(paths, sz):
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()


def font(sz, b=False):
    return _f(["/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if b else
              ["/System/Library/Fonts/SFNS.ttf",
               "/System/Library/Fonts/Supplemental/Arial.ttf"], sz)


def body(sz, b=False):
    assert sz >= MIN_BODY, f"body {sz} unreadable on a phone"
    return font(sz, b)


def mono(sz, b=False):
    assert sz >= MIN_MONO, f"mono {sz} unreadable on a phone"
    return _f(["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf"], sz)


# ---------- glyph guard ------------------------------------------------------
_ND = {}


def _notdef(f):
    k = (getattr(f, "path", id(f)), f.size)
    if k not in _ND:
        m = f.getmask("", mode="L"); _ND[k] = (m.size, bytes(m))
    return _ND[k]


def check_glyphs(s, f):
    nd = _notdef(f)
    for ch in set(s):
        if ch in " \n\t":
            continue
        m = f.getmask(ch, mode="L")
        if (m.size, bytes(m)) == nd:
            raise AssertionError(f"font has no glyph for {ch!r} (U+{ord(ch):04X}) in {s[:60]!r}")


# ---------- primitives -------------------------------------------------------
PAD_X, PAD_Y = 40, 16
_BOXES, _TEXTS = [], []


def new():
    _BOXES.clear(); _TEXTS.clear()
    i = Image.new("RGB", (W, H), BG)
    return i, ImageDraw.Draw(i)


def tx(d, xy, s, f, fill=FG):
    check_glyphs(s, f); d.text(xy, s, font=f, fill=fill)


def ct(d, cx, y, s, f, fill=FG):
    check_glyphs(s, f)
    w = d.textlength(s, font=f); x = cx - w / 2
    _TEXTS.append((x, y, x + w, y + f.size, s))
    d.text((x, y), s, font=f, fill=fill)


def lt(d, x, y, s, f, fill=FG):
    check_glyphs(s, f)
    w = d.textlength(s, font=f)
    _TEXTS.append((x, y, x + w, y + f.size, s))
    d.text((x, y), s, font=f, fill=fill)


def bx(d, x0, y0, x1, y1, fill=PANEL, bord=BORD, w=3, r=20):
    _BOXES.append((x0, y0, x1, y1))
    d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=bord, width=w)


def arrow(d, x0, y0, x1, y1, col=MUT, w=5, head=20):
    import math
    d.line([x0, y0, x1, y1], fill=col, width=w)
    a = math.atan2(y1 - y0, x1 - x0)
    for s in (0.45, -0.45):
        d.line([x1, y1, x1 - head * math.cos(a - s), y1 - head * math.sin(a - s)],
               fill=col, width=w)


def title(d, s, sub=None):
    ct(d, W // 2, 46, s, font(52, True))
    if sub:
        ct(d, W // 2, 120, sub, body(30), MUT)


def panel_img(img, src_png, crop, box, bg=(255, 255, 255)):
    """Paste a crop of a real PNG into `box`, scaled to fit, on a light card.

    `crop` is in the source PNG's own pixels; `box` is (x0, y0, x1, y1) on the slide.
    Scaling is LANCZOS and never downward past 1:1 without reason — these graphs carry
    small labels, and the whole point of cropping is to make them bigger.
    """
    s = Image.open(src_png).convert("RGB").crop(crop)
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    k = min(bw / s.width, bh / s.height)
    s = s.resize((max(1, round(s.width * k)), max(1, round(s.height * k))),
                 Image.Resampling.LANCZOS)
    card = Image.new("RGB", (bw, bh), bg)
    card.paste(s, ((bw - s.width) // 2, (bh - s.height) // 2))
    img.paste(card, (x0, y0))
    return (x0 + (bw - s.width) // 2, y0 + (bh - s.height) // 2, s.width, s.height)


def audit(name):
    bad = []
    for tx0, ty0, tx1, ty1, s in _TEXTS:
        cx, cy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
        inner = [b for b in _BOXES if b[0] < cx < b[2] and b[1] < cy < b[3]]
        if not inner:
            # A line that has fallen clean OUT of its panel has no box containing its
            # centre, so the padding check below never sees it — that is exactly how an
            # overflowing JSON listing shipped once. Catch it by horizontal overlap.
            # "Crosses a border", not "is near one": a subtitle sitting above a panel
            # is fine, a line sawn in half by the panel edge is not.
            escaped = [b for b in _BOXES if b[0] < cx < b[2]
                       and (ty0 < b[1] < ty1 or ty0 < b[3] < ty1)]
            if escaped:
                b = min(escaped, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                bad.append(f"    {name}: ESCAPED its panel (y {ty0:.0f}-{ty1:.0f} vs "
                           f"{b[1]:.0f}-{b[3]:.0f})  {s[:40]!r}")
            elif tx0 < 24 or tx1 > W - 24 or ty0 < 12 or ty1 > H - 12:
                bad.append(f"    {name}: off-frame {s[:46]!r}")
            continue
        x0, y0, x1, y1 = min(inner, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        gl, gr, gb = tx0 - x0, x1 - tx1, y1 - ty1
        if gl < PAD_X or gr < PAD_X or gb < PAD_Y:
            bad.append(f"    {name}: L{gl:4.0f} R{gr:4.0f} B{gb:4.0f}  {s[:44]!r}")
    return bad


ALL_BAD = []


def save(img, name):
    img.save(OUT / f"{name}.png")
    b = audit(name)
    ALL_BAD.extend(b)
    print(f"   {name}{'  !! tight' if b else ''}")
    for line in b:
        print(line)


# ============================ slides =========================================

def s01_hook():
    img, d = new()
    # Wordmark, with the "mask" half dimmed — the name is the pitch.
    f = font(150, True)
    a, b2 = "UN", "MASK"
    wa, wb = d.textlength(a, font=f), d.textlength(b2, font=f)
    x = (W - wa - wb) / 2
    tx(d, (x, 300), a, f, FG); tx(d, (x + wa, 300), b2, f, RED)
    d.rounded_rectangle([x, 480, x + wa + wb, 492], radius=6, fill=RED)
    ct(d, W // 2, 560, "Who is really behind the early buyers", body(46), FG)
    ct(d, W // 2, 626, "of a Solana token launch", body(46), FG)
    ct(d, W // 2, 760, "Wallet clusters with on-chain evidence", body(32), MUT)
    ct(d, W // 2, 812, "Telegram bot  ·  public HTTP API", body(32), MUT)
    ct(d, W // 2, 960, "Crypto World's Fair  ·  Superteam Earn", body(28), DIM)
    save(img, "s01_hook")


def s02_problem():
    img, d = new()
    title(d, "What it looks like, and what it is")
    # left: one funder
    bx(d, 80, 300, 430, 620, fill=(38, 24, 24), bord=RED, w=4)
    ct(d, 255, 340, "ONE WALLET", font(38, True), RED)
    ct(d, 255, 410, "funds dozens of", body(30), FG)
    ct(d, 255, 456, "fresh addresses", body(30), FG)
    ct(d, 255, 528, "minutes before", body(28), MUT)
    ct(d, 255, 570, "the launch", body(28), MUT)
    # middle: the fresh wallets, as a column the funder fans out to — an arc read as a
    # comet and lost the "dozens of separate addresses" point entirely.
    cols, rows_n = 3, 4
    gx0, gy0, dx, dy = 560, 310, 74, 74
    for r_ in range(rows_n):
        for c_ in range(cols):
            px, py = gx0 + c_ * dx, gy0 + r_ * dy
            if c_ == 0:
                arrow(d, 440, 460, px - 26, py, col=(146, 58, 58), w=3, head=11)
            d.ellipse([px - 21, py - 21, px + 21, py + 21], fill=RED,
                      outline=(255, 185, 185), width=2)
    ct(d, 634, 232, "dozens of brand-new wallets", body(30, True), FG)
    ct(d, 634, 638, "they all buy in the first seconds", body(30), FG)
    # right: retail
    arrow(d, 960, 460, 1150, 460, col=MUT, w=5)
    bx(d, 1180, 300, 1840, 620, fill=(20, 32, 42), bord=BLU, w=4)
    ct(d, 1510, 340, "THEN THEY SELL", font(38, True), BLU)
    ct(d, 1510, 410, "into the retail buyers", body(30), FG)
    ct(d, 1510, 456, "who arrive later", body(30), FG)
    ct(d, 1510, 528, "on the chart it reads", body(28), MUT)
    ct(d, 1510, 570, "as organic demand", body(28), MUT)
    bx(d, 300, 760, 1620, 960, fill=PANEL2, bord=YEL, w=4)
    ct(d, 960, 800, "Hundreds of early buyers can be one actor.", body(40, True), YEL)
    ct(d, 960, 876, "The addresses are all different. The money is not.", body(32), FG)
    save(img, "s02_problem")


def s03_gap():
    img, d = new()
    title(d, "The gap Unmask fills")
    bx(d, 70, 210, 930, 880, fill=PANEL, bord=DIM, w=4)
    ct(d, 500, 252, "WHAT A TRACKER SHOWS", font(36, True), MUT)
    fm = mono(30)
    # The six real wallets of cluster #1, verbatim from the API response. No invented
    # trade sizes: a number nobody can check has no business on this slide.
    for i, w in enumerate(API["clusters"][0]["wallets"]):
        lt(d, 130, 330 + i * 56, f"{w[:8]}…   early buyer", fm,
           FG if i % 2 == 0 else MUT)
    ct(d, 500, 710, "A list of addresses.", body(34, True), FG)
    ct(d, 500, 772, "Six strangers who happened to buy.", body(30), MUT)
    arrow(d, 952, 545, 1020, 545, col=GRN, w=6, head=22)
    bx(d, 1040, 210, 1850, 880, fill=(18, 32, 26), bord=GRN, w=4)
    ct(d, 1445, 252, "WHAT UNMASK SHOWS", font(36, True), GRN)
    ct(d, 1445, 332, "the same six addresses,", body(34), FG)
    ct(d, 1445, 384, "returned as one group", body(34), FG)
    bx(d, 1110, 452, 1780, 648, fill=(26, 40, 32), bord=(60, 120, 86), w=3, r=16)
    ct(d, 1445, 482, "cluster #1   ·   confidence 0.776", body(30, True), GRN)
    ct(d, 1445, 534, "6 wallets  ·  92% of what the", body(29), FG)
    ct(d, 1445, 578, "analysed early buyers received", body(29), FG)
    ct(d, 1445, 710, "Not a list. A group.", body(34, True), FG)
    ct(d, 1445, 772, "With the evidence that groups it.", body(30), MUT)
    save(img, "s03_gap")


def s07_graph():
    img, d = new()
    title(d, "The graph the bot sends back",
          "risk 72/100 · high concentration · 2 clusters · 30 buyers analysed")
    # Real PNG, cropped to the part that carries the drawing: the source has a lot of
    # white margin, and keeping it would shrink the labels below the legible floor.
    panel_img(img, STILLS / "insider_GnQUsLcy.png", (55, 225, 1110, 690),
              (60, 190, 1860, 790))
    bx(d, 60, 820, 630, 1030, fill=PANEL, bord=RED, w=4, r=16)
    ct(d, 345, 852, "the circles", body(32, True), RED)
    ct(d, 345, 912, "each one is a cluster", body(29), FG)
    ct(d, 345, 956, "of related wallets", body(29), FG)
    bx(d, 675, 820, 1245, 1030, fill=PANEL, bord=MUT, w=4, r=16)
    ct(d, 960, 852, "the grey dots", body(32, True), MUT)
    ct(d, 960, 912, "wallets outside the cluster", body(29), FG)
    ct(d, 960, 956, "that connect to it", body(29), FG)
    bx(d, 1290, 820, 1860, 1030, fill=PANEL, bord=(150, 40, 40), w=4, r=16)
    ct(d, 1575, 852, "the thick red lines", body(32, True), (210, 80, 80))
    ct(d, 1575, 912, "one funder paying", body(29), FG)
    ct(d, 1575, 956, "many of the buyers", body(29), FG)
    save(img, "s07_graph")


def s08_diamonds():
    img, d = new()
    title(d, "A diamond is one piece of evidence",
          "not a transfer between the addresses it sits on")
    # Cluster #2 of the same token, blown up: this is where the diamonds are dense.
    panel_img(img, STILLS / "insider_GnQUsLcy.png", (690, 245, 1075, 540),
              (70, 190, 1140, 1010))
    bx(d, 1190, 240, 1860, 520, fill=PANEL, bord=VIO, w=4, r=18)
    d.polygon([(1255, 300), (1283, 328), (1255, 356), (1227, 328)], fill=VIO)
    lt(d, 1310, 300, "indirect link", body(36, True), VIO)
    ct(d, 1525, 380, "the same funder reaches them", body(28), FG)
    ct(d, 1525, 422, "through one hop in between", body(28), FG)
    bx(d, 1190, 560, 1860, 840, fill=PANEL, bord=(70, 90, 200), w=4, r=18)
    d.polygon([(1255, 620), (1283, 648), (1255, 676), (1227, 648)], fill=(90, 110, 220))
    lt(d, 1310, 620, "same amounts", body(36, True), (120, 140, 235))
    ct(d, 1525, 700, "identical transfer sizes", body(28), FG)
    ct(d, 1525, 742, "inside one slot window", body(28), FG)
    ct(d, 960, 1040, "One diamond is drawn once per group of addresses it binds.",
       body(28), MUT)
    save(img, "s08_diamonds")


def s10_fan():
    img, d = new()
    title(d, "One wallet, three clusters",
          "risk 74/100 · shares 0.56 · 0.33 · 0.10")
    panel_img(img, STILLS / "insider_AKF3enKZ.png", (85, 225, 1105, 700),
              (50, 190, 1870, 870))
    bx(d, 420, 900, 1500, 1040, fill=PANEL, bord=RED, w=4, r=18)
    ct(d, 960, 936, "every red line starts at the same grey wallet", body(34, True), (225, 95, 95))
    ct(d, 960, 992, "it funded buyers in all three groups", body(29), FG)
    save(img, "s10_fan")


def s15_api():
    img, d = new()
    title(d, "The same answer, as JSON",
          "GET /api/token/{mint} — public, no key, same numbers as the bot")
    bx(d, 70, 185, 1850, 1020, fill=(10, 12, 16), bord=BORD, w=3, r=18)
    fm, fb = mono(28), mono(28, True)
    y = 220
    lt(d, 118, y, "$ curl -s localhost:18080/api/token/GnQUsLcy…pump | python3 -m json.tool",
       fb, GRN); y += 52
    # Verbatim from the live response (api_GnQUsLcy.json), only elided with "…".
    lines = [
        ('{', FG), ('  "mint": "GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump",', FG),
        ('  "risk_score": 72,', YEL), ('  "band": "high_concentration",', YEL),
        ('  "coordination_category": "strong",', FG),
        ('  "wallets_analyzed": 30,', FG),
        ('  "clusters": [', FG),
        ('    {  "cluster_id": "c-6d4e46a267363bce",', FG),
        ('       "confidence": 0.776,', BLU),
        ('       "supply_share": 0.9206,', BLU),
        ('       "supply_share_denominator": "analyzed_buyers_received_amount",', MUT),
        ('       "wallets": [ "7R8Ap9om…", "Fd88R9dF…", "CP4vZWiv…", +3 ],', FG),
        ('       "evidence": [', FG),
        ('         { "type": "shared_funder",', ORG),
        ('           "window": { "basis": "block_time",', MUT),
        ('                       "start": 1739732065, "end": 1739732108 },', MUT),
        ('           "source": [ "6RvyevJn…", "CP4vZWiv…", +4 ] },', MUT),
        ('         { "type": "same_amounts",  "window": { "basis": "slot", … } },', ORG),
        ('         { "type": "indirect_link", "window": { "basis": "block_time", … } }', ORG),
    ]
    for s, col in lines:
        lt(d, 118, y, s, fm, col); y += 38
    save(img, "s15_api")


def s16_complete():
    img, d = new()
    title(d, "Incomplete is never shown as clean")
    bx(d, 150, 230, 1770, 520, fill=(10, 12, 16), bord=GRN, w=4, r=18)
    fm = mono(32)
    lt(d, 215, 282, '"provenance": {', fm, MUT)
    lt(d, 215, 334, '    "graph_status": "complete",', fm, GRN)
    lt(d, 215, 386, '    "completeness_reasons": [ "complete_data" ],', fm, GRN)
    lt(d, 215, 438, '    "ingest_config_version": 5, "hub_config_version": 3, …', fm, MUT)
    ct(d, 960, 580, "Every answer carries how it was produced, and whether the data was whole.",
       body(32), FG)
    bx(d, 150, 650, 940, 960, fill=(20, 32, 26), bord=GRN, w=4, r=18)
    ct(d, 545, 694, "complete", font(42, True), GRN)
    ct(d, 545, 778, "the history came back whole,", body(29), FG)
    ct(d, 545, 822, "and risk zero really means", body(29), FG)
    ct(d, 545, 866, "no related groups were found", body(29), FG)
    bx(d, 980, 650, 1770, 960, fill=(40, 30, 18), bord=ORG, w=4, r=18)
    ct(d, 1375, 694, "incomplete", font(42, True), ORG)
    ct(d, 1375, 778, "the answer says so, in the bot", body(29), FG)
    ct(d, 1375, 822, "and in the API. A gap is never", body(29), FG)
    ct(d, 1375, 866, "rounded down to “nothing found”", body(29), FG)
    save(img, "s16_complete")


def s17_method1():
    img, d = new()
    title(d, "Step one — pull the funding history")
    bx(d, 90, 230, 600, 470, fill=PANEL, bord=BLU, w=4)
    ct(d, 345, 268, "FIRST BUYERS", font(36, True), BLU)
    ct(d, 345, 338, "the first 30 wallets", body(30), FG)
    ct(d, 345, 384, "to buy the token", body(30), FG)
    arrow(d, 620, 350, 740, 350, col=MUT, w=6, head=20)
    bx(d, 760, 230, 1830, 470, fill=PANEL, bord=GRN, w=4)
    ct(d, 1295, 268, "RPC POOL", font(36, True), GRN)
    ct(d, 1295, 338, "where did each of those wallets", body(30), FG)
    ct(d, 1295, 384, "get its money from?", body(30), FG)
    bx(d, 150, 560, 930, 900, fill=(18, 32, 26), bord=GRN, w=4, r=18)
    ct(d, 540, 600, "RPC Fast", font(42, True), GRN)
    ct(d, 540, 684, "serves the recent history", body(30), FG)
    ct(d, 540, 730, "and a share of the", body(30), FG)
    ct(d, 540, 776, "transaction fetches", body(30), FG)
    ct(d, 540, 840, "about 21.5 h of chain", body(28), MUT)
    bx(d, 990, 560, 1770, 900, fill=(20, 28, 40), bord=BLU, w=4, r=18)
    ct(d, 1380, 600, "Archival providers", font(42, True), BLU)
    ct(d, 1380, 684, "back it up for anything", body(30), FG)
    ct(d, 1380, 730, "older than that", body(30), FG)
    ct(d, 1380, 840, "batches split in parallel, fail over", body(28), MUT)
    ct(d, 960, 980, "A truncated history is never reported as a complete one.",
       body(30, True), YEL)
    save(img, "s17_method1")


def s18_method2():
    img, d = new()
    title(d, "Step two — prune the hubs",
          "an exchange pays everybody, so it links everybody to everybody")
    import math
    # left: hub in — one blob
    bx(d, 90, 230, 930, 890, fill=PANEL, bord=RED, w=4)
    ct(d, 510, 266, "WITH THE HUBS IN", font(34, True), RED)
    cx, cy = 510, 580
    pts = [(cx + 215 * math.cos(a), cy + 215 * math.sin(a))
           for a in [i * math.pi / 6 for i in range(12)]]
    for i, (x1, y1) in enumerate(pts):
        for x2, y2 in pts[i + 1:]:
            d.line([x1, y1, x2, y2], fill=(84, 60, 60), width=2)
    for x1, y1 in pts:
        d.ellipse([x1 - 15, y1 - 15, x1 + 15, y1 + 15], fill=(190, 80, 80),
                  outline=(240, 170, 170), width=2)
    d.ellipse([cx - 32, cy - 32, cx + 32, cy + 32], fill=DIM, outline=FG, width=3)
    ct(d, 510, 822, "everything glues into one blob", body(30), FG)
    arrow(d, 952, 560, 1020, 560, col=GRN, w=6, head=22)
    # right: pruned — two honest clusters
    bx(d, 1040, 230, 1850, 890, fill=(18, 32, 26), bord=GRN, w=4)
    ct(d, 1445, 266, "HUBS REMOVED", font(34, True), GRN)
    for gx, gy, col in ((1260, 540, RED), (1640, 600, BLU)):
        d.ellipse([gx - 120, gy - 120, gx + 120, gy + 120],
                  outline=col, width=3, fill=(26, 32, 40))
        gp = [(gx + 78 * math.cos(a), gy + 78 * math.sin(a))
              for a in [i * 2 * math.pi / 5 for i in range(5)]]
        for i, (x1, y1) in enumerate(gp):
            for x2, y2 in gp[i + 1:]:
                d.line([x1, y1, x2, y2], fill=(70, 86, 104), width=2)
        for x1, y1 in gp:
            d.ellipse([x1 - 14, y1 - 14, x1 + 14, y1 + 14], fill=col,
                      outline=(235, 235, 235), width=2)
    ct(d, 1445, 822, "groups that are actually related", body(30), FG)
    ct(d, 960, 960, "exchanges  ·  routers  ·  program accounts  —  dropped before clustering",
       body(30), MUT)
    save(img, "s18_method2")


def s19_method3():
    img, d = new()
    title(d, "Step three — merge, then score")
    steps = [("MERGE", GRN, ["wallets that share a funder", "inside a narrow time window",
                             "become one cluster"]),
             ("EXTEND", VIO, ["indirect links add wallets", "the funder reached through",
                              "one hop in between"]),
             ("CONFIRM", ORG, ["identical amounts, the same slot", "— behaviour that raises",
                               "the confidence of the cluster"])]
    for i, (name, col, lines) in enumerate(steps):
        x0 = 70 + i * 605
        bx(d, x0, 215, x0 + 565, 700, fill=PANEL, bord=col, w=4)
        ct(d, x0 + 282, 252, name, font(40, True), col)
        for j, s in enumerate(lines):
            ct(d, x0 + 282, 360 + j * 52, s, body(28), FG)
        ct(d, x0 + 282, 620, f"{i + 1}", font(52, True), col)
        if i < 2:
            arrow(d, x0 + 575, 455, x0 + 595, 455, col=MUT, w=5, head=16)
    bx(d, 220, 770, 1700, 1020, fill=PANEL2, bord=YEL, w=4)
    ct(d, 960, 806, "risk  =  supply share  x  link strength  x  behavioural similarity",
       body(36, True), YEL)
    ct(d, 960, 890, "with the primary references attached to every cluster:", body(30), FG)
    ct(d, 960, 940, "the addresses, the slots, the time window", body(30), MUT)
    save(img, "s19_method3")


def s20_limits():
    img, d = new()
    title(d, "What this is, and what it is not")
    bx(d, 110, 230, 930, 760, fill=(18, 32, 26), bord=GRN, w=4)
    ct(d, 520, 272, "IT IS", font(40, True), GRN)
    for j, s in enumerate(["a signal about financial links",
                           "between wallets, with the",
                           "on-chain evidence attached,",
                           "and a documented method",
                           "you can re-run yourself"]):
        ct(d, 520, 370 + j * 56, s, body(30), FG)
    ct(d, 520, 690, "every threshold is versioned YAML", body(27), MUT)
    bx(d, 990, 230, 1810, 760, fill=(40, 26, 26), bord=RED, w=4)
    ct(d, 1400, 272, "IT IS NOT", font(40, True), RED)
    for j, s in enumerate(["proof of insider trading,",
                           "an accusation against anyone,",
                           "or financial advice.",
                           "A shared funder is a reason",
                           "to look, not a verdict."]):
        ct(d, 1400, 370 + j * 56, s, body(30), FG)
    ct(d, 1400, 690, "the limits are written down in the README", body(27), MUT)
    bx(d, 300, 820, 1620, 1010, fill=PANEL2, bord=BORD, w=3)
    ct(d, 960, 856, "Profile shipped: first 30 buyers, funding depth 2.", body(30), FG)
    ct(d, 960, 908, "The full 200-500 buyer profile needs paid RPC throughput", body(29), MUT)
    ct(d, 960, 952, "— and that is said in the README too, not hidden.", body(29), MUT)
    save(img, "s20_limits")


def s21_next():
    img, d = new()
    title(d, "What is next")
    items = [(GRN, "Deeper buyer samples", "200-500 early buyers instead of 30,", "on faster RPC throughput"),
             (BLU, "Monitoring of new launches", "check a token while it is still", "inside the RPC Fast window"),
             (VIO, "Funder reuse across tokens", "the same wallet financing", "launch after launch")]
    for i, (col, head, l1, l2) in enumerate(items):
        y0 = 230 + i * 268
        bx(d, 180, y0, 1740, y0 + 222, fill=PANEL, bord=col, w=4)
        d.ellipse([240, y0 + 86, 290, y0 + 136], fill=col)
        lt(d, 340, y0 + 42, head, font(42, True), col)
        lt(d, 340, y0 + 112, l1, body(30), FG)
        lt(d, 340, y0 + 160, l2, body(30), MUT)
    save(img, "s21_next")


def s22_outro():
    img, d = new()
    f = font(126, True)
    a, b2 = "UN", "MASK"
    wa, wb = d.textlength(a, font=f), d.textlength(b2, font=f)
    x = (W - wa - wb) / 2
    tx(d, (x, 170), a, f, FG); tx(d, (x + wa, 170), b2, f, RED)
    d.rounded_rectangle([x, 322, x + wa + wb, 332], radius=5, fill=RED)
    ct(d, W // 2, 392, "See who is behind the buyers.", body(46), FG)
    rows = [("repository", "github.com/nikshev/unmask"),
            ("telegram bot", "@unmask123_bot"),
            ("api", "GET /api/token/{mint}"),
            ("try it with", "GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump")]
    fm = mono(30)
    for i, (k, v) in enumerate(rows):
        y = 540 + i * 86
        bx(d, 300, y, 1620, y + 68, fill=PANEL, bord=BORD, w=2, r=14)
        lt(d, 346, y + 18, k, body(28), MUT)
        lt(d, 660, y + 16, v, fm, FG)
    ct(d, W // 2, 940, "signal about financial links  ·  not proof  ·  not financial advice",
       body(28), DIM)
    save(img, "s22_outro")


if __name__ == "__main__":
    print("slides:")
    for fn in (s01_hook, s02_problem, s03_gap, s07_graph, s08_diamonds, s10_fan,
               s15_api, s16_complete, s17_method1, s18_method2, s19_method3,
               s20_limits, s21_next, s22_outro):
        fn()
    from narration_segments import SEGMENTS
    want = {s["id"] for s in SEGMENTS if s["kind"] == "slide"}
    have = {p.stem for p in OUT.glob("*.png")}
    if want - have:
        raise SystemExit(f"\nmissing slides: {sorted(want - have)}")
    print(f"\n{len(have)} slides in {OUT}")
    if ALL_BAD:
        raise SystemExit(f"{len(ALL_BAD)} tight/off-frame lines above — fix before assembling")
