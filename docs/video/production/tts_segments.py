# -*- coding: utf-8 -*-
"""Synthesize each narration segment to voice/<id>.mp3 via ElevenLabs. Cached: an
existing clip is not re-sung unless its id is passed on the command line.

Run: ELEVENLABS_API_KEY=... python3 tts_segments.py [id ...]

SPEED note: the YouTube channel runs 0.85 (see memory `video-tts-quality`). This is a
hackathon demo with a HARD 3:00 cap, and 0.95 was tried first; it landed at 3.2 words/s,
too rushed for numbers judges must absorb. 0.85 fits the cap with room to spare.
"""
import json, os, sys, urllib.request, urllib.error
from pathlib import Path
from narration_segments import SEGMENTS

VOICE_ID = "vsbamHjSri0whdbDPwdy"      # Yevhen (cloned)
MODEL_ID = "eleven_multilingual_v2"
SPEED = 0.85
VOICE_SETTINGS = {"stability": 0.30, "similarity_boost": 0.85,
                  "style": 0.45, "use_speaker_boost": True, "speed": SPEED}
OUT = Path(__file__).parent / "voice"


def synth(text, path, key):
    body = json.dumps({"text": text, "model_id": MODEL_ID,
                       "voice_settings": VOICE_SETTINGS}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}", data=body,
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        path.write_bytes(r.read())


def main():
    key = os.environ.get("ELEVENLABS_API_KEY") or sys.exit("ELEVENLABS_API_KEY not set")
    only = set(sys.argv[1:])
    OUT.mkdir(exist_ok=True)
    failed = []
    for s in SEGMENTS:
        p = OUT / f"{s['id']}.mp3"
        if only and s["id"] not in only:
            continue
        if p.exists() and p.stat().st_size > 0 and not only:
            print(f"  [cache] {p.name}", flush=True); continue
        print(f"  [tts]   {s['id']} ({len(s['text'])} chars)", flush=True)
        try:
            synth(s["text"], p, key)
        except urllib.error.HTTPError as e:
            # A 429 here is why a `| tail` once hid a half-finished run and called it
            # a success ([[north-production-pitfalls]]). Collect and fail loudly.
            failed.append(f"{s['id']}: HTTP {e.code} {e.read()[:200]!r}")
            print(f"  [FAIL]  {s['id']}: HTTP {e.code}", flush=True)
    done = sorted(OUT.glob("*.mp3"))
    print(f"\n{len(done)}/{len(SEGMENTS)} clips in voice/")
    if failed:
        sys.exit("FAILED:\n  " + "\n  ".join(failed))
    if len(done) < len(SEGMENTS):
        sys.exit(f"missing {len(SEGMENTS) - len(done)} clips")


if __name__ == "__main__":
    main()
