#!/usr/bin/env python3
"""Audio QA for TTS clips: transcribe each voice/*.mp3 back with Whisper, detect the language,
and compare to the source narration text. Flags clips that drifted (non-English) or garbled
(low similarity to the script). Usage: tts_qc.py <production_dir> [model]"""
import sys, re, difflib, importlib.util
from pathlib import Path
from faster_whisper import WhisperModel

prod = Path(sys.argv[1])
model_name = sys.argv[2] if len(sys.argv) > 2 else "small"

spec = importlib.util.spec_from_file_location("ns", prod / "narration_segments.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = {s["id"]: s["text"] for s in m.SEGMENTS}

def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()

print(f"model={model_name}  dir={prod.name}\n")
wm = WhisperModel(model_name, device="cpu", compute_type="int8")
print(f"{'clip':22} {'lang':5} {'p':>4} {'sim':>5}  verdict  transcript-snippet")
print("-" * 100)
bad = []
for sid, text in src.items():
    mp3 = prod / "voice" / f"{sid}.mp3"
    if not mp3.exists():
        continue
    segs, info = wm.transcribe(str(mp3), language=None, beam_size=1)
    tr = " ".join(s.text for s in segs).strip()
    # autojunk=False: difflib's default junk heuristic tanks the ratio on long (>200 char)
    # strings, giving false "garbled" flags on clips that are actually fine.
    sim = difflib.SequenceMatcher(None, norm(tr), norm(text), autojunk=False).ratio()
    # A short garbled/drifted span barely dents the OVERALL similarity, so sim alone misses
    # localized drift. Also flag clips the model was UNSURE about (low language probability)
    # unless they matched near-perfectly — that low confidence is the tell for a garbled span.
    flag = (info.language != "en") or (sim < 0.55) or (info.language_probability < 0.60 and sim < 0.92)
    verdict = "❌ BAD" if flag else "ok"
    if flag:
        bad.append((sid, info.language, round(info.language_probability, 2), round(sim, 2)))
    print(f"{sid:22} {info.language:5} {info.language_probability:4.2f} {sim:5.2f}  {verdict:7}  {tr[:44]}")
print("-" * 100)
print(f"\nFLAGGED ({len(bad)}/{len(src)}):", [b[0] for b in bad])
for b in bad:
    print("  ", b)
