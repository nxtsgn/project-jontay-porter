"""Grade logged paper opportunities with each venue's own settlement (no box scores needed).

Reads data/paper/opportunities.csv (written by collect_prices.py -> find_arbs.detect_and_log), counts each unique hedge once
(its first sighting), and once both markets have settled writes the result to data/paper/graded.csv:
    payout = YES-leg settlement + (1 - NO-leg settlement), each from the venue (1, 0, or a "fair price" on a DNP/void)
    pnl    = (payout - cost) x contracts - fees
Runs inside the collect-prices workflow; safe to rerun (already-graded hedges are skipped).

    python scripts/grade.py
"""
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPP = ROOT / "data" / "paper" / "opportunities.csv"
OUT = ROOT / "data" / "paper" / "graded.csv"
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}
FIELDS = ["first_seen", "sport", "game_date", "teams", "market_type", "player", "a_venue", "a_id", "a_line", "a_price", "a_settle",
          "b_venue", "b_id", "b_line", "b_price", "b_settle", "contracts", "cost", "fees", "net_total_expected", "payout", "pnl"]


def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == 2:
                print(f"WARN {url[:120]}: {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    return None


def venue_yes_settlement(venue, market_id):
    """The venue's own YES payout per contract (0..1), or None if not settled yet."""
    if venue == "kalshi":
        m = (get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{market_id}") or {}).get("market") or {}
        if m.get("status") not in ("settled", "finalized") and m.get("result") in (None, ""):
            return None
        if m.get("result") == "yes":
            return 1.0
        if m.get("result") == "no":
            return 0.0
        v = m.get("settlement_value_dollars")
        return float(v) if v not in (None, "") else None  # "scalar" = fair-price settlement (e.g. DNP)
    d = get(f"https://gateway.polymarket.us/v1/markets/{market_id}/settlement") or {}
    s = d.get("settlement")
    return None if s is None else float(s)


def main():
    if not OPP.exists():
        print("no opportunities logged yet")
        return
    first = {}
    for o in csv.DictReader(open(OPP)):
        first.setdefault((o["a_id"], o["b_id"]), o)
    done = set()
    if OUT.exists():
        done = {(r["a_id"], r["b_id"]) for r in csv.DictReader(open(OUT))}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache, rows = {}, []
    for key, o in first.items():
        if key in done or (o.get("event_time") or "9999") > cutoff:
            continue  # graded already, or the game may not be over
        for v, mid in ((o["a_venue"], o["a_id"]), (o["b_venue"], o["b_id"])):
            if (v, mid) not in cache:
                cache[(v, mid)] = venue_yes_settlement(v, mid)
        sa, sb = cache[(o["a_venue"], o["a_id"])], cache[(o["b_venue"], o["b_id"])]
        if sa is None or sb is None:
            continue
        # rows store the normalized YES; a flipped row's YES is the venue's NO
        ya = 1 - sa if o["a_flipped"] in ("1", 1) else sa
        yb = 1 - sb if o["b_flipped"] in ("1", 1) else sb
        c, cost, fees = float(o["contracts"]), float(o["cost"]), float(o["fees"])
        payout = ya + (1 - yb)
        rows.append({"first_seen": o["ts_utc"], "sport": o.get("sport", "nba"), **{k: o[k] for k in ("game_date", "teams", "market_type", "player", "a_venue", "a_id",
                     "a_line", "a_price", "b_venue", "b_id", "b_line", "b_price", "contracts", "cost", "fees")},
                     "a_settle": sa, "b_settle": sb, "net_total_expected": o["net_total"],
                     "payout": round(payout, 4), "pnl": round((payout - cost) * c - fees, 2)})
    if rows:
        new = not OUT.exists()
        with OUT.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
    graded = list(csv.DictReader(open(OUT))) if OUT.exists() else []
    pnl = sum(float(r["pnl"]) for r in graded)
    exp = sum(float(r["net_total_expected"]) for r in graded)
    print(f"graded {len(rows)} new; total {len(graded)} paper hedges, expected ${exp:+.2f}, realized ${pnl:+.2f}; "
          f"{len(first) - len(done) - len(rows)} pending")


if __name__ == "__main__":
    main()
