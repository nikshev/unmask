# -*- coding: utf-8 -*-
"""Scan the finished video for any trace of the Telegram chat list.

The crop makes it structurally impossible, and capture_tg re-checks the layout every
scene — this is the third, independent check, done on the pixels that actually shipped.

Telegram's chat list is a column of near-white cards on the left. The conversation
column at that x is always the patterned wallpaper. So: sample the left edge of every
TG second and fail if it is bright and flat the way a list card is.
"""
import json, subprocess, sys
from pathlib import Path
from PIL import Image, ImageStat

HERE = Path(__file__).parent
VIDEO = HERE.parent / "unmask-demo.mp4"
SEG = json.loads((HERE / "durations.json").read_text())
from narration_segments import SEGMENTS
PAUSE = 0.30

# Timeline position of every TG segment in the finished cut.
spans, t = [], 0.0
for s in SEGMENTS:
    d = SEG[s["id"]] + PAUSE
    if s["kind"] == "tg":
        spans.append((s["id"], t, t + d))
    t += d

tmp = HERE / "qc" / "_priv.png"
tmp.parent.mkdir(exist_ok=True)
bad, n = [], 0
for sid, a, b in spans:
    tt = a + 0.25
    while tt < b - 0.1:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{tt:.2f}",
                        "-i", str(VIDEO), "-frames:v", "1", str(tmp)], check=True)
        im = Image.open(tmp).convert("RGB")
        strip = im.crop((0, 60, 90, im.height - 80))      # left edge, below the header
        st = ImageStat.Stat(strip)
        mean = sum(st.mean) / 3
        var = sum(st.stddev) / 3
        # A chat-list card is bright (>200) and flat (low spread). The wallpaper is
        # mid-green with pattern texture.
        if mean > 200 and var < 18:
            bad.append((sid, round(tt, 2), round(mean), round(var, 1)))
        n += 1
        tt += 1.0
print(f"checked {n} frames across {len(spans)} telegram scenes")
if bad:
    print("!! POSSIBLE CHAT LIST IN FRAME:")
    for x in bad:
        print("   ", x)
    sys.exit(1)
print("clean: no chat-list column found in any sampled frame")
