# -*- coding: utf-8 -*-
"""Recompute durations.json from voice/*.mp3.

The screencast is shot against these numbers (capture_tg.py reads them), so run this
after every re-synth — otherwise the picture and the words drift apart.
"""
import json, subprocess, sys
from pathlib import Path
from narration_segments import SEGMENTS

PAUSE = 0.30     # same gap the assembler leaves between segments

d = {}
for s in SEGMENTS:
    p = Path("voice") / f"{s['id']}.mp3"
    if not p.exists():
        sys.exit(f"missing {p}")
    d[s["id"]] = round(float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)]).decode()), 2)
Path("durations.json").write_text(json.dumps(d, indent=1))
total = sum(d.values()) + PAUSE * len(d)
print(f"{len(d)} clips · voice {sum(d.values()):.1f}s · with pauses {total:.1f}s "
      f"= {int(total // 60)}:{total % 60:04.1f}")
for s in SEGMENTS:
    wps = len(s["text"].split()) / d[s["id"]]
    print(f"  {s['id']:16s} {d[s['id']]:6.2f}s  {wps:4.2f} w/s  {s['kind']}")
if total > 180:
    print(f"\n!! OVER THE 3:00 CAP by {total - 180:.1f}s — trim narration")
