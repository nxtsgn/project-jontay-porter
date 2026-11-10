"""Read-only snapshot of prices on the two venues the user can trade from Arizona: Kalshi and Polymarket US.

Sports: NBA, NFL, college football (Power 4 + Notre Dame), men's college basketball (Power 4 + Notre Dame + Big East + WCC + Gonzaga),\nwomen's college basketball (same minus Gonzaga); see teams.py.
A college game is kept when at least one team is in those conferences.
No accounts, API keys or trading: only public market-data endpoints are called.
  Kalshi:         https://api.elections.kalshi.com/trade-api/v2
  Polymarket US:  https://gateway.polymarket.us   (public gateway; NOT the international polymarket.com)

Every market is normalized to one question with a YES side, so the two venues can be compared directly:
  props   YES = player's stat > line    (NBA: points, rebounds, assists, threes, steals, blocks, PRA;
                                         NFL: passing/rushing/receiving yards, receptions, touchdowns, passing TDs/attempts)
  totals  YES = combined score > line
  margin  YES = (canonical team's score - opponent's score) > line. Moneylines (line 0) and every spread share this axis.
          Canonical team: alphabetically first team (pros); the conference team, alphabetically first if both (college).

Appends one row per market per run to data/prices/prices_us_<YYYY-MM-DD>.csv.gz (UTC date), then runs the gap detector
(find_arbs.py, sized with live depth and logged to data/paper/opportunities.csv) and the resting-order test (maker_sim.py).

    python scripts/collect_prices.py                     # all sports
    python scripts/collect_prices.py --sports nba,nfl --dry-run
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
import teams as T  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "prices"
UA = {"User-Agent": "project-jontay-porter/0.3 (read-only research)"}

KALSHI_FEE = 0.07  # taker coefficient, fee rounds up to the cent per order
PMUS_FEE_DEFAULT = 0.095  # Polymarket US base-tier taker coefficient since 2026-11-06; markets report their own
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

# Per sport: Kalshi series (game / spread / total / props) and Polymarket US league code + prop slug codes.
SPORTS = {
    "nba": dict(college=False, pm="nba", game="KXNBAGAME", spread="KXNBASPREAD", total="KXNBATOTAL",
                props={"KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists", "KXNBA3PT": "threes",
                       "KXNBASTL": "steals", "KXNBABLK": "blocks", "KXNBAPRA": "pra"},
                pm_props={"pts": "points", "reb": "rebounds", "ast": "assists", "3pm": "threes", "stl": "steals",
                          "blk": "blocks", "pra": "pra"}),
    "nfl": dict(college=False, pm="nfl", game="KXNFLGAME", spread="KXNFLSPREAD", total="KXNFLTOTAL",
                props={"KXNFLPASSYDS": "pass_yds", "KXNFLRSHYDS": "rush_yds", "KXNFLRECYDS": "rec_yds",
                       "KXNFLREC": "receptions", "KXNFLTD": "touchdowns", "KXNFLPASSTDS": "pass_tds",
                       "KXNFLPASSATT": "pass_att"},
                pm_props={"pyd": "pass_yds", "ryd": "rush_yds", "recyd": "rec_yds", "rec": "receptions",
                          "td": "touchdowns", "ptd": "pass_tds", "patt": "pass_att"}),
    "cfb": dict(college=True, conf=T.FOOTBALL_SET, pm="cfb", game="KXNCAAFGAME", spread="KXNCAAFSPREAD",
                total="KXNCAAFTOTAL", props={}, pm_props={}),
    "cbb": dict(college=True, conf=T.MENS_BASKETBALL_SET, pm="cbb", game="KXNCAAMBGAME", spread="KXNCAAMBSPREAD",
                total="KXNCAAMBTOTAL", props={}, pm_props={}),
    "wcbb": dict(college=True, conf=T.BASKETBALL_SET, pm="wcbb", game="KXNCAAWBGAME", spread="KXNCAAWBSPREAD",
                 total="KXNCAAWBTOTAL", props={}, pm_props={}),
}
# Pro team codes normalized to Kalshi's (lowercase). Extend if the summary reports games on only one venue.
PRO_ALIAS = {"nba": {"ny": "nyk", "sa": "sas", "gs": "gsw", "no": "nop", "pho": "phx", "bk": "bkn", "uth": "uta", "wsh": "was"},
             "nfl": {"jax": "jac", "lar": "la", "wsh": "was", "lvr": "lv"}}

FIELDS = ["ts_utc", "venue", "sport", "market_id", "game_date", "teams", "market_type", "player", "line",
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


def flip(r):
    """Turn a row into its complement: YES <-> NO."""
    r["yes_bid"], r["yes_ask"], r["no_bid"], r["no_ask"] = r["no_bid"], r["no_ask"], r["yes_bid"], r["yes_ask"]
    r["flipped"] = 1 - int(r.get("flipped") or 0)
    return r


def set_margin(r, canon, subject, wins_by_more_than):
    """Express 'subject wins by more than X' on the canonical team's margin axis."""
    if subject == canon:
        r["line"] = wins_by_more_than
        return r
    # opponent wins by > X  <=>  canonical margin < -X  <=>  NOT(canonical margin > -X)   (lines are .5 or 0: no pushes)
    r["line"] = -wins_by_more_than
    return flip(r)


class Game:
    """Identity of one game on one venue: date, the two team keys, and the canonical team (None = filtered out)."""

    def __init__(self, sport, date, keys):
        cfg = SPORTS[sport]
        self.sport, self.date, self.keys = sport, date, keys
        if cfg["college"]:
            inconf = sorted(k for k in keys if k in cfg["conf"])
            self.canon = inconf[0] if inconf else None
            self.teams = self.canon  # opponents' names differ across venues; the conference team identifies the game
        else:
            self.canon = sorted(keys)[0]
            self.teams = "|".join(sorted(keys))


def team_key(sport, code=None, name=None):
    if SPORTS[sport]["college"]:
        return T.norm(name)
    c = (code or "").lower()
    return PRO_ALIAS.get(sport, {}).get(c, c)


# ---------------------------------------------------------------- Kalshi
def kalshi_markets(series):
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets", params)
        if not d:
            break
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)
    return out


def kalshi_rows(sport, ts):
    cfg = SPORTS[sport]
    games, mk = {}, kalshi_markets(cfg["game"])
    # game markets name both teams: KXNCAAFGAME-26NOV21LSUTENN-TENN (yes_sub_title "Tennessee")
    by_event = {}
    for m in mk:
        seg, code = m["ticker"].split("-")[1], m["ticker"].split("-")[-1]
        by_event.setdefault(seg, {})[code] = m.get("yes_sub_title") or ""
    for seg, codes in by_event.items():
        d = re.match(r"(\d\d)([A-Z]{3})(\d\d)", seg)
        if len(codes) != 2 or not d:
            continue
        date = f"20{d[1]}-{MONTHS[d[2]]:02d}-{d[3]}"
        keys = {c: team_key(sport, c, n) for c, n in codes.items()}
        g = Game(sport, date, set(keys.values()))
        if g.canon:
            games[seg] = (g, keys)

    def base(m, g, mtype):
        return {"venue": "kalshi", "sport": sport, "market_id": m["ticker"], "game_date": g.date, "teams": g.teams,
                "market_type": mtype, "player": "", "line": num(m.get("floor_strike")),
                "yes_bid": num(m.get("yes_bid_dollars")), "yes_ask": num(m.get("yes_ask_dollars")),
                "no_bid": num(m.get("no_bid_dollars")), "no_ask": num(m.get("no_ask_dollars")),
                "volume": num(m.get("volume_fp")), "fee_coef": KALSHI_FEE, "event_time": m.get("occurrence_datetime"),
                "title": m.get("yes_sub_title") or m.get("title") or "", "flipped": 0, "ts_utc": ts}

    rows = []
    for m in mk:
        seg, code = m["ticker"].split("-")[1], m["ticker"].split("-")[-1]
        if seg in games:
            g, keys = games[seg]
            rows.append(set_margin(base(m, g, "margin"), g.canon, keys[code], 0.0))
    for series, mtype in [(cfg["spread"], "margin"), (cfg["total"], "totals")] + list(cfg["props"].items()):
        for m in kalshi_markets(series):
            parts = m["ticker"].split("-")
            if len(parts) < 3 or parts[1] not in games:
                continue
            g, keys = games[parts[1]]
            r = base(m, g, mtype)
            if series == cfg["spread"]:
                code = re.match(r"[A-Z]+", parts[-1])
                if not code or code[0] not in keys or r["line"] is None:
                    continue
                r = set_margin(r, g.canon, keys[code[0]], r["line"])
            elif mtype not in ("totals",):
                r["player"] = r["title"].split(":")[0].strip()
            rows.append(r)
    return rows


# ---------------------------------------------------------------- Polymarket US
def pmus_all(now):
    """Open sports markets that end within 3 weeks (game markets end ~2 weeks after the game; excludes season futures)."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    out, off = [], 0
    while True:
        d = get(f"{PMUS}/v1/markets", {"closed": "false", "categories": "sports", "endDateMin": now.strftime(fmt),
                                       "endDateMax": (now + timedelta(days=21)).strftime(fmt), "limit": 500, "offset": off})
        ms = (d or {}).get("markets", [])
        out += ms
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


SLUG = re.compile(r"([a-z]+)-([a-z]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})(?:-(.*))?$")


def pmus_rows(sport, ts, markets):
    cfg = SPORTS[sport]
    mine = []
    for m in markets:
        s = SLUG.match(m.get("slug", ""))
        if s and s[2] == cfg["pm"]:
            mine.append((m, s))
    # team names per slug code, from the moneyline markets' team objects (college needs names; pros use codes)
    names = {}
    for m, s in mine:
        if s[1] == "aec" and not s[6]:
            for code, side in zip((s[3], s[4]), m.get("marketSides") or []):
                names[code] = (side.get("team") or {}).get("name") or side.get("description") or code
    if cfg["college"]:
        # moneylines listed before the listing window: fetch them by slug once per game for the team names
        missing = {(s[3], s[4], s[5]) for m, s in mine if s[3] not in names or s[4] not in names}
        for a, h, date in missing:
            d = get(f"{PMUS}/v1/market/slug/aec-{cfg['pm']}-{a}-{h}-{date}", retries=1) or {}
            for code, side in zip((a, h), (d.get("market") or d).get("marketSides") or []):
                names[code] = (side.get("team") or {}).get("name") or code
    rows = []
    for m, s in mine:
        kind, a, h, date, rest = s[1], s[3], s[4], s[5], s[6] or ""
        if cfg["college"] and (a not in names or h not in names):
            continue
        ka, kh = team_key(sport, a, names.get(a)), team_key(sport, h, names.get(h))
        g = Game(sport, date, {ka, kh})
        if not g.canon:
            continue
        quotes = pmus_quotes(m)
        if not quotes:
            continue
        q = m.get("question", "")
        r = {"ts_utc": ts, "venue": "polymarket_us", "sport": sport, "market_id": m["slug"], "game_date": date,
             "teams": g.teams, "market_type": "", "player": "", "line": None,
             "yes_bid": quotes[0], "yes_ask": quotes[1], "no_bid": quotes[2], "no_ask": quotes[3],
             "volume": "", "fee_coef": num(m.get("feeCoefficient")) or PMUS_FEE_DEFAULT,
             "event_time": m.get("gameStartTime"), "title": q, "flipped": 0}
        if kind == "astatc" and cfg["pm_props"]:  # ...-pts-jaybro-gte10  ("at least 10" == "> 9.5")
            p = re.match(r"([a-z0-9]+)-[a-z0-9]+-gte(\d+)$", rest)
            name = re.match(r"Will (.+?) (?:record|throw|score) ", q)
            if not p or p[1] not in cfg["pm_props"] or not name:
                continue
            r.update(market_type=cfg["pm_props"][p[1]], player=name[1], line=int(p[2]) - 0.5)
        elif kind == "tsc":  # full-game total only: ...-216pt5
            t = re.match(r"(\d+)pt(\d)$", rest)
            if not t:
                continue
            r.update(market_type="totals", line=float(f"{t[1]}.{t[2]}"))
        elif kind == "aec" and rest == "":  # moneyline; side 0 = first team in the slug
            r["market_type"] = "margin"
            r = set_margin(r, g.canon, ka, 0.0)
        elif kind == "asc":  # full-game spread: ...-neg-5pt5 / ...-pos-1pt5; subject = first team in the slug
            t = re.match(r"(neg|pos)-(\d+)pt(\d)$", rest)
            if not t:
                continue
            x = float(f"{t[2]}.{t[3]}")
            r["market_type"] = "margin"
            if t[1] == "neg":  # subject -X: subject wins by > X
                r = set_margin(r, g.canon, ka, x)
            else:  # subject +X covers  <=>  NOT(opponent wins by > X)
                r = flip(set_margin(r, g.canon, kh, x))
        else:
            continue
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default=",".join(SPORTS), help="comma list from: " + ",".join(SPORTS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--horizon-hours", type=float, default=48, help="keep markets whose game starts within this many hours")
    ap.add_argument("--no-detect", action="store_true", help="skip the gap detector / depth / opportunity log")
    a = ap.parse_args()
    sports = [s for s in a.sports.split(",") if s in SPORTS]
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    pm_all = pmus_all(now)
    rows = []
    for sp in sports:
        rows += kalshi_rows(sp, ts) + pmus_rows(sp, ts, pm_all)
    cutoff = now + timedelta(hours=a.horizon_hours)

    def soon(r):
        try:
            return datetime.fromisoformat(re.sub(r"\.\d+", "", r["event_time"]).replace("Z", "+00:00")) <= cutoff
        except Exception:  # noqa: BLE001
            return False
    rows = [r for r in rows if soon(r)]

    for sp in sports:
        sr = [r for r in rows if r["sport"] == sp]
        g = {v: {(r["game_date"], r["teams"]) for r in sr if r["venue"] == v} for v in ("kalshi", "polymarket_us")}
        both = g["kalshi"] & g["polymarket_us"]
        types = sorted({r["market_type"] for r in sr})
        print(f"{sp:<5} rows K {sum(r['venue'] == 'kalshi' for r in sr):>5} / PM-US {sum(r['venue'] == 'polymarket_us' for r in sr):>5}"
              f" | games both {len(both)}, only K {len(g['kalshi'] - both)}, only PM-US {len(g['polymarket_us'] - both)} | {','.join(types)}")
        if g["kalshi"] - both or g["polymarket_us"] - both:
            print(f"      only K: {sorted(g['kalshi'] - both)[:6]}  only PM-US: {sorted(g['polymarket_us'] - both)[:6]}")
    print(f"total rows: {len(rows)}")
    if a.dry_run or not rows:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"prices_us_{now:%Y-%m-%d}.csv.gz"  # Kalshi + Polymarket US (older prices_<date> files are Kalshi + international)
    n = 1
    while path.exists() and gzip.open(path, "rt").readline().strip() != ",".join(FIELDS):
        n += 1  # columns changed since this file was started: begin a new file rather than mixing layouts
        path = OUT_DIR / f"prices_us_{now:%Y-%m-%d}_{n}.csv.gz"
    new = not path.exists()
    with gzip.open(path, "at", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"appended to {path}")
    if not a.no_detect:
        import find_arbs
        import maker_sim
        find_arbs.detect_and_log(rows, ts, fetch_depth=True)
        maker_sim.step(rows, ts)  # paper test of resting Kalshi orders hedged on Polymarket US


if __name__ == "__main__":
    main()
