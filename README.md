# GAF — Garlic Agentic Framework

Two Telegram bots sharing one group and one process: **Alfred Cockburn**, a dry English
butler, and the **Joker**, a rude one. DeepSeek V4 Flash brain, Google AI Studio images
and vision, Tavily web search, SQLite memory. Five small Python files, long polling, no
framework beyond aiogram.

## How they decide to speak

- **Mentioned** (`@botusername`, a reply, or their names — alfred/gaf/cockburn/butler and
  Альфред, joker/jerker/jester and Джокер/Джеркер, any case ending) → that one answers.
  Only what a human typed counts as a mention, never the caption Gemini writes for an image.
- **Not mentioned** → after a 2.5 s lull, a tiny non-thinking DeepSeek call asks whether
  either should interject — capped at 1 unprompted reply per 5 min and 4 per hour each.
- **Then they keep going.** Once one of them speaks, the other takes a turn 8 s later, and
  they alternate **until muted**. There is no turn limit; `/mute` is the brake.

Telegram never delivers one bot's messages to another, so they don't hear each other
through Telegram at all — both run in one process and share one in-memory transcript.

`/mute joker`, `/mute alfred`, `/mute all` (bare `/mute` means all), and `/unmute` the
same way — their nicknames work too, so `/mute джеркер` is fine. A mute cancels whatever
is in flight *immediately*, mid-sentence, and is stored in SQLite so a redeploy can't
quietly bring the flood back.

Tools available on each reply: `tavily_search`, `generate_image`, `remember`, `forget`.
Memories live in one SQLite table shared by both and are injected into both system
prompts; `/memory` prints the whole ledger with its `#n` ids, so you can tell them which
one to forget.

Photos, PDFs and text files are described by Gemini Flash the moment they arrive, and
the description goes into the transcript as `[photo: …]` — so he can react to an image
unprompted, and still answer about one sent twenty messages ago. Anything else (zip,
voice, video) is logged as a bare `[sent a file: …]` with no API call.

## Setup

1. **BotFather**: `/newbot` → save the token. Then `/setprivacy` → your bot →
   **Disable** (required so he sees all group messages, not just commands). Repeat for
   a second bot if you want the Joker, and put its token in `JOKER_BOT_TOKEN` — omit it
   and only Alfred runs. Do *not* enable Bot-to-Bot Communication Mode; it isn't used.
2. **Group**: add the bot(s) to the group. Do this *before* step 3 — if a group
   upgrades to a supergroup later, its chat id changes.
3. **Chat id**: post anything in the group, then
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the negative
   `chat.id` (make sure the bot isn't polling anywhere while you do this).
4. **Keys**: DeepSeek (platform.deepseek.com, top up a few dollars), Google AI Studio
   — one key serves both image generation and vision (the default image model
   `gemini-3.1-flash-image` needs a **billed** project, ~$0.05/image; set
   `IMAGE_MODEL=gemini-2.5-flash-image` to stay on the free tier, and
   `VISION_MODEL=gemini-2.5-flash` for cheaper reading) — Tavily (free tier, 1000
   credits/month is plenty).

## Run locally

```bash
cp .env.example .env   # fill in values
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

Only one poller per token at a time: stop the local bot before the Render instance runs,
or Telegram returns 409 on getUpdates. With two bots that's two ways to get it wrong —
a forgotten local process makes the deployed pair look randomly deaf.

## Deploy (Render)

Push this repo to GitHub → Render → **New → Blueprint** → pick the repo. Render reads
`render.yaml` (background worker, Starter $7/mo + 1 GB disk $0.25/mo), prompts for the
secret env vars, deploys. Check logs for "on duty:" — it lists every persona that started.
Redeploys happen on `git push`; `/data/gaf.db` (their memory, and who is muted) survives
them. `/mute all` before pushing, so the redeploy can't drop you back into a live chain.

## Tuning

Chattiness knobs are constants at the top of `bot.py`: `DEBOUNCE_S`, `COOLDOWN_S`,
`HOURLY_MAX`, `HISTORY_MAX`, and `ALFRED_RE` / `JOKER_RE` (add the group's own nicknames —
the Cyrillic branches take a stem plus `\w*`, so case endings come free). What they'll
read is `VISION_MIMES` / `VISION_MAX_BYTES`. Their personalities are `prompts.py`.

`TURN_GAP_S` (8 s) is the pause between bot turns, and it is a **rate limit, not a taste
knob**: Telegram allows 20 messages/minute per group, shared by both bots, and a turn can
emit two (text plus an image). Below ~6 s you will start seeing `flood control: waiting Ns`
in the logs. Since nothing caps the number of turns, an unattended chain runs until muted —
budget accordingly, especially for `generate_image` at ~$0.05 a picture.
