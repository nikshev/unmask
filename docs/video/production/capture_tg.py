# -*- coding: utf-8 -*-
"""Record the live Telegram chat with the Unmask bot into capture/tg.mp4 + scenes.json.

One continuous screencast; each scene runs for exactly as long as its narration clip
(durations.json), so the assembler only has to cut on the boundaries this script wrote.

Frames land in the scratchpad, NOT in the repo, and they are cropped to the conversation
column on the way into the video — the raw frames (which still contain the chat list)
are deleted afterwards. See the privacy note in tg.py.

Run:  python3 capture_tg.py          (needs durations.json, i.e. the voice must exist)
"""
import asyncio, base64, json, shutil, subprocess, sys, time
from pathlib import Path

import tg
from narration_segments import MINT_INSIDER_1, MINT_INSIDER_2, MINT_CLEAN, SEGMENTS

HERE = Path(__file__).parent
CAP = HERE / "capture"; CAP.mkdir(exist_ok=True)
SCRATCH = Path("/private/tmp/claude-501/-Users-yevhenshkurnhykov-Work-my-projects-video/"
               "5c29396c-d191-4e17-811c-4faa909accbb/scratchpad/tgframes")
OUT = CAP / "tg.mp4"
SCENES = CAP / "scenes.json"
FPS = 30
DUR = json.loads((HERE / "durations.json").read_text())
PAUSE = 0.30
TG_IDS = [s["id"] for s in SEGMENTS if s["kind"] == "tg"]


class Rec:
    """Chrome screencast -> numbered jpegs + their wall-clock timestamps."""

    def __init__(self, cdp, outdir):
        self.cdp, self.dir = cdp, outdir
        self.n = 0
        self.times = []
        self.t0 = None

    async def _on_frame(self, ev):
        # Ack first: Chrome sends no further frames until the current one is acked,
        # and a slow ack silently drops the frame rate to a slideshow.
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            return
        t = ev.get("metadata", {}).get("timestamp") or time.time()
        if self.t0 is None:
            self.t0 = t
        (self.dir / f"f{self.n:06d}.jpg").write_bytes(base64.b64decode(ev["data"]))
        self.times.append(t - self.t0)
        self.n += 1

    async def start(self):
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.cdp.on("Page.screencastFrame", lambda ev: asyncio.create_task(self._on_frame(ev)))
        await self.cdp.send("Page.startScreencast",
                            {"format": "jpeg", "quality": 92,
                             "maxWidth": 2400, "maxHeight": 1200, "everyNthFrame": 1})

    async def stop(self):
        try:
            await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        await asyncio.sleep(0.6)


async def main():
    missing = [i for i in TG_IDS if i not in DUR]
    if missing:
        sys.exit(f"durations.json has no {missing} — run make_durations.py first")

    p, b, pg, cdp = await tg.attach()
    chat = tg.Chat(pg, cdp)
    rec = Rec(cdp, SCRATCH)
    scenes, first_result_id = {}, None

    try:
        await chat.assert_private("startup")
        await chat.scroll_bottom()
        await pg.wait_for_timeout(1200)
        await rec.start()
        await pg.wait_for_timeout(900)          # let a first frame land
        t0 = time.time()

        async def scene(sid, action, dwell_on=None):
            """Run the action, then live out the rest of the narration's length."""
            nonlocal scenes
            start = time.time() - t0
            await chat.assert_private(sid)
            msg_id = await action()
            target = DUR[sid] + PAUSE
            left = target - ((time.time() - t0) - start)
            if left > 0.3:
                await chat.dwell(left, dwell_on if dwell_on is not None else msg_id)
            end = time.time() - t0
            scenes[sid] = {"start": round(start, 2), "end": round(end, 2)}
            SCENES.write_text(json.dumps(scenes, indent=1))
            over = (end - start) - target
            print(f"  {sid:16s} {start:6.1f} → {end:6.1f}  "
                  f"({end - start:5.2f}s / {target:5.2f}s{'  OVERRAN' if over > 0.45 else ''})",
                  flush=True)

        print("scene              start      end")

        # --- the command menu ------------------------------------------------
        async def a_menu():
            await chat.focus_input()
            await chat.type("/", delay=120)      # Telegram opens the command list
            await pg.wait_for_timeout(1100)
            return None
        await scene("s04_menu", a_menu, dwell_on=None)

        # --- send the first token --------------------------------------------
        async def a_send():
            # "check " is typed; the address is dumped fast, the way anyone pastes a
            # mint. Typing 44 base58 chars at reading speed would overrun the line.
            await chat.type("check ", delay=60)
            await chat.paste(MINT_INSIDER_1)
            await pg.wait_for_timeout(250)
            await chat.send()
            await pg.wait_for_timeout(400)
            return None
        await scene("s05_send", a_send)

        # --- the result ------------------------------------------------------
        async def a_result():
            nonlocal first_result_id
            before = await chat.last_id()
            m = await chat.wait_reply(before - 1, want_img=True)
            first_result_id = m["id"]
            await chat.scroll_bottom()
            await pg.wait_for_timeout(500)
            print(f"      result #{m['id']}: {m['text'][:80]!r}", flush=True)
            return m["id"]
        await scene("s06_result", a_result)

        # --- second token ----------------------------------------------------
        async def a_second():
            before = await chat.last_id()
            # The input keeps focus after a send, so no click here: the extra 0.9 s of
            # cursor travel is what pushed this scene past its narration.
            await chat.type("/check ", delay=45)
            await chat.paste(MINT_INSIDER_2)
            await chat.send()
            m = await chat.wait_reply(before, want_img=True)
            await chat.scroll_bottom()
            return m["id"]
        await scene("s09_second", a_second)

        # --- clean token -----------------------------------------------------
        async def a_clean():
            before = await chat.last_id()
            await chat.type("/check ", delay=45)
            await chat.paste(MINT_CLEAN)
            await chat.send()
            m = await chat.wait_reply(before)
            await chat.scroll_bottom()
            await pg.wait_for_timeout(500)
            print(f"      clean  #{m['id']}: {m['text'][:80]!r}", flush=True)
            # A clean token's graph is almost entirely white — there is nothing to draw
            # — so parking the cursor in the middle of it makes a correct answer look
            # like a failed image load. Keep it on the verdict text instead.
            b = await pg.evaluate("i => window.__msgBox(i)", m["id"])
            if b and b["h"] > 260:
                rect = {"x": b["x"] + 30, "y": b["y"] + b["h"] - 150,
                        "w": b["w"] - 120, "h": 110}
                # Move it there NOW, not only during the dwell: this scene's action
                # eats almost its whole length, so the dwell may never run and the
                # cursor would just sit wherever the previous scene parked it.
                await chat.to(rect["x"] + 90, rect["y"] + 55, steps=30)
                return rect
            return m["id"]
        await scene("s11_clean", a_clean)

        # --- cached note, then travel back to the first result ---------------
        async def a_cold():
            await chat.dwell(max(0.0, DUR["s12_cold"] * 0.55))
            await pg.evaluate("i => window.__showMsg(i)", first_result_id)
            await pg.wait_for_timeout(1400)
            return first_result_id
        await scene("s12_cold", a_cold, dwell_on=first_result_id)

        # --- tap Evidence ----------------------------------------------------
        ev_before = await chat.last_id()

        async def a_evidence():
            box = await pg.evaluate("i => window.__evidenceBtn(i)", first_result_id)
            # The click goes by coordinates, so a button scrolled off-screen would be a
            # click into whatever happens to sit there instead. Re-centre and re-read.
            if not box or not (40 < box["y"] < tg.VH - 60):
                await pg.evaluate("i => window.__showMsg(i)", first_result_id)
                await pg.wait_for_timeout(1500)
                box = await pg.evaluate("i => window.__evidenceBtn(i)", first_result_id)
            if not box or not (40 < box["y"] < tg.VH - 60):
                raise RuntimeError(f"Evidence button not on screen for {first_result_id}: {box}")
            await chat.click_at(box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            return first_result_id
        await scene("s13_evidence", a_evidence)

        # --- read the evidence ------------------------------------------------
        async def a_evidence2():
            m = await chat.wait_reply(ev_before, timeout=60_000)
            await chat.scroll_bottom()
            await pg.wait_for_timeout(600)
            print(f"      evidence #{m['id']}: {m['text'][:90]!r}", flush=True)
            return m["id"]
        await scene("s14_evidence2", a_evidence2)

        await pg.wait_for_timeout(700)
        await rec.stop()
        await chat.assert_private("final")
    finally:
        try:
            await rec.stop()
        except Exception:
            pass
        await tg.detach(p, b, cdp)

    # ---- frames -> cropped video -------------------------------------------
    if rec.n < 30:
        sys.exit(f"only {rec.n} frames captured — the tab was probably not rendering")
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(SCRATCH / "f000000.jpg")]).decode().strip()
    fw, fh = (int(x) for x in probe.split(","))
    sx = fw / tg.VW                                     # device px per CSS px
    cx, cw, ch = round(tg.CROP_X * sx), round(tg.CROP_W * sx), round(tg.CROP_H * sx)
    print(f"\n  {rec.n} frames at {fw}x{fh} · crop {cw}x{ch}+{cx}+0 "
          f"· {rec.times[-1]:.1f}s wall")
    if cx < 1:
        sys.exit("crop offset is 0 — the chat list would be in frame; refusing to encode")

    lst = SCRATCH / "list.txt"
    with lst.open("w") as f:
        for i in range(rec.n):
            d = (rec.times[i + 1] - rec.times[i]) if i + 1 < rec.n else 1 / FPS
            f.write(f"file '{SCRATCH / f'f{i:06d}.jpg'}'\nduration {max(d, 1 / 120):.4f}\n")
        f.write(f"file '{SCRATCH / f'f{rec.n - 1:06d}.jpg'}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-vf", f"crop={cw}:{ch}:{cx}:0,scale=1920:1080:flags=lanczos,fps={FPS}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "19",
         str(OUT)], check=True)
    # The raw frames still hold the chat list. They do not outlive the encode.
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\nwrote {OUT} + {SCENES.name}  (raw frames deleted)")


if __name__ == "__main__":
    asyncio.run(main())
