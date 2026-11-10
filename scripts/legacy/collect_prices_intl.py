"""Read-only snapshot of NBA market prices on Kalshi and Polymarket.

No accounts, API keys or trading: only public market-data endpoints are called.
Appends one row per market per run to data/prices/prices_<YYYY-MM-DD>.csv.gz (UTC date).
Only markets whose game starts within --horizon-hours are kept (default 36) to keep the data small.

    python scripts/collect_prices.py            # all NBA game + player-prop markets
    python scripts/collect_prices.py --dry-run  # print a summary, write nothing
"""
import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
NBA_TAG_ID = 745  # from GET /sports (sport=nba)

# Kalshi series -> normalized market type (matches Polymarket sportsMarketType names)
KALSHI_SERIES = {
    "KXNBAGAME": "moneyline",
    "KXNBASPREAD": "spreads",
    "KXNBATOTAL": "totals",
    "KXNBAPTS": "points",
    "KXNBAREB": "rebounds",
    "KXNBAAST": "assists",
    "KXNBA3PT": "threes",
    "KXNBASTL": "steals",
    "KXNBABLK": "blocks",
}
PM_TYPES = {"moneyline", "spreads", "totals", "points", "rebounds", "assists", "threes", "steals", "blocks"}

FIELDS = [
    "ts_utc", "venue", "market_id", "event_id", "market_type", "title", "player", "line",
    "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest", "liquidity", "close_time", "event_time", "outcome1",
]
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "prices"
UA = {"User-Agent": "project-jontay-porter/0.1 (read-only research)"}


def get(url, params=None, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - network errors vary; retry then give up
            if i == retries - 1:
                print(f"WARN {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kalshi_rows(ts):
    rows = []
    for series, mtype in KALSHI_SERIES.items():
        cursor = None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = get(f"{KALSHI}/markets", params)
            if not d:
                break
            for m in d.get("markets", []):
                title = m.get("yes_sub_title") or m.get("title") or ""
                player = title.split(":")[0].strip() if mtype in ("points", "rebounds", "assists", "threes", "steals", "blocks") else ""
                rows.append({
                    "ts_utc": ts, "venue": "kalshi", "market_id": m["ticker"], "event_id": m["event_ticker"],
                    "market_type": mtype, "title": title, "player": player, "line": m.get("floor_strike"),
                    "yes_bid": num(m.get("yes_bid_dollars")), "yes_ask": num(m.get("yes_ask_dollars")),
                    "no_bid": num(m.get("no_bid_dollars")), "no_ask": num(m.get("no_ask_dollars")),
                    "volume": num(m.get("volume_fp")), "open_interest": num(m.get("open_interest_fp")),
                    "liquidity": "", "close_time": m.get("close_time"), "event_time": m.get("occurrence_datetime"), "outcome1": "",
                })
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.2)
        time.sleep(0.2)
    return rows


def polymarket_rows(ts):
    rows, offset = [], 0
    while True:
        d = get(f"{GAMMA}/events", {"tag_id": NBA_TAG_ID, "active": "true", "closed": "false", "limit": 100, "offset": offset})
        if not d:
            break
        for e in d:
            for m in e.get("markets", []):
                mtype = m.get("sportsMarketType")
                if mtype not in PM_TYPES:
                    continue
                q = m.get("question", "")
                player = re.split(r":\s", q)[0] if mtype in ("points", "rebounds", "assists", "threes", "steals", "blocks") else ""
                bid, ask = num(m.get("bestBid")), num(m.get("bestAsk"))
                rows.append({
                    "ts_utc": ts, "venue": "polymarket", "market_id": m.get("slug"), "event_id": e.get("gameId") or e.get("slug"),
                    "market_type": mtype, "title": q, "player": player, "line": m.get("line"),
                    # Yes-token book; No side is the complement of the Yes book
                    "yes_bid": bid, "yes_ask": ask,
                    "no_bid": None if ask is None else round(1 - ask, 4), "no_ask": None if bid is None else round(1 - bid, 4),
                    "volume": num(m.get("volumeNum")), "open_interest": "", "liquidity": num(m.get("liquidityNum")),
                    "close_time": m.get("endDate"),
                    "event_time": (m.get("gameStartTime") or e.get("startTime") or "").replace(" ", "T").replace("+00", "Z"),
                    # first outcome is what the "yes" prices refer to (team, Over, or Yes)
                    "outcome1": (json.loads(m["outcomes"])[0] if m.get("outcomes") else ""),
                })
        if len(d) < 100:
            break
        offset += 100
        time.sleep(0.2)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--horizon-hours", type=float, default=36, help="keep markets whose game starts within this many hours")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = kalshi_rows(ts) + polymarket_rows(ts)
    cutoff = now.timestamp() + args.horizon_hours * 3600
    def soon(r):
        try:
            return datetime.fromisoformat((r["event_time"] or r["close_time"]).replace("Z", "+00:00")).timestamp() <= cutoff
        except Exception:  # noqa: BLE001
            return False
    rows = [r for r in rows if soon(r)]
    by = {}
    for r in rows:
        by[(r["venue"], r["market_type"])] = by.get((r["venue"], r["market_type"]), 0) + 1
    for k in sorted(by):
        print(f"{k[0]:<11}{k[1]:<10}{by[k]}")
    print(f"total rows: {len(rows)}")
    if args.dry_run or not rows:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"prices_{now:%Y-%m-%d}.csv.gz"
    new = not path.exists()
    with gzip.open(path, "at", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"appended to {path}")


if __name__ == "__main__":
    main()
