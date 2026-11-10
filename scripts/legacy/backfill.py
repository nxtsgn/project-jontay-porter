"""Rebuild pre-game NBA prices for games that already finished, from both platforms' public price history.

For every finished game since --since it takes one snapshot per market, --hours-before the game starts, and writes it in the
same schema as collect_prices.py plus the final result, so backtest.py can grade it. Read-only, no accounts.

    python scripts/backfill.py --since 2026-10-22
    python scripts/backfill.py --since 2026-11-03 --max-games 3     # quick test
    python scripts/backfill.py --since 2026-10-22 --repair          # refetch only Kalshi markets missing from the file

Honest limits (this is coarser than the live collector):
  * Kalshi: hourly candlesticks give real bid/ask closes, so Kalshi prices are good.
  * Polymarket: price history is a single price per hour (no bid/ask). We assume bid/ask = price -/+ --poly-half-spread.
  * Polymarket volume/liquidity at snapshot time isn't available, so only Kalshi pre-game volume is filtered.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_prices_intl import FIELDS, GAMMA, KALSHI, KALSHI_SERIES, PM_TYPES, UA  # noqa: E402
from find_arbs import STATS, kalshi_key, norm_name, poly_key  # noqa: E402

CLOB = "https://clob.polymarket.com"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backfill"


def get(url, params=None, retries=5):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                print(f"WARN {url[:120]}: {e}", file=sys.stderr)
                return None
            time.sleep(min(30, 3 * 2 ** i))  # back off through brief DNS/network outages


def epoch(s):
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")  # py3.9 can't parse odd fractional-second widths
    return int(datetime.fromisoformat(s).timestamp())


def poly_games(since, max_games):
    """Finished Polymarket NBA game events since `since`, newest first."""
    out, offset = [], 0
    while True:
        d = get(f"{GAMMA}/events", {"tag_id": 745, "closed": "true", "limit": 100, "offset": offset,
                                    "order": "startDate", "ascending": "false"})
        if not d:
            break
        for e in d:
            st = e.get("startTime") or e.get("startDate") or ""
            if e.get("gameId") and e["slug"].startswith("nba-") and "exact-margin" not in e["slug"] and st[:10] >= since:
                out.append(e)
        if (d[-1].get("startDate") or "")[:10] < since or len(d) < 100:
            break
        offset += 100
    if max_games:
        out = out[:max_games]
    return out


def poly_resolution(m):
    try:
        p = [float(x) for x in json.loads(m["outcomePrices"])]
    except Exception:  # noqa: BLE001
        return "void"
    if p and p[0] >= 0.99:
        return "yes"
    if len(p) > 1 and p[1] >= 0.99:
        return "no"
    return "void"


def poly_price_at(token, snap):
    h = get(f"{CLOB}/prices-history", {"market": token, "interval": "max", "fidelity": 60})
    pts = [x for x in (h or {}).get("history", []) if x["t"] <= snap]
    return pts[-1]["p"] if pts else None


def kalshi_settled(since_ts, series):
    out, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 1000, "min_close_ts": since_ts}
        if cursor:
            p["cursor"] = cursor
        d = get(f"{KALSHI}/markets", p)
        if not d:
            break
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def kalshi_book_at(m, series, snap):
    if epoch(m["created_time"]) >= snap:
        return None  # market didn't exist yet at the snapshot time
    d = get(f"{KALSHI}/series/{series}/markets/{m['ticker']}/candlesticks",
            {"start_ts": epoch(m["created_time"]) - 3600, "end_ts": snap, "period_interval": 60})
    if d is None:
        return "failed"
    cs = [c for c in (d or {}).get("candlesticks", []) if c["end_period_ts"] <= snap]
    if not cs:
        return None
    c = cs[-1]
    f = lambda side, k: float(c[side][k])  # noqa: E731
    vol = sum(float(x.get("volume_fp") or 0) for x in cs)
    return f("yes_bid", "close_dollars"), f("yes_ask", "close_dollars"), vol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-22")
    ap.add_argument("--hours-before", type=float, default=1.0)
    ap.add_argument("--poly-half-spread", type=float, default=0.01)
    ap.add_argument("--min-poly-volume", type=float, default=200, help="skip Polymarket markets with less total volume ($)")
    ap.add_argument("--max-games", type=int, default=0)
    ap.add_argument("--repair", action="store_true", help="keep the existing file; fetch only Kalshi markets missing from it")
    a = ap.parse_args()
    path = OUT_DIR / f"pregame_{a.hours_before:g}h_since_{a.since}.csv.gz"
    if a.repair:
        return repair(a, path)

    events = poly_games(a.since, a.max_games)
    print(f"{len(events)} finished Polymarket games since {a.since}")
    pm = {}  # (type, date, teams, player/..., line) -> row, via find_arbs key logic
    for e in events:
        for m in e.get("markets", []):
            t = m.get("sportsMarketType")
            if t not in PM_TYPES or float(m.get("volumeNum") or 0) < a.min_poly_volume:
                continue
            start = (m.get("gameStartTime") or e.get("startTime") or "").replace(" ", "T").replace("+00", "Z")
            if not start:
                continue
            snap = epoch(start) - int(a.hours_before * 3600)
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if not tokens:
                continue
            p = poly_price_at(tokens[0], snap)
            time.sleep(0.05)
            if p is None or not (0 < p < 1):
                continue
            q = m.get("question", "")
            hs = a.poly_half_spread
            pm[m["slug"]] = {
                "snap": snap, "result": poly_resolution(m), "type": t,
                "row": {
                    "ts_utc": datetime.fromtimestamp(snap, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "venue": "polymarket",
                    "market_id": m["slug"], "event_id": e.get("gameId"), "market_type": t, "title": q,
                    "player": q.split(": ")[0] if t in STATS else "", "line": m.get("line"),
                    "yes_bid": round(max(p - hs, 0.01), 4), "yes_ask": round(min(p + hs, 0.99), 4),
                    "no_bid": round(1 - min(p + hs, 0.99), 4), "no_ask": round(1 - max(p - hs, 0.01), 4),
                    "volume": m.get("volumeNum"), "open_interest": "", "liquidity": "", "close_time": m.get("endDate"),
                    "event_time": start, "outcome1": json.loads(m["outcomes"])[0] if m.get("outcomes") else "",
                },
            }
    print(f"{len(pm)} Polymarket markets with a pre-game price")

    games = {poly_key(v["row"])[:2] for v in pm.values() if poly_key(v["row"])}
    snaps = {}
    for v in pm.values():
        k = poly_key(v["row"])
        if k:
            snaps[k[:2]] = v["snap"]

    krows, failed = kalshi_rows(a.since, games, snaps, skip=set())
    write(path, [{**v["row"], "result": v["result"]} for v in pm.values()] + krows)
    print(f"wrote {len(pm)} Polymarket + {len(krows)} Kalshi rows to {path}; {failed} Kalshi lookups failed (rerun with --repair)")


def kalshi_rows(since, games, snaps, skip):
    """Settled Kalshi markets in `games`, priced at each game's snapshot. Returns (rows, failed_lookups)."""
    rows, failed, since_ts = [], 0, epoch(since + "T00:00:00Z")
    for series, t in KALSHI_SERIES.items():
        mk = kalshi_settled(since_ts, series)
        mk = [m for m in mk if (kalshi_key({"market_id": m["ticker"]}) or (0, 0))[:2] in games and m["ticker"] not in skip]
        print(f"  {series}: {len(mk)} settled Kalshi markets to price")
        for m in mk:
            k = kalshi_key({"market_id": m["ticker"]})
            snap = snaps[k[:2]]
            b = kalshi_book_at(m, series, snap)
            time.sleep(0.05)
            if b == "failed":
                failed += 1
                continue
            if b is None:
                continue
            yb, ya, vol = b
            title = m.get("yes_sub_title") or m.get("title") or ""
            rows.append({
                "ts_utc": datetime.fromtimestamp(snap, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "venue": "kalshi",
                "market_id": m["ticker"], "event_id": m["event_ticker"], "market_type": t, "title": title,
                "player": title.split(":")[0].strip() if t in STATS else "", "line": m.get("floor_strike"),
                "yes_bid": yb, "yes_ask": ya, "no_bid": round(1 - ya, 4), "no_ask": round(1 - yb, 4),
                "volume": round(vol, 2), "open_interest": "", "liquidity": "", "close_time": m.get("close_time"),
                "event_time": m.get("occurrence_datetime"), "outcome1": "", "result": m.get("result"),
            })
    return rows, failed


def write(path, rows):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS + ["result"])
        w.writeheader()
        w.writerows(rows)


def repair(a, path):
    rows = list(csv.DictReader(gzip.open(path, "rt")))
    pm_rows = [r for r in rows if r["venue"] == "polymarket"]
    have = {r["market_id"] for r in rows if r["venue"] == "kalshi"}
    games, snaps = set(), {}
    for r in pm_rows:
        k = poly_key(r)
        if k:
            games.add(k[:2])
            snaps[k[:2]] = epoch(r["ts_utc"])
    print(f"repair: {len(games)} games, {len(have)} Kalshi markets already priced")
    new, failed = kalshi_rows(a.since, games, snaps, skip=have)
    write(path, rows + new)
    print(f"added {len(new)} Kalshi rows to {path}; {failed} lookups still failing")


if __name__ == "__main__":
    main()
