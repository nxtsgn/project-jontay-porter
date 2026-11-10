"""Grade logged paper opportunities against real NBA results.

Reads data/paper/opportunities.csv (written by find_arbs.py), looks up final box scores via nba_api for every game
that has finished, and writes data/paper/settled.csv with the profit/loss each hedge would have produced.
Run locally (stats.nba.com is blocked from the cloud sandbox):

    pip install -r requirements.txt
    python scripts/settle.py

Rules modeled (see research/legal-and-rules-review.md):
  * Player did not play: Kalshi refunds that leg's price; Polymarket resolves the prop "No".
  * Props: stat > line. Totals: combined score > line. Moneyline: the Kalshi ticker's team wins.
  * Both legs are priced in Kalshi's frame (yes = proposition true), as find_arbs.py logs them.
P&L is per contract, after the fees logged at detection time. Each unique hedge is settled once (first sighting).
"""
import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import ROOT, STATS, kalshi_key, norm_name, norm_team  # noqa: E402

OPP = ROOT / "data" / "paper" / "opportunities.csv"
OUT = ROOT / "data" / "paper" / "settled.csv"
COL = {"points": "PTS", "rebounds": "REB", "assists": "AST", "threes": "FG3M", "steals": "STL", "blocks": "BLK"}
OUT_FIELDS = ["game_date", "market_type", "game", "player", "line", "combo", "k_leg", "k_price", "p_leg", "p_price",
              "actual", "dnp", "proposition_true", "pnl", "net_edge_at_detection", "expected_edge_at_detection", "dnp_exposed"]


def season_for(date):
    y, m = int(date[:4]), int(date[5:7])
    start = y if m >= 9 else y - 1
    return f"{start}-{str(start + 1)[2:]}"


def fetch(date, kind):
    """All player ('P') or team ('T') game lines for one calendar date; None if the API is unreachable."""
    from nba_api.stats.endpoints import leaguegamelog
    d = f"{date[5:7]}/{date[8:10]}/{date[:4]}"
    for i in range(3):
        try:
            df = leaguegamelog.LeagueGameLog(season=season_for(date), player_or_team_abbreviation=kind,
                                             date_from_nullable=d, date_to_nullable=d, timeout=30).get_data_frames()[0]
            return df
        except Exception as e:  # noqa: BLE001
            print(f"  retry {i + 1} for {date}/{kind}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    return None


def team_of(row_matchup):
    return norm_team(row_matchup.split()[0])


def settle_one(o, players, teams):
    """Return (actual, dnp, proposition_true) or None if the game isn't final / not found."""
    date, gteams, suffix = kalshi_key({"market_id": o["kalshi_id"]})
    t = o["market_type"]
    tg = teams[teams["_team"].isin(gteams)]
    if len(tg) != 2:
        return None  # game not finished (or not on this date)
    if t == "moneyline":
        row = tg[tg["_team"] == norm_team(suffix)]
        return ("W" if row.iloc[0]["WL"] == "W" else "L"), False, row.iloc[0]["WL"] == "W"
    if t == "totals":
        total = int(tg["PTS"].sum())
        return total, False, total > float(o["line"])
    pg = players[(players["_team"].isin(gteams)) & (players["_name"] == norm_name(o["player"]))]
    if pg.empty or float(pg.iloc[0]["MIN"] or 0) == 0:
        return None, True, None
    val = float(pg.iloc[0][COL[t]])
    return val, False, val > float(o["line"])


def pnl(o, true, dnp):
    kp, pp = float(o["k_price"]), float(o["p_price"])
    fees = float(o["kalshi_fee"]) + float(o["poly_fee"])
    if dnp:
        k_pay = kp  # Kalshi refunds the leg ("fair price" approximated as the entry price)
        p_pay = 1.0 if o["p_leg"] == "no" else 0.0  # Polymarket resolves the prop "No"
    else:
        k_pay = 1.0 if (o["k_leg"] == "yes") == true else 0.0
        p_pay = 1.0 if (o["p_leg"] == "yes") == true else 0.0
    return k_pay + p_pay - kp - pp - fees


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--opps", default=str(OPP))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    if not Path(a.opps).exists():
        sys.exit(f"no opportunities log at {a.opps}; run find_arbs.py first")
    seen, opps = set(), []
    for o in csv.DictReader(open(a.opps)):
        key = (o["kalshi_id"], o["poly_id"], o["combo"])
        if key not in seen:  # same hedge logged at many snapshots counts once
            seen.add(key)
            opps.append(o)
    done = set()
    if Path(a.out).exists():
        done = {(r["game"], r["player"], r["line"], r["combo"], r["market_type"]) for r in csv.DictReader(open(a.out))}
    by_date = {}
    for o in opps:
        date = kalshi_key({"market_id": o["kalshi_id"]})[0]
        by_date.setdefault(date, []).append(o)
    rows = []
    for date, lst in sorted(by_date.items()):
        players, teams = fetch(date, "P"), fetch(date, "T")
        if players is None or teams is None or teams.empty:
            print(f"{date}: no final results available yet ({len(lst)} opportunities pending)")
            continue
        players["_team"] = players["TEAM_ABBREVIATION"].map(norm_team)
        players["_name"] = players["PLAYER_NAME"].map(norm_name)
        teams["_team"] = teams["TEAM_ABBREVIATION"].map(norm_team)
        for o in lst:
            if (o["game"], o["player"], o["line"], o["combo"], o["market_type"]) in done:
                continue
            res = settle_one(o, players, teams)
            if res is None:
                continue
            actual, dnp, true = res
            rows.append(dict(game_date=date, market_type=o["market_type"], game=o["game"], player=o["player"],
                             line=o["line"], combo=o["combo"], k_leg=o["k_leg"], k_price=o["k_price"], p_leg=o["p_leg"],
                             p_price=o["p_price"], actual=actual, dnp=dnp, proposition_true=true, pnl=round(pnl(o, true, dnp), 4),
                             net_edge_at_detection=o["net_edge"], expected_edge_at_detection=o["expected_edge"],
                             dnp_exposed=o["dnp_exposed"]))
    if rows:
        new = not Path(a.out).exists()
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=OUT_FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
    allrows = list(csv.DictReader(open(a.out))) if Path(a.out).exists() else []
    summarize(allrows)


def summarize(rows):
    if not rows:
        print("nothing settled yet")
        return
    pn = [float(r["pnl"]) for r in rows]
    wins = sum(1 for x in pn if x > 0)
    pred = sum(float(r["expected_edge_at_detection"]) for r in rows)
    print(f"\nSettled {len(rows)} paper hedges: total P&L {sum(pn):+.3f} per-contract units, avg {sum(pn) / len(pn):+.4f}, "
          f"{wins} winners / {len(pn) - wins} losers/flat; predicted avg edge {pred / len(rows):+.4f}")
    for flag in ("False", "True"):
        sub = [float(r["pnl"]) for r in rows if r["dnp_exposed"] == flag]
        if sub:
            print(f"  DNP-exposed={flag}: n={len(sub)}, avg {sum(sub) / len(sub):+.4f}")


if __name__ == "__main__":
    main()
