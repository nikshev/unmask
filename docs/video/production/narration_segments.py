# -*- coding: utf-8 -*-
"""Unmask — hackathon demo video (Crypto World's Fair / Superteam Earn), English, <= 3:00.

Content is the approved scenario in `docs/video/script-en.md`, cut into one segment per
screen action so the screencast and the slides can be timed to the voice instead of the
other way round.

`kind` tells the pipeline where each segment's picture comes from:

  "tg"     — recorded live in the Telegram chat with the bot (capture_tg.py writes a
             scene of exactly this id into scenes.json; the assembler cuts it out).
  "slide"  — a still rendered by slides.py into slides/<id>.png.

Hard rules this file obeys:
  - Every number spoken here is a number the bot really returned (see api_GnQUsLcy.json
    and the chat): risk 72, two clusters, cluster #1 = 6 wallets, share 0.9206.
  - Nothing is called "fresh" or a "live launch": the tokens are real launches from
    early 2025, which is what the data covers.
  - "Signal, not proof" is said out loud, once, in full.
  - Numbers are spelled out so the TTS reads them as words, not as digits.
"""

# Tokens are pre-analysed on the server, so the bot answers from cache instantly.
MINT_INSIDER_1 = "GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump"
MINT_INSIDER_2 = "AKF3enKZKEpaQwwP3fU6ij3P5pj3VEpRXEuQ3NE5pump"
MINT_CLEAN = "GpK8AqaHvKePqF3bbYNDg2h9w3drAhqepRCcXXEqpump"

SEGMENTS = [
    # ---- 0:00-0:22 — the problem ------------------------------------------
    {"id": "s01_hook", "kind": "slide",
     "text": "A new Solana token shows hundreds of early buyers. It looks like organic demand."},

    {"id": "s02_problem", "kind": "slide",
     "text": "But often it is one actor. One wallet funds dozens of fresh addresses, "
             "they all buy in the first seconds, and later sell to retail."},

    {"id": "s03_gap", "kind": "slide",
     "text": "Trackers show you who bought. They almost never show who stands behind them. "
             "Unmask answers exactly that."},

    # ---- 0:22-1:40 — the live product -------------------------------------
    {"id": "s04_menu", "kind": "tg",
     "text": "The whole interface is a Telegram bot. Type slash, and the commands appear."},

    {"id": "s05_send", "kind": "tg",
     "text": "Check, then a token address."},

    {"id": "s06_result", "kind": "tg",
     "text": "Risk seventy-two out of a hundred. High concentration. Two clusters. "
             "The first one is six wallets that received ninety-two percent of everything "
             "the analysed early buyers got."},

    {"id": "s07_graph", "kind": "slide",
     "text": "Here is the graph the bot sends with it. Each circle is a cluster. "
             "The grey dots are wallets outside the cluster that connect to it. "
             "Here, a common funder. Thick red lines: one funder paying many buyers."},

    {"id": "s08_diamonds", "kind": "slide",
     "text": "The diamonds are a single piece of evidence tying a group of addresses together. "
             "They are not transfers between those addresses."},

    {"id": "s09_second", "kind": "tg",
     "text": "Another token. Three clusters, and one funder feeding all three."},

    {"id": "s10_fan", "kind": "slide",
     "text": "Look at the red fan spreading out of a single grey wallet."},

    {"id": "s11_clean", "kind": "tg",
     "text": "And for contrast, a clean token. Risk zero. No related groups found. "
             "Unmask does not invent clusters."},

    {"id": "s12_cold", "kind": "tg",
     "text": "A token it has not seen takes about two minutes, with progress messages. "
             "After that, the result is cached."},

    # ---- 1:40-2:20 — the evidence -----------------------------------------
    {"id": "s13_evidence", "kind": "tg",
     "text": "Every claim comes with evidence. Tap Evidence."},

    {"id": "s14_evidence2", "kind": "tg",
     "text": "For each cluster you get the type. Shared funder, identical amounts, the same slot, "
             "an indirect link. Which wallets are involved. And the time window, "
             "in block time or in slots."},

    {"id": "s15_api", "kind": "slide",
     "text": "The same data is in the public API. Clusters, confidence, supply share, "
             "and the full evidence list. Anyone can replay it on chain."},

    {"id": "s16_complete", "kind": "slide",
     "text": "And if the data is incomplete, Unmask says so. "
             "It never shows incomplete data as clean."},

    # ---- 2:20-3:00 — method, honesty, next --------------------------------
    {"id": "s17_method1", "kind": "slide",
     "text": "How it works. We take the first buyers of a token and pull each wallet's funding "
             "history through an RPC pool. RPC Fast serves the recent history, archival providers "
             "back it up for older tokens."},

    {"id": "s18_method2", "kind": "slide",
     "text": "We build a funding graph and prune the hubs. Exchanges, routers, program accounts. "
             "Otherwise everything glues into one blob."},

    {"id": "s19_method3", "kind": "slide",
     "text": "Wallets sharing a funder inside a narrow time window get merged. Indirect links are "
             "added. Behavioural signals, like identical amounts in the same slot, raise the "
             "confidence."},

    {"id": "s20_limits", "kind": "slide",
     "text": "This is a signal about financial links. It is not proof of insider trading, "
             "and not financial advice. The limits are in the README."},

    {"id": "s21_next", "kind": "slide",
     "text": "What is next. Deeper buyer samples on faster RPC, and monitoring of new launches."},

    {"id": "s22_outro", "kind": "slide",
     "text": "Unmask. See who is behind the buyers."},
]

TG_SCENES = [s["id"] for s in SEGMENTS if s["kind"] == "tg"]
SLIDES = [s["id"] for s in SEGMENTS if s["kind"] == "slide"]

if __name__ == "__main__":
    words = sum(len(s["text"].split()) for s in SEGMENTS)
    print(f"{len(SEGMENTS)} segments · {words} words "
          f"· ~{words / 2.5:.0f}s of voice at 2.5 w/s")
    print(f"  tg scenes: {len(TG_SCENES)} · slides: {len(SLIDES)}")
