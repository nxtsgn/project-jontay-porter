"""Read-only snapshot of NBA prices on the two venues the user can trade from Arizona: Kalshi and Polymarket US.

No accounts, API keys or trading: only public market-data endpoints are called.
  Kalshi:         https://api.elections.kalshi.com/trade-api/v2   (series KXNBA*)
  Polymarket US:  https://gateway.polymarket.us                    (public gateway; NOT the international polymarket.com)

Every market is normalized to one question with a YES side, so the two venues can be compared directly:
  props   (points, rebounds, assists, threes, steals, blocks, pra):  YES = player's stat > line
  totals  YES = combined score > line
  margin  YES = (canonical team's score - other team's score) > line, where canonical = alphabetically first team code.
          Moneylines (line 0) and every spread are expressed on this one axis, so "wins", "-5.5" and "+3.5" all line up.

Appends one row per market per run to data/prices/prices_us_<YYYY-MM-DD>.csv.gz (UTC date). Then runs the gap detector
(find_arbs.py) on the snapshot, fetches order-book depth for any gap it finds, and appends those to
data/paper/opportunities.csv for automatic grading (grade.py).

    python scripts/collect_prices.py              # snapshot + detect + log
    python scripts/collect_prices.py --dry-run    # print a summary, write nothing
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

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "prices"
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}

KALSHI_FEE = 0.07  # taker coefficient, fee rounds up to the cent per order
PMUS_FEE_DEFAULT = 0.095  # Polymarket US base-tier taker coefficient since 2026-11-06; markets report their own

# Kalshi series -> normalized type
KALSHI_SERIES = {
    "KXNBAGAME": "margin", "KXNBASPREAD": "margin", "KXNBATOTAL": "totals",
    "KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists", "KXNBA3PT": "threes",
    "KXNBASTL": "steals", "KXNBABLK": "blocks", "KXNBAPRA": "pra",
}
# Polymarket US prop slug codes -> normalized type
PMUS_STATS = {"pts": "points", "reb": "rebounds", "ast": "assists", "3pm": "threes", "stl": "steals", "blk": "blocks", "pra": "pra"}
PROPS = set(PMUS_STATS.values())

# Team codes normalized to Kalshi's (extend if the summary reports unmatched games)
TEAM_ALIAS = {"ny": "nyk", "sa": "sas", "gs": "gsw", "no": "nop", "pho": "phx", "bk": "bkn", "uth": "uta", "wsh": "was"}
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

FIELDS = ["ts_utc", "venue", "market_id", "game_date", "teams", "market_type", "player", "line",
          "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "fee_coef", "event_time", "title", "flipped"]
# flipped=1: the row's YES is the venue's own NO side (needed to place orders and read settlements correctly)


def get(url, params=None, retries=4):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - network errors vary; retry then give up
            if i == retries - 1:
                print(f"WARN {url[:140]}: {e}", file=sys.stderr)
                return None
            time.sleep(min(30, 3 * 2 ** i))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def team(c):
    c = c.lower()
    return TEAM_ALIAS.get(c, c)


def flip(r):
    """Turn a row into its complement: YES <-> NO."""
    r["yes_bid"], r["yes_ask"], r["no_bid"], r["no_ask"] = r["no_bid"], r["no_ask"], r["yes_bid"], r["yes_ask"]
    r["flipped"] = 1 - int(r.get("flipped") or 0)
    return r


def margin_row(r, teams_pair, subject, wins_by_more_than):
    """Express 'subject wins by more than X' on the canonical margin axis (canonical = sorted first)."""
    canon, other = sorted(teams_pair)
    if subject == canon:
        r["line"] = wins_by_more_than
        return r
    # other wins by > X  <=>  canonical margin < -X  <=>  NOT(canonical margin > -X)   (lines are .5 or 0, no pushes)
    r["line"] = -wins_by_more_than
    return flip(r)


# ---------------------------------------------------------------- Kalshi
def kalshi_parse(ticker):
    """KXNBAPTS-26NOV10BOSPHI-... -> ('2026-11-10', ('bos','phi'), last_segment)."""
    parts = ticker.split("-")
    m = re.match(r"(\d\d)([A-Z]{3})(\d\d)([A-Z]{6})$", parts[1]) if len(parts) > 1 else None
    if not m:
        return None
    return f"20{m[1]}-{MONTHS[m[2]]:02d}-{m[3]}", (team(m[4][:3]), team(m[4][3:])), parts[-1]


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
                p = kalshi_parse(m["ticker"])
                if not p:
                    continue
                date, pair, last = p
                title = m.get("yes_sub_title") or m.get("title") or ""
                r = {"ts_utc": ts, "venue": "kalshi", "market_id": m["ticker"], "game_date": date, "teams": "|".join(sorted(pair)),
                     "market_type": mtype, "player": "", "line": m.get("floor_strike"),
                     "yes_bid": num(m.get("yes_bid_dollars")), "yes_ask": num(m.get("yes_ask_dollars")),
                     "no_bid": num(m.get("no_bid_dollars")), "no_ask": num(m.get("no_ask_dollars")),
                     "volume": num(m.get("volume_fp")), "fee_coef": KALSHI_FEE, "event_time": m.get("occurrence_datetime"),
                     "title": title, "flipped": 0}
                if mtype in PROPS:
                    r["player"] = title.split(":")[0].strip()
                elif mtype == "margin":
                    subject = team(re.match(r"[A-Z]+", last)[0])
                    if subject not in pair:
                        continue
                    r = margin_row(r, pair, subject, 0.0 if series == "KXNBAGAME" else float(m.get("floor_strike") or 0))
                rows.append(r)
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.2)
    return rows


# ---------------------------------------------------------------- Polymarket US
def pmus_markets(listed_since):
    out, off = [], 0
    while True:
        d = get(f"{PMUS}/v1/markets", {"closed": "false", "categories": "sports", "startDateMin": listed_since,
                                       "limit": 500, "offset": off})
        ms = (d or {}).get("markets", [])
        out += [m for m in ms if "-nba-" in m.get("slug", "")]
        if len(ms) < 500:
            break
        off += 500
    return out


def pmus_quotes(m):
    """(yes_bid, yes_ask, no_bid, no_ask) from the listing's long/short quotes (each is that side's best ask)."""
    sides = m.get("marketSides") or []
    if len(sides) != 2:
        return None
    ya = num((sides[0].get("quote") or {}).get("value"))
    na = num((sides[1].get("quote") or {}).get("value"))
    if ya is None or na is None:
        return None
    return round(1 - na, 4), ya, round(1 - ya, 4), na


def pmus_rows(ts, listed_since):
    rows = []
    for m in pmus_markets(listed_since):
        slug, q = m["slug"], m.get("question", "")
        s = re.match(r"([a-z]+)-nba-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})(?:-(.*))?$", slug)
        if not s:
            continue
        kind, a, h, date, rest = s[1], team(s[2]), team(s[3]), s[4], s[5] or ""
        quotes = pmus_quotes(m)
        if not quotes:
            continue
        r = {"ts_utc": ts, "venue": "polymarket_us", "market_id": slug, "game_date": date, "teams": "|".join(sorted((a, h))),
             "market_type": "", "player": "", "line": None,
             "yes_bid": quotes[0], "yes_ask": quotes[1], "no_bid": quotes[2], "no_ask": quotes[3],
             "volume": "", "fee_coef": num(m.get("feeCoefficient")) or PMUS_FEE_DEFAULT,
             "event_time": m.get("gameStartTime"), "title": q, "flipped": 0}
        if kind == "astatc":  # player props: ...-pts-jaybro-gte10  ("at least 10" == "> 9.5")
            p = re.match(r"(pts|reb|ast|3pm|stl|blk|pra)-[a-z0-9]+-gte(\d+)$", rest)
            name = re.match(r"Will (.+?) record at least", q)
            if not p or not name:
                continue
            r.update(market_type=PMUS_STATS[p[1]], player=name[1], line=int(p[2]) - 0.5)
        elif kind == "tsc":  # full-game total only: ...-216pt5
            t = re.match(r"(\d+)pt(\d)$", rest)
            if not t:
                continue
            r.update(market_type="totals", line=float(f"{t[1]}.{t[2]}"))
        elif kind == "aec" and rest == "":  # moneyline; side 0 = first (away) team
            r["market_type"] = "margin"
            r = margin_row(r, (a, h), a, 0.0)
        elif kind == "asc":  # full-game spread: ...-neg-5pt5 / ...-pos-1pt5; subject named in the question
            t = re.match(r"(neg|pos)-(\d+)pt(\d)$", rest)
            subj = re.match(r"Will the ([A-Z]+) ", q)
            if not t or not subj or team(subj[1]) not in (a, h):
                continue
            x = float(f"{t[2]}.{t[3]}")
            r["market_type"] = "margin"
            subject, other = team(subj[1]), (h if team(subj[1]) == a else a)
            if t[1] == "neg":  # subject -X: subject wins by > X
                r = margin_row(r, (a, h), subject, x)
            else:  # subject +X covers  <=>  NOT(other wins by > X)
                r = flip(margin_row(r, (a, h), other, x))
        else:
            continue
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--horizon-hours", type=float, default=36, help="keep markets whose game starts within this many hours")
    ap.add_argument("--no-detect", action="store_true", help="skip the gap detector / depth / opportunity log")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    listed_since = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = kalshi_rows(ts) + pmus_rows(ts, listed_since)
    cutoff = now + timedelta(hours=a.horizon_hours)

    def soon(r):
        try:
            return datetime.fromisoformat(re.sub(r"\.\d+", "", r["event_time"]).replace("Z", "+00:00")) <= cutoff
        except Exception:  # noqa: BLE001
            return False
    rows = [r for r in rows if soon(r)]

    by = {}
    for r in rows:
        by[(r["venue"], r["market_type"])] = by.get((r["venue"], r["market_type"]), 0) + 1
    for k in sorted(by):
        print(f"{k[0]:<14}{k[1]:<10}{by[k]}")
    games = {v: {(r["game_date"], r["teams"]) for r in rows if r["venue"] == v} for v in ("kalshi", "polymarket_us")}
    print(f"total rows: {len(rows)}; games on both venues: {len(games['kalshi'] & games['polymarket_us'])}; "
          f"only Kalshi: {sorted(games['kalshi'] - games['polymarket_us'])}; only Polymarket US: {sorted(games['polymarket_us'] - games['kalshi'])}")
    if a.dry_run or not rows:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"prices_us_{now:%Y-%m-%d}.csv.gz"  # Kalshi + Polymarket US (older prices_<date> files are Kalshi + international)
    new = not path.exists()
    with gzip.open(path, "at", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"appended to {path}")
    if not a.no_detect:
        import find_arbs
        find_arbs.detect_and_log(rows, ts, fetch_depth=True)


if __name__ == "__main__":
    main()
