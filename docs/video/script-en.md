# Voiceover and shot list (English, ≈ 3:00)

Narration is written at ≈ 2.4 words per second. Pauses are marked `[pause]`. Screen actions are in *italics*.
All tokens are pre-analyzed on the server; replies are instant (see `README.md`).

## 0:00–0:20 — The problem

**Screen:** title card "Unmask — who is really behind the buyers", then `stills/insider_GnQUsLcy.png` slowly zooming in.

> A new Solana token shows hundreds of early buyers, and it looks like organic demand. [pause]
> But often it is one actor: one wallet funds dozens of fresh addresses, they all buy in the first seconds,
> and later sell to retail. Trackers show who bought. They almost never show who stands behind them. [pause]
> Unmask answers exactly that.

(≈ 55 words)

## 0:20–1:40 — The live product

**Screen:** Telegram chat with the bot.

1. *Type `/` — the command menu appears: `/start`, `/help`, `/check`.* (2–3 s on the menu)
2. *Send `/check GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump`.*

> The whole interface is a Telegram bot. Type slash, and the commands appear. [pause]
> Check, then a token address. [pause]
> Risk seventy-two out of a hundred, high concentration. Two clusters. The first one is six wallets that
> received ninety-two percent of what the analyzed early buyers got.

3. *Show the graph image full-screen for ~8 s; point at elements as you name them.*

> Here is the graph. Each circle is a cluster. Grey dots are wallets outside the cluster that connect to it,
> here a common funder. Thick red lines: one funder paying many buyers. The diamonds are one piece of evidence
> tying a group of addresses together. They are not transfers between them.

4. *Send `/check AKF3enKZKEpaQwwP3fU6ij3P5pj3VEpRXEuQ3NE5pump`; show the graph.*

> Another token: three clusters, and one funder feeding all three. Look at the red fan from a single grey wallet.

5. *Send `/check GpK8AqaHvKePqF3bbYNDg2h9w3drAhqepRCcXXEqpump`.*

> And for contrast, a clean token. Risk zero, no related groups found. Unmask does not invent clusters. [pause]
> A token it has not seen takes about two minutes, with progress messages. After that, the result is cached.

(≈ 150 words; the rest of the 80 s is screen time)

## 1:40–2:20 — The evidence

**Screen:** go back to the first message (`GnQUsLcy…`), tap **Evidence**; scroll the evidence message slowly.
Then (optional) a terminal on the server: `curl -s localhost:18080/api/token/GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump | python3 -m json.tool | head -60`.

> Every claim comes with evidence. Tap Evidence. For each cluster you see the type: shared funder, same amounts,
> same slot, indirect link. The wallets involved, and the time window in block time or in slots. [pause]
> The same data is in the public API: clusters, confidence, supply share, and the evidence list. Anyone can verify it
> on-chain. [pause]
> If the data is incomplete, Unmask says so. It never shows incomplete data as clean.

(≈ 80 words)

## 2:20–3:00 — Method, honesty, next steps

**Screen:** a simple diagram or `docs/video/stills/` frames; end on the bot chat plus the repository URL.

> How it works. We take the first buyers of a token and pull each wallet's funding history through an RPC pool:
> RPC Fast serves the recent history, archival providers back it up for older tokens. [pause]
> We build a funding graph and prune exchanges, routers and program accounts, otherwise everything glues into one blob.
> Wallets sharing a funder in a narrow time window are merged, indirect links are added, and behavior signals such as
> identical amounts and the same slot raise the confidence. [pause]
> This is a signal about financial links, not proof, and not financial advice. Limits are in the README.
> Next: deeper buyer samples on faster RPC, and monitoring of new launches. [pause]
> Unmask. See who is behind the buyers.

(≈ 115 words)

## Recording checklist

- Telegram Web or desktop at 1280×720 or larger; zoom the chat to 125%.
- Record the screen without voice first, then add narration; match each narrated line to its screen action.
- Do not show the bot token, `.env`, or RPC URLs. They are not in the chat.
- Keep the final card up for 3–4 seconds: project name, repository, bot name.
