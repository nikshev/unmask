# -*- coding: utf-8 -*-
"""Assemble the Unmask demo: one clip per narration segment, voice laid on a timeline,
a quiet music bed, and an SRT from the same timeline.

Picture per segment:
  kind "tg"    -> cut out of capture/tg.mp4 on the boundaries capture_tg.py recorded
  kind "slide" -> slides/<id>.png held for the length of the clip

The screencast was already shot against these durations, so the fit here is insurance
against small drift, not a rescue of a 5-second scene stretched over a minute.

Run: python3 assemble_video.py
"""
import json, subprocess, sys
from pathlib import Path
from narration_segments import SEGMENTS

HERE = Path(__file__).parent
CAP = HERE / "capture"
TG = CAP / "tg.mp4"
SCENES = json.loads((CAP / "scenes.json").read_text()) if (CAP / "scenes.json").exists() else {}
SLIDES = HERE / "slides"
CLIPS = CAP / "clips"
SILENT = CAP / "video_silent.mp4"
OUTPUT = HERE.parent / "unmask-demo.mp4"
SRT = HERE.parent / "unmask-demo.srt"

FPS, W, H = 30, 1920, 1080
PAUSE = 0.30
MUSIC = Path("/Users/yevhenshkurnhykov/Work/my_projects/video/background")
# 0.03, not 0.05 and never 0.09 — see memory `video-tts-quality`. A bed you notice is
# a bed that is too loud under a voice carrying numbers.
MUSIC_VOL = 0.03
CAP_SECONDS = 180          # the submission's hard limit

# Every clip must encode to a BYTE-IDENTICAL SPS, otherwise concatenating them by stream
# copy yields one track containing several parameter sets while the container's avcC
# names only the first. ffmpeg and Chrome re-configure on the in-band change; QuickTime
# and other AVFoundation players do not, and stop at the switch.
VTAIL = "format=yuv420p,setsar=1"
VFLAGS = ["-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
          "-color_primaries", "bt709", "-color_trc", "bt709"]


def dur(p):
    try:
        return float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)]).decode().strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def srt_ts(t):
    h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
    return f"{h:02d}:{m:02d}:{s:02d},{int(round((t - int(t)) * 1000)):03d}"


def lines_of(text, width=42):
    out, line = [], ""
    for w in text.split():
        if line and len(line) + 1 + len(w) > width:
            out.append(line); line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def cues(text, start, length, width=42, max_lines=2):
    """Split one segment into readable cues, timed by share of the characters.

    A 144-character sentence cannot sit in a single two-line cue; forcing it produced
    80-character lines that run off a phone screen. Splitting into several cues is what
    subtitles are supposed to do.
    """
    ls = lines_of(text, width)
    chunks = ["\n".join(ls[i:i + max_lines]) for i in range(0, len(ls), max_lines)]
    total = sum(len(c) for c in chunks) or 1
    out, t = [], start
    for i, c in enumerate(chunks):
        d = length * len(c) / total
        # the last cue absorbs the rounding so the segment ends exactly on its voice
        end = start + length if i == len(chunks) - 1 else t + d
        out.append((t, end, c))
        t = end
    return out


def run(args):
    subprocess.run(args, check=True)


def main():
    voice = {s["id"]: HERE / "voice" / f"{s['id']}.mp3" for s in SEGMENTS}
    missing = [k for k, v in voice.items() if not v.exists()]
    if missing:
        sys.exit(f"no voice for: {missing} — run tts_segments.py")
    vdur = {k: dur(v) for k, v in voice.items()}

    total = sum(vdur[s["id"]] + PAUSE for s in SEGMENTS)
    if total > CAP_SECONDS:
        sys.exit(f"runtime {total:.1f}s is over the {CAP_SECONDS}s cap — trim narration")

    CLIPS.mkdir(parents=True, exist_ok=True)
    parts = []
    for s in SEGMENTS:
        sid = s["id"]
        want = vdur[sid] + PAUSE
        out = CLIPS / f"{sid}.mp4"
        have = dur(out) if out.exists() else None
        if have is not None and abs(have - want) < 0.12:
            parts.append(out); print(f"  {sid:16s} {want:5.2f}s  [cache]"); continue

        if s["kind"] == "tg":
            sc = SCENES.get(sid) or sys.exit(f"scene {sid} missing from scenes.json")
            if not TG.exists():
                sys.exit(f"{TG} missing — run capture_tg.py")
            run(["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", f"{sc['start']:.2f}", "-i", str(TG),
                 "-t", f"{sc['end'] - sc['start']:.2f}",
                 # tpad clones the last frame if the scene came out shorter than its
                 # voice; the -t after it trims if longer. Together: exact length.
                 "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                         f"tpad=stop_mode=clone:stop_duration=20,fps={FPS},"
                         # tg.mp4 comes from JPEG frames and is therefore FULL range
                         # (yuvj420p); the slides are limited range. Concatenating the
                         # two by stream copy put two different SPS in one track while
                         # avcC advertised only the first, which is what players that
                         # configure once from avcC choke on. Normalise here.
                         f"scale=in_range=full:out_range=tv,{VTAIL}"),
                 "-t", f"{want:.2f}", "-an",
                 "-c:v", "libx264", *VFLAGS, "-preset", "veryfast",
                 "-crf", "19", str(out)])
        else:
            png = SLIDES / f"{sid}.png"
            if not png.exists():
                sys.exit(f"{png} missing — run slides.py")
            run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png),
                 "-t", f"{want:.2f}",
                 "-vf", f"scale={W}:{H}:out_color_matrix=bt709,fps={FPS},{VTAIL}",
                 "-an", "-c:v", "libx264", *VFLAGS, "-preset", "veryfast",
                 "-crf", "18", str(out)])
        parts.append(out)
        print(f"  {sid:16s} {want:5.2f}s  {s['kind']}")

    lst = CLIPS / "concat.txt"
    lst.write_text("\n".join(f"file '{p.resolve()}'" for p in parts) + "\n")
    have = dur(SILENT) if SILENT.exists() else None
    if have is None or abs(have - total) > 1.0:
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", str(SILENT)])

    t, inputs, filt, labels, srt, n = 0.0, [], [], [], [], 0
    for i, s in enumerate(SEGMENTS):
        sid = s["id"]
        ms = int(t * 1000)
        inputs += ["-i", str(voice[sid])]
        filt.append(f"[{i + 1}:a]adelay={ms}|{ms}[a{i}]")
        labels.append(f"a{i}")
        for a, b, text in cues(s["text"], t, vdur[sid]):
            n += 1
            srt.append(f"{n}\n{srt_ts(a)} --> {srt_ts(b)}\n{text}\n")
        t += vdur[sid] + PAUSE
    SRT.write_text("\n".join(srt) + "\n")

    tracks = sorted(MUSIC.glob("*.mp3"))
    if tracks:
        bed = next((x for x in tracks if "Circuit" in x.name), tracks[0])
        print(f"\n  music bed: {bed.name} @ {MUSIC_VOL}")
        inputs += ["-stream_loop", "-1", "-i", str(bed)]
        idx = len(SEGMENTS) + 1
        filt.append(f"[{idx}:a]volume={MUSIC_VOL},afade=t=in:st=0:d=2,"
                    f"afade=t=out:st={max(0, t - 3):.2f}:d=3[mus]")
        labels.append("mus")

    # The raw mix lands near -20 LUFS, which plays noticeably quiet in a browser next to
    # everything else a judge watches. loudnorm brings it to the usual web target and
    # limits on the way, which a flat gain could not: true peak was already -1 dBFS.
    #
    # TWO passes, not one. Single-pass loudnorm adapts its gain as it goes, so while it
    # is still measuring the opening silence it applies enormous gain and fires a click
    # at about t=0.3 — a pop on the very first frame of the video. Measuring first and
    # then applying fixed values removes the ramp entirely.
    mixfc = (";".join(filt) + ";" + "".join(f"[{l}]" for l in labels)
             + f"amix=inputs={len(labels)}:normalize=0,"
             # a short fade kills any decoder transient on the first samples
             f"afade=t=in:st=0:d=0.05[mix]")
    premix = CAP / "mix_raw.wav"
    # The silent video stays as input 0 even though this pass writes only audio: the
    # adelay labels above are numbered against that layout, and dropping it would
    # shift every [n:a] by one.
    # -t is mandatory here: the music bed is fed with `-stream_loop -1`, and with no
    # video stream mapped there is nothing to stop amix, so the wav grows forever.
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(SILENT), *inputs,
         "-filter_complex", mixfc, "-map", "[mix]", "-t", f"{t:.2f}",
         # 48 kHz stereo explicitly: amix otherwise adopts the highest rate among the
         # inputs and the master came out 96 kHz mono, which some players and upload
         # pipelines handle badly.
         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(premix)])

    meas = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(premix),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    blob = json.loads(meas[meas.rindex("{"):meas.rindex("}") + 1])
    print(f"  loudness in: {blob['input_i']} LUFS -> target -16 LUFS")
    ln = (f"loudnorm=I=-16:TP=-1.5:LRA=11"
          f":measured_I={blob['input_i']}:measured_LRA={blob['input_lra']}"
          f":measured_TP={blob['input_tp']}:measured_thresh={blob['input_thresh']}"
          f":offset={blob['target_offset']}:linear=true")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(SILENT), "-i", str(premix),
         "-af", ln, "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", "-shortest", str(OUTPUT)])
    premix.unlink(missing_ok=True)
    print(f"\nWrote {OUTPUT.name}  {int(t // 60)}:{t % 60:04.1f}  "
          f"(cap {CAP_SECONDS // 60}:{CAP_SECONDS % 60:02d})  + {SRT.name}")


if __name__ == "__main__":
    main()
