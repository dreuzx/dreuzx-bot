# Meme Scanner

Polls Nansen for Smart Money wallets that have bought multiple $20k–$100k
market-cap tokens currently trending across Solana, Ethereum, Base, BNB and
Robinhood, cross-checks against the live token screener, and pushes a report
to Telegram every 15 minutes via GitHub Actions.

## Keys are hardcoded in scan.py

Per your setup, `NANSEN_API_KEY`, `TELEGRAM_BOT_TOKEN`, and
`TELEGRAM_CHAT_ID` are written directly into `scan.py` rather than pulled
from environment/secrets. That means:

- **Keep this repo Private.** Repo → Settings → General → Danger Zone →
  Change visibility, if it isn't already. A public repo with live keys in
  it will get scraped and drained/abused within minutes.
- Anyone with read access to the repo has full use of the key and bot.
- If you ever want to change any of them later, just edit the constants at
  the top of `scan.py` directly — no secrets dashboard involved.

Only the optional `TWITTER_BEARER_TOKEN` still comes from GitHub Secrets
(step 3 below).

## New: on-demand /scantoken command (bot.py)

`bot.py` is a separate, persistent program that does two things in one
process:

1. Runs the same scan every 15 minutes automatically
2. Listens for you typing `/scantoken` in Telegram and runs the scan
   immediately, replying only to you (it checks the chat ID)

**This needs to run continuously somewhere** — GitHub Actions cron can't
listen for incoming messages, it only wakes up on a schedule. Use one of:

- **Railway** (easiest free option): New Project → Deploy from GitHub repo →
  set the start command to `python bot.py` → deploy. It'll restart
  automatically if it crashes and stays running 24/7.
- **Render**: New → Background Worker → connect the repo → start command
  `python bot.py`.
- Any VPS / always-on machine you already have: `pip install -r
  requirements.txt && python bot.py`, ideally under something like `tmux`,
  `screen`, or a systemd service so it survives you closing the terminal.

**Important — turn off the old cron workflow.** If `bot.py` is running,
delete or disable `.github/workflows/meme-scan.yml` (Actions tab → Meme
Scanner → "..." → Disable workflow). Otherwise you'll get duplicate scans:
one from the GitHub cron, one from `bot.py`'s internal 15-minute loop.

## Setup

1. **Create a private repo** and push these files (`scan.py`,
   `requirements.txt`, `.github/workflows/meme-scan.yml`).

2. **Add a repo secret (optional)** — Settings → Secrets and variables →
   Actions → New repository secret:
   - `TWITTER_BEARER_TOKEN` (only if you have one; skip otherwise)

3. **Enable Actions** on the repo if prompted, then trigger it once manually
   from the Actions tab (`Meme Scanner` → `Run workflow`) to confirm it
   works before waiting for the schedule.

## About the "wallets that hit every popular meme" logic

Nansen doesn't expose a single metric for "wallet that caught every winning
meme." What `scan.py` does instead, using real endpoints:

- Pulls all Smart Money DEX trades (last 24h) for tokens in the $20k–$100k
  band across your 5 chains
- Flags wallets that bought **2+ distinct tokens** in that band as
  "repeat winners"
- Keeps only tokens where **2+ repeat-winner wallets overlap**
- Cross-checks those tokens against the live Token Screener (smart-money
  netflow, sorted descending) to confirm they're trending *right now*, not
  just bought once and abandoned

This is a reasonable proxy, not a guarantee — adjust `MIN_WALLET_OVERLAP` in
`scan.py` to make it stricter or looser.

## About Twitter/X mention counts

There's no free, reliable way to get live mention counts. Two honest options:

1. **X API** — set `TWITTER_BEARER_TOKEN` (requires a paid X API tier for
   the recent-counts endpoint used here) and the script will pull real
   numbers.
2. **Leave it unset** — the report includes a direct search link per token
   instead, so you can eyeball it yourself in a few seconds.

I did not wire up a scraper for this; scraping X violates its ToS and tends
to break constantly, which isn't a good foundation for something running
unattended every 15 minutes.

## Nansen credit cost

Each run costs Nansen credits (5 credits per Smart Money endpoint call, plus
token-screener cost — check `docs.nansen.ai/getting-started/credits` for
current rates). Running every 15 minutes = 96 runs/day. Watch your credit
balance for the first day or two and adjust the cron schedule in
`meme-scan.yml` if you're burning through your plan faster than expected.

## Disclaimer

This surfaces on-chain activity that already happened — it does not predict
price. Meme coins in this market-cap range are extremely volatile and
illiquid; smart-money wallets can be wrong, and past accumulation is not a
signal of future performance. Size any position accordingly.
