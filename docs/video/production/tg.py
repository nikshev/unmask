# -*- coding: utf-8 -*-
"""Shared driving code for the Telegram Web chat with the Unmask bot.

The tab is the operator's real, logged-in Chrome on CDP 9022 — we attach to it, we do
not launch our own browser, because only that profile is signed in.

PRIVACY, non-negotiable: the chat list must never reach the frame. Two independent
guards, because one of them failing silently would publish the operator's contacts:
  1. the capture is cropped to the middle (conversation) column, so the left column is
     outside the picture by construction;
  2. `assert_private()` re-measures the left column every scene and refuses to continue
     if it ever overlaps the crop.
Hiding the left column with CSS was tried and rejected: Telegram's layout collapses
(the middle column goes to width 0) and the take is lost.
"""
import asyncio, base64, time
from playwright.async_api import async_playwright

CDP = "http://127.0.0.1:9022"
CHAT_URL_PART = "web.telegram.org"

# Viewport is sized so the conversation column is exactly 16:9: 1552 - 272 = 1280 x 720.
# deviceScaleFactor 1.5 renders it at 1920x1080 — native, no upscaling, and the chat
# text lands well above the phone-legibility floor.
VW, VH, DSF = 1552, 720, 1.5
CROP_X, CROP_W, CROP_H = 272, 1280, 720          # CSS px, in page coordinates

# Playwright shows no cursor in a capture, and without one it is impossible to see what
# is being clicked. Same overlay as the Genstar demo.
CURSOR = """
(() => {
  if (document.getElementById('__cur')) return;
  const c = document.createElement('div');
  c.id = '__cur';
  c.style.cssText = 'position:fixed;left:-99px;top:-99px;z-index:2147483647;' +
    'width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;' +
    'background:rgba(255,110,0,.28);border:2px solid #ff6e00;pointer-events:none;' +
    'box-shadow:0 0 0 1px rgba(255,255,255,.85);transition:transform .08s ease-out';
  document.body.appendChild(c);
  addEventListener('mousemove', e => {
    c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
  }, true);
  addEventListener('mousedown', () => { c.style.transform = 'scale(.55)'; }, true);
  addEventListener('mouseup', () => { c.style.transform = 'scale(1)'; }, true);
})();
"""

HELPERS = """
window.__msgs = () => [...document.querySelectorAll('.Message')].map(m => ({
  id: +m.getAttribute('data-message-id'),
  own: m.classList.contains('own'),
  text: (m.innerText || '').slice(0, 300),
  hasImg: !!m.querySelector('.media-inner img, img.full-media'),
  // Telegram Web A hashes its container class names, so the inline-keyboard wrapper
  // cannot be selected by name; the <button> elements themselves are stable.
  btns: [...m.querySelectorAll('button')].map(b => b.innerText.trim()).filter(Boolean),
}));
window.__box = (sel) => {
  const e = typeof sel === 'string' ? document.querySelector(sel) : sel;
  if (!e) return null;
  const b = e.getBoundingClientRect();
  return {x: b.x, y: b.y, w: b.width, h: b.height};
};
window.__msgEl = (id) =>
  document.querySelector(`.Message[data-message-id="${id}"]`);
window.__msgBox = (id) => window.__box(window.__msgEl(id));
window.__showMsg = (id) => {
  const e = window.__msgEl(id); if (!e) return false;
  e.scrollIntoView({behavior: 'smooth', block: 'center'}); return true;
};
window.__evidenceBtn = (id) => {
  const e = window.__msgEl(id); if (!e) return null;
  const b = [...e.querySelectorAll('button')]
            .find(x => /evidence/i.test(x.innerText));
  return b ? window.__box(b) : null;
};
window.__scrollBottom = () => {
  const c = document.querySelector('.MessageList')?.closest('.custom-scroll')
         || document.querySelector('.messages-container')?.parentElement;
  if (c) c.scrollTop = c.scrollHeight;
  return !!c;
};
"""


class Chat:
    """One Telegram conversation, driven by coordinates with a visible cursor."""

    def __init__(self, pg, cdp):
        self.pg, self.cdp = pg, cdp
        self.x = self.y = 0

    # ---- privacy guard -------------------------------------------------
    async def assert_private(self, where=""):
        """Refuse to keep recording if the chat list could be inside the crop."""
        left = await self.pg.evaluate("() => window.__box('#LeftColumn')")
        if left and left["x"] + left["w"] > CROP_X + 1:
            raise RuntimeError(
                f"PRIVACY STOP at {where}: chat list reaches x={left['x'] + left['w']:.0f}, "
                f"crop starts at {CROP_X}")
        mid = await self.pg.evaluate("() => window.__box('#MiddleColumn')")
        if not mid or abs(mid["x"] - CROP_X) > 2 or mid["w"] < CROP_W - 2:
            raise RuntimeError(f"layout moved at {where}: middle column {mid}")

    # ---- cursor --------------------------------------------------------
    async def to(self, x, y, steps=26):
        await self.pg.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y

    async def click_at(self, x, y):
        await self.to(x, y)
        await self.pg.wait_for_timeout(380)
        await self.pg.mouse.click(x, y)
        await self.pg.wait_for_timeout(500)

    async def input_box(self):
        b = await self.pg.evaluate("() => window.__box('#editable-message-text')")
        if not b:
            raise RuntimeError("message input not found")
        return b

    async def focus_input(self):
        b = await self.input_box()
        await self.click_at(b["x"] + 60, b["y"] + b["h"] / 2)

    async def type(self, text, delay=55):
        await self.pg.keyboard.type(text, delay=delay)

    async def paste(self, text):
        """Drop a long string in at once, the way anyone pastes a mint address.

        Typing 44 base58 characters costs a CDP round trip each and ran the send scene
        1.2 s past its narration; the assembler would have cut it off mid-address.
        """
        await self.cdp.send("Input.insertText", {"text": text})
        await self.pg.wait_for_timeout(120)

    async def send(self):
        await self.pg.keyboard.press("Enter")

    async def scroll_bottom(self):
        await self.pg.evaluate("() => window.__scrollBottom()")
        await self.pg.wait_for_timeout(600)

    async def msgs(self):
        return await self.pg.evaluate("() => window.__msgs()")

    async def last_id(self):
        m = await self.msgs()
        return m[-1]["id"] if m else 0

    async def wait_reply(self, after_id, timeout=120_000, want_img=False):
        """Wait for the bot's answer after `after_id`. Returns the message dict.

        `want_img` waits for the rendered graph too: the text arrives first and the
        picture a beat later, and cutting between the two shows a grey placeholder.
        """
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for m in reversed(await self.msgs()):
                if m["id"] > after_id and not m["own"]:
                    if not want_img or m["hasImg"]:
                        return m
            await self.pg.wait_for_timeout(400)
        raise RuntimeError(f"no reply after id {after_id} in {timeout / 1000:.0f}s")

    async def dwell(self, seconds, msg_id=None):
        """Keep the frame alive: drift the cursor over the message being talked about.

        A frozen picture reads as a stalled recording, and Chrome's screencast emits
        nothing at all while nothing moves.
        """
        end = time.time() + seconds
        if isinstance(msg_id, dict):
            box = msg_id              # an explicit rect, for messages that are mostly
        else:                         # whitespace — see the clean-token scene
            box = (await self.pg.evaluate("i => window.__msgBox(i)", msg_id)) if msg_id else None
        if not box or box["w"] < 40:
            box = {"x": CROP_X + 80, "y": 180, "w": CROP_W - 200, "h": 360}
        pts = [(0.25, 0.30), (0.62, 0.48), (0.38, 0.72), (0.70, 0.35)]
        i = 0
        while time.time() < end - 0.7:
            fx, fy = pts[i % len(pts)]
            x = max(CROP_X + 20, min(CROP_X + CROP_W - 20, box["x"] + box["w"] * fx))
            y = max(20, min(VH - 70, box["y"] + box["h"] * fy))
            await self.to(x, y, steps=34)
            await self.pg.wait_for_timeout(420)
            i += 1
        left = end - time.time()
        if left > 0:
            await self.pg.wait_for_timeout(int(left * 1000))


async def attach(keep_emulation=True):
    """Attach to the live Telegram tab and put it into capture geometry."""
    p = await async_playwright().start()
    b = await p.chromium.connect_over_cdp(CDP)
    pg = None
    for ctx in b.contexts:
        for page in ctx.pages:
            if CHAT_URL_PART in page.url:
                pg = page
    if pg is None:
        raise RuntimeError("no Telegram Web tab on CDP 9022")
    await pg.bring_to_front()
    cdp = await pg.context.new_cdp_session(pg)
    await cdp.send("Emulation.setDeviceMetricsOverride",
                   {"width": VW, "height": VH, "deviceScaleFactor": DSF, "mobile": False})
    # Without focus emulation a background tab stops rendering and the capture is
    # frozen frames ([[video-youtube-pinning]] — same root cause, same fix).
    await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    await pg.evaluate(HELPERS)
    await pg.evaluate(CURSOR)
    await pg.wait_for_timeout(900)
    return p, b, pg, cdp


async def detach(p, b, cdp, restore=True):
    if restore:
        try:
            await cdp.send("Emulation.clearDeviceMetricsOverride")
            await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
        except Exception:
            pass
    await b.close()
    await p.stop()
