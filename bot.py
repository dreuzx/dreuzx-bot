"""
Persistent Telegram bot for the meme scanner.

Two things run in this one process:
1. A background loop that runs the scan every SCAN_INTERVAL_MINUTES and
   reports to Telegram (same logic as scan.py's scheduled run).
2. A long-polling loop that listens for you typing /scantoken in Telegram
   and runs the scan immediately on demand.

This REPLACES the GitHub Actions cron schedule (meme-scan.yml) -- you only
need one or the other, not both, or you'll get duplicate messages. See
README for hosting instructions; this needs to run on something that stays
on (Railway/Render/Fly.io/a VPS/your own always-on machine), not GitHub
Actions.
"""

import os
import time
import threading
import requests
from collections import defaultdict

NANSEN_API_KEY = "nsn_35caecb986718f9390332d1f7e210a37"
TELEGRAM_BOT_TOKEN = "8441036330:AAHHfIrOs5iMByph9J9wqQqVG0HZ4fd7dVw"
TELEGRAM_CHAT_ID = "1215969084"
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")  # optional

NANSEN_BASE = "https://api.nansen.ai"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
CHAINS = ["solana", "ethereum", "base", "bnb", "robinhood"]
MCAP_MIN = 20_000
MCAP_MAX = 100_000
MIN_WALLET_OVERLAP = 2
SCAN_INTERVAL_MINUTES = 15

HEADERS = {"apiKey": NANSEN_API_KEY, "Content-Type": "application/json"}


# ---------- Nansen calls (identical logic to scan.py) ----------

def nansen_post(path, body, retries=3):
    url = f"{NANSEN_BASE}{path}"
    for attempt in range(retries):
        r = requests.post(url, headers=HEADERS, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def get_smart_money_dex_trades():
    body = {
        "chains": CHAINS,
        "filters": {"token_bought_market_cap": {"min": MCAP_MIN, "max": MCAP_MAX}},
        "order_by": [{"field": "trade_value_usd", "direction": "DESC"}],
        "pagination": {"page": 1, "per_page": 500},
    }
    return nansen_post("/api/v1/smart-money/dex-trades", body).get("data", [])


def get_trending_screener():
    body = {
        "chains": CHAINS,
        "timeframe": "24h",
        "filters": {
            "market_cap_usd": {"min": MCAP_MIN, "max": MCAP_MAX},
            "trader_type": "sm",
        },
        "order_by": [{"field": "netflow", "direction": "DESC"}],
        "pagination": {"page": 1, "per_page": 100},
    }
    return nansen_post("/api/v1/token-screener", body).get("data", [])


def build_repeat_winner_candidates(trades):
    wallet_tokens = defaultdict(set)
    token_buyers = defaultdict(set)
    token_meta = {}

    for t in trades:
        wallet = t.get("trader_address")
        token_addr = t.get("token_bought_address")
        if not wallet or not token_addr:
            continue
        wallet_tokens[wallet].add(token_addr)
        token_buyers[token_addr].add(wallet)
        token_meta[token_addr] = {
            "symbol": t.get("token_bought_symbol"),
            "chain": t.get("chain"),
            "market_cap": t.get("token_bought_market_cap"),
            "label_examples": token_meta.get(token_addr, {}).get("label_examples", set())
            | {t.get("trader_address_label") or "unlabeled"},
        }

    repeat_wallets = {w for w, toks in wallet_tokens.items() if len(toks) >= 2}

    scored_tokens = {}
    for token_addr, buyers in token_buyers.items():
        overlap = buyers & repeat_wallets
        if len(overlap) >= MIN_WALLET_OVERLAP:
            scored_tokens[token_addr] = {
                **token_meta[token_addr],
                "repeat_wallet_count": len(overlap),
                "total_buyer_count": len(buyers),
            }
    return scored_tokens


def get_twitter_mentions(symbol):
    search_url = f"https://twitter.com/search?q=%24{symbol}&src=typed_query&f=live"
    if not TWITTER_BEARER_TOKEN:
        return None, search_url
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/counts/recent",
            headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
            params={"query": f"${symbol}"},
            timeout=15,
        )
        r.raise_for_status()
        total = sum(b["tweet_count"] for b in r.json().get("data", []))
        return total, search_url
    except Exception:
        return None, search_url


def format_report(candidates):
    if not candidates:
        return None
    lines = ["<b>🔎 Meme Scanner — Smart Money Overlap Alert</b>", ""]
    for addr, info in sorted(candidates.items(), key=lambda kv: -kv[1]["repeat_wallet_count"]):
        mentions, search_url = get_twitter_mentions(info["symbol"])
        mcap = info.get("market_cap")
        mcap_str = f"${mcap:,.0f}" if mcap else "n/a"
        mention_str = f"{mentions} tweets (recent)" if mentions is not None else f'<a href="{search_url}">check manually</a>'
        lines.append(
            f"<b>${info['symbol']}</b> ({info['chain']})\n"
            f"MCap: {mcap_str} | Smart wallets overlapping: {info['repeat_wallet_count']} "
            f"of {info['total_buyer_count']} total SM buyers\n"
            f"Labels seen: {', '.join(list(info['label_examples'])[:4])}\n"
            f"X mentions: {mention_str}\n"
            f"Token: <code>{addr}</code>\n"
        )
    lines.append("⚠️ Not financial advice. Smart-money labels show past activity, not predictions.")
    return "\n".join(lines)


def send_telegram(text, chat_id=None):
    r = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    r.raise_for_status()


# ---------- Core scan, shared by both the scheduler and the command ----------

def run_scan(chat_id=None, silent_when_empty=False):
    try:
        trades = get_smart_money_dex_trades()
        dex_candidates = build_repeat_winner_candidates(trades)

        if not dex_candidates:
            if not silent_when_empty:
                send_telegram("🩶 Scan complete — no repeat-winner overlap found.", chat_id)
            return

        trending_rows = get_trending_screener()
        trending_addrs = {row["token_address"] for row in trending_rows}
        confirmed = {a: i for a, i in dex_candidates.items() if a in trending_addrs}

        report = format_report(confirmed)
        if report:
            send_telegram(report, chat_id)
        elif not silent_when_empty:
            send_telegram(
                f"🩶 Scan complete — {len(dex_candidates)} overlap candidate(s), none confirmed trending.",
                chat_id,
            )
    except requests.HTTPError as e:
        send_telegram(f"⚠️ Scan failed: {e.response.status_code} — check logs.", chat_id)


# ---------- Background scheduled scan ----------

def scheduler_loop():
    while True:
        run_scan()
        time.sleep(SCAN_INTERVAL_MINUTES * 60)


# ---------- Telegram command listener (long polling) ----------

def command_listener_loop():
    offset = None
    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=40,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
        except requests.RequestException:
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # only respond to your chat -- ignore commands from anyone else
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text == "/scantoken":
                send_telegram("⏳ Scanning now...", chat_id)
                run_scan(chat_id=chat_id, silent_when_empty=False)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    command_listener_loop()
