"""Paper test of "rest one leg, take the other": would resting limit orders on Kalshi have filled, and would the hedge
still have been profitable once they did?

Each collector run (every ~15 min):
 1. RESOLVE the previous run's imaginary orders. A resting Kalshi bid at price px counts as filled if Kalshi's public trade
    tape shows a taker selling into that side at <= px after we "placed" it (we'd have been the best bid, so first in line).
    Filled quantity = those trades' size, capped at our order. For each fill, we buy the other leg on Polymarket US at its
    ask in the *current* snapshot (up to ~15 min after the fill, so this includes price movement and bad-timing fills).
    Result goes to data/paper/maker_fills.csv: the profit locked in at that point (payout is >= $1 either way).
 2. PLAN new orders from this snapshot: for every Kalshi/Polymarket US pair on the same quantity (YES on "X > L1" + NO on
    "X > L2", L1 <= L2), rest the Kalshi leg 1c above its best bid and require the plan to clear --min-edge after the
    Polymarket US taker fee. One plan per Kalshi market side, sized to the $50-per-venue budget.
    Saved to data/paper/maker_plans.csv (replaced every run; unfilled plans are treated as cancelled).
Every run appends a line to data/paper/maker_runs.csv (plans, fills, fill rate) so fill rates can be measured.

Kalshi's NBA series are "quadratic" fee type: no fee on resting (maker) orders. Polymarket US doesn't publish its trades,
so only the Kalshi leg can be tested as the resting one.

    python scripts/maker_sim.py --file data/prices/prices_us_2026-11-10.csv.gz     # plan from a saved snapshot only
"""
import argparse
import csv
import glob
import gzip
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import f, fee, groups  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "data" / "paper"
PLANS, FILLS, RUNS = PAPER / "maker_plans.csv", PAPER / "maker_fills.csv", PAPER / "maker_runs.csv"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}
PLAN_FIELDS = ["placed_ts", "game_date", "teams", "market_type", "player", "event_time",
               "rest_id", "rest_norm_side", "rest_raw_side", "rest_line", "rest_px", "rest_bid", "rest_ask",
               "hedge_id", "hedge_norm_side", "hedge_line", "hedge_px_at_plan", "hedge_fee_coef",
               "contracts", "edge_at_plan", "middle"]
FILL_FIELDS = PLAN_FIELDS + ["resolved_ts", "filled", "first_fill_ts", "hedge_px_at_fill", "hedge_status",
                             "cost", "hedge_fee", "locked_pnl", "locked_per_contract"]
RUN_FIELDS = ["ts_utc", "plans_resolved", "plans_filled", "contracts_filled", "locked_pnl", "new_plans"]


def epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def get(url, params):
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=UA), timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"WARN {url}: {e}", file=sys.stderr)
        return None


def plan(rows, ts, min_edge=0.01, budget=50.0, max_spread=0.25, max_plans=80):
    """Resting-Kalshi / taking-Polymarket-US plans from one snapshot. Wider books are allowed than for pure taking,
    because resting inside a wide book is the whole point."""
    best = {}
    for key, rs in groups(rows, ts, max_spread).items():
        ks = [r for r in rs if r["venue"] == "kalshi"]
        ps = [r for r in rs if r["venue"] == "polymarket_us"]
        for k in ks:
            for p in ps:
                for rest_side, a, b in (("yes", k, p), ("no", p, k)):
                    # hedge = YES on a (line L1) + NO on b (line L2), needs L1 <= L2; Kalshi leg is k
                    if f(a["line"]) > f(b["line"]):
                        continue
                    bid, ask = f(k[f"{rest_side}_bid"]), f(k[f"{rest_side}_ask"])
                    if bid is None or ask is None:
                        continue
                    px = round(bid + 0.01, 2)
                    if not (0 < px < ask):
                        continue  # no room inside the spread
                    hedge_side = "no" if rest_side == "yes" else "yes"
                    hp = f(p[f"{hedge_side}_ask"])
                    c = math.floor(min(budget / px, budget / hp))
                    if c < 1:
                        continue
                    hfee = fee("polymarket_us", f(p["fee_coef"]), hp, c)
                    edge = 1 - px - hp - hfee / c
                    if edge < min_edge:
                        continue
                    raw = rest_side if not int(k.get("flipped") or 0) else ("no" if rest_side == "yes" else "yes")
                    o = dict(placed_ts=ts, game_date=key[0], teams=key[1], market_type=key[2], player=k["player"] or p["player"],
                             event_time=min(k["event_time"], p["event_time"]),
                             rest_id=k["market_id"], rest_norm_side=rest_side, rest_raw_side=raw, rest_line=f(k["line"]),
                             rest_px=px, rest_bid=bid, rest_ask=ask,
                             hedge_id=p["market_id"], hedge_norm_side=hedge_side, hedge_line=f(p["line"]),
                             hedge_px_at_plan=hp, hedge_fee_coef=f(p["fee_coef"]),
                             contracts=c, edge_at_plan=round(edge, 4), middle=f(a["line"]) < f(b["line"]))
                    slot = (k["market_id"], rest_side)
                    if slot not in best or o["edge_at_plan"] > best[slot]["edge_at_plan"]:
                        best[slot] = o
    return sorted(best.values(), key=lambda o: -o["edge_at_plan"])[:max_plans]


def fills_since(ticker, raw_side, px, since_ts, until_ts):
    """Contracts and first time that takers sold into a resting `raw_side` bid at <= px on Kalshi."""
    qty, first, cursor = 0.0, None, None
    for _ in range(5):  # a few pages is plenty for a 15-minute window
        params = {"ticker": ticker, "min_ts": since_ts, "max_ts": until_ts, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets/trades", params)
        if not d:
            break
        for t in d.get("trades", []):
            # a taker buying the opposite side matches resting bids on our side
            taker = t.get("taker_side")
            price = float(t["yes_price_dollars"] if raw_side == "yes" else t["no_price_dollars"])
            if taker != raw_side and price <= px + 1e-9:
                qty += float(t.get("count_fp") or 0)
                ts = t["created_time"]
                first = ts if first is None or ts < first else first
        cursor = d.get("cursor")
        if not cursor:
            break
    return qty, first


def resolve(prev, rows, ts):
    now = {r["market_id"]: r for r in rows if r["venue"] == "polymarket_us"}
    out = []
    for o in prev:
        until = min(epoch(ts), epoch(o["event_time"])) if o.get("event_time") else epoch(ts)
        qty, first = fills_since(o["rest_id"], o["rest_raw_side"], float(o["rest_px"]), epoch(o["placed_ts"]), until)
        filled = min(qty, float(o["contracts"]))
        r = dict(o, resolved_ts=ts, filled=round(filled, 2), first_fill_ts=first or "")
        if filled <= 0:
            r.update(hedge_px_at_fill="", hedge_status="not filled", cost="", hedge_fee=0, locked_pnl=0, locked_per_contract="")
        else:
            h = now.get(o["hedge_id"])
            hp = f(h[f"{o['hedge_norm_side']}_ask"]) if h else None
            if hp is None or not (0 < hp < 1):
                # couldn't hedge (market gone / game started): one-sided exposure, count the Kalshi leg as at risk
                r.update(hedge_px_at_fill="", hedge_status="UNHEDGED", cost=o["rest_px"], hedge_fee=0,
                         locked_pnl=round(-float(o["rest_px"]) * filled, 2), locked_per_contract=-float(o["rest_px"]))
            else:
                c = max(1, math.ceil(filled))
                hfee = fee("polymarket_us", float(o["hedge_fee_coef"]), hp, c)
                cost = float(o["rest_px"]) + hp
                pnl = (1 - cost) * filled - hfee
                r.update(hedge_px_at_fill=hp, hedge_status="hedged", cost=round(cost, 4), hedge_fee=hfee,
                         locked_pnl=round(pnl, 2), locked_per_contract=round(pnl / filled, 4))
        out.append(r)
    return out


def _write(path, fields, rows, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = mode == "w" or not path.exists()
    with path.open(mode, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def step(rows, ts):
    """Called by collect_prices.py once per run."""
    prev = list(csv.DictReader(PLANS.open())) if PLANS.exists() else []
    res = resolve(prev, rows, ts) if prev else []
    filled = [r for r in res if float(r["filled"]) > 0]
    if filled:
        _write(FILLS, FILL_FIELDS, filled, "a")
    new = plan(rows, ts)
    _write(PLANS, PLAN_FIELDS, new, "w")
    locked = sum(float(r["locked_pnl"]) for r in filled)
    _write(RUNS, RUN_FIELDS, [dict(ts_utc=ts, plans_resolved=len(res), plans_filled=len(filled),
                                   contracts_filled=round(sum(float(r["filled"]) for r in filled), 2),
                                   locked_pnl=round(locked, 2), new_plans=len(new))], "a")
    print(f"resting-order test: {len(filled)}/{len(res)} previous plans filled, locked ${locked:+.2f}; {len(new)} new plans")


def summary():
    if not FILLS.exists():
        print("no fills yet")
        return
    fl = list(csv.DictReader(FILLS.open()))
    runs = list(csv.DictReader(RUNS.open())) if RUNS.exists() else []
    plans = sum(int(r["plans_resolved"]) for r in runs)
    hedged = [r for r in fl if r["hedge_status"] == "hedged"]
    pnl = sum(float(r["locked_pnl"]) for r in fl)
    print(f"{len(fl)} fills from {plans} plans ({len(fl) / max(plans, 1):.0%} fill rate); "
          f"{len(hedged)} hedged, {len(fl) - len(hedged)} unhedged; locked P&L ${pnl:+.2f}")
    if hedged:
        slip = [float(r["hedge_px_at_fill"]) - float(r["hedge_px_at_plan"]) for r in hedged]
        print(f"avg hedge price move between plan and fill: {sum(slip) / len(slip):+.3f}; "
              f"profitable after fill: {sum(float(r['locked_pnl']) > 0 for r in hedged)}/{len(hedged)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="plan from this snapshot file (no resolve, nothing written)")
    ap.add_argument("--summary", action="store_true", help="summarize fills so far")
    a = ap.parse_args()
    if a.summary:
        return summary()
    path = a.file or sorted(glob.glob(str(ROOT / "data" / "prices" / "prices_us_*.csv.gz")))[-1]
    rows = list(csv.DictReader(gzip.open(path, "rt")))
    ts = max(r["ts_utc"] for r in rows)
    ps = plan([r for r in rows if r["ts_utc"] == ts], ts)
    print(f"{len(ps)} resting-order plans from {path} @ {ts}")
    for o in ps[:10]:
        print(f"  edge {o['edge_at_plan']:+.3f} x{o['contracts']} {o['market_type']:<9}{o['player'] or o['teams']:<20} rest Kalshi "
              f"{o['rest_norm_side'].upper()}>{o['rest_line']} @{o['rest_px']} (book {o['rest_bid']}/{o['rest_ask']}), "
              f"take PM-US {o['hedge_norm_side'].upper()}>{o['hedge_line']} @{o['hedge_px_at_plan']}")


if __name__ == "__main__":
    main()
