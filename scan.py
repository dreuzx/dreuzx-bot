"""
Meme coin smart-money scanner.

Logic:
1. Pull the last 24h of Smart Money DEX trades (Nansen) for tokens whose
   market cap sits in the $20k-$100k band, across Solana, Ethereum, Base,
   BNB and Robinhood.
2. Build a wallet -> {tokens bought} map. Wallets that bought 2+ distinct
   tokens in that band are treated as "repeat winners" (our proxy for
   "wallets that have successfully entered every popular meme currently
   doing well" -- Nansen doesn't expose a single "hit every meme" metric,
   so this is the closest real signal: overlapping smart-money conviction
   across multiple live candidates).
3. Independently pull the Token Screener (24h) for the same mcap band,
   sorted by netflow, to confirm the token is *currently* trending
   (not just bought once).
4. Keep only tokens that appear in BOTH sets.
5. Optionally check X/Twitter mention volume (requires TWITTER_BEARER_TOKEN;
   if absent, a manual search link is included instead -- there is no free
   way to get reliable mention counts, see README).
6. Push a formatted report to Telegram.

Run manually:  python scan.py
Run in CI:     see .github/workflows/meme-scan.yml
"""

import os
import sys
import time
import requests
from collections import defaultdict

NANSEN_API_KEY = "nsn_35caecb986718f9390332d1f7e210a37"
TELEGRAM_BOT_TOKEN = "8441036330:AAHHfIrOs5iMByph9J9wqQqVG0HZ4fd7dVw"
TELEGRAM_CHAT_ID = "1215969084"
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")  # optional

NANSEN_BASE = "https://api.nansen.ai"
CHAINS = ["solana", "ethereum", "base", "bnb", "robinhood"]
MCAP_MIN = 20_000
MCAP_MAX = 100_000
MIN_WALLET_OVERLAP = 2  # a token needs >=2 distinct "repeat winner" wallets buying it

HEADERS = {"apiKey": NANSEN_API_KEY, "Content-Type": "application/json"}


def nansen_post(path, body, retries=3):
    url = f"{NANSEN_BASE}{path}"
    for attempt in range(retries):
        r = requests.post(url, headers=HEADERS, json=body, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def get_smart_money_dex_trades():
    """Recent smart-money buys of tokens in our market cap band."""
    body = {
        "chains": CHAINS,
        "filters": {
            "token_bought_market_cap": {"min": MCAP_MIN, "max": MCAP_MAX},
        },
        "order_by": [{"field": "trade_value_usd", "direction": "DESC"}],
        "pagination": {"page": 1, "per_page": 500},
    }
    return nansen_post("/api/v1/smart-money/dex-trades", body).get("data", [])


def get_trending_screener():
    """Tokens in the mcap band that are currently trending / netflow positive."""
    all_rows = []
    # token-screener allows max 5 chains per call, which matches our set exactly
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
    all_rows.extend(nansen_post("/api/v1/token-screener", body).get("data", []))
    return all_rows


def build_repeat_winner_candidates(trades):
    """Wallet -> set of token addresses it bought in the band."""
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
            "label_examples": token_meta.get(token_addr, {}).get("label_examples", set()) | {t.get("trader_address_label") or "unlabeled"},
        }

    # "repeat winner" wallets = bought 2+ distinct tokens in this band
    repeat_wallets = {w for w, toks in wallet_tokens.items() if len(toks) >= 2}

    # tokens bought by >= MIN_WALLET_OVERLAP repeat-winner wallets
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
    """Best-effort mention count. Returns (count_or_none, search_url)."""
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
        total = sum(bucket["tweet_count"] for bucket in r.json().get("data", []))
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


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    r.raise_for_status()


def main():
    trades = get_smart_money_dex_trades()
    dex_candidates = build_repeat_winner_candidates(trades)

    if not dex_candidates:
        msg = "No repeat-winner overlap found this run."
        print(msg)
        send_telegram(f"🩶 Meme Scanner ran — {msg}")
        return

    trending_rows = get_trending_screener()
    trending_addrs = {row["token_address"] for row in trending_rows}

    # keep only tokens confirmed as currently trending by the screener too
    confirmed = {
        addr: info for addr, info in dex_candidates.items() if addr in trending_addrs
    }

    report = format_report(confirmed)
    if report:
        send_telegram(report)
        print(f"Sent report with {len(confirmed)} token(s).")
    else:
        msg = f"{len(dex_candidates)} overlap candidate(s) found, none confirmed trending this run."
        print(msg)
        send_telegram(f"🩶 Meme Scanner ran — {msg}")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"API error: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
