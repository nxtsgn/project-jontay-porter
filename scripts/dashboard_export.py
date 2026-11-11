"""Export the paper data as small CSV tables for the private claude.ai dashboard (refreshed by Claude on request).
Conservative, same rules as progress_report.py: open unhedged contracts are a total loss until they settle, locked profit
on hedged-but-unsettled contracts is "unrealized" until both markets settle.

    git fetch origin price-data && mkdir -p /tmp/pd && git archive origin/price-data data | tar -x -C /tmp/pd
    python scripts/dashboard_export.py --data /tmp/pd/data --out /tmp/dash
"""
import argparse
import csv
import glob
import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


START = {"kalshi": 50.0, "polymarket_us": 50.0}
RESERVE = 10.0


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _read(d, name):
    p = d / "paper" / name
    return list(csv.DictReader(p.open())) if p.exists() else []


def _write(out, name, rows, fields):
    with (out / name).open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="the data/ folder from the price-data branch")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    d, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stamps = set()
    for p in glob.glob(str(d / "prices" / "prices_us_*.csv.gz")):
        with gzip.open(p, "rt") as fh:
            stamps |= {r["ts_utc"] for r in csv.DictReader(fh) if r.get("sport")}

    outcomes, fills = _read(d, "maker_outcomes.csv"), _read(d, "maker_fills.csv")
    bags = {(b["placed_ts"], b["rest_id"]): _f(b["bag_pnl"]) for b in _read(d, "maker_bags.csv")}
    plans_now = {(p["rest_id"], p["rest_norm_side"], p["hedge_id"]): p for p in _read(d, "maker_plans.csv")}
    orders, runs = _read(d, "trader_orders.csv"), _read(d, "trader_runs.csv")

    chosen = {}
    for r in orders:
        if r["strategy"] == "maker" and r["leg"] == "rest":
            _, rid, side, hid = r["hedge_key"].split("|")
            chosen[(r["ts_utc"], rid, side, hid)] = _f(json.loads(r["payload"])["count"])

    # one row per filled plan, valued conservatively, at full size and at the size the practice trader chose
    tracked = {(o["placed_ts"], o["rest_id"]) for o in outcomes}
    filled = [dict(o, era="tracked") for o in outcomes if _f(o["filled"]) > 0]
    filled += [dict(r, era="before trader") for r in fills if (r["placed_ts"], r["rest_id"]) not in tracked]
    pos = []
    for o in filled:
        c = max(_f(o["contracts"]), 1)
        k = (o["placed_ts"], o["rest_id"], o["rest_norm_side"], o["hedge_id"])
        share = chosen[k] / c if k in chosen else 0.0
        q, locked = _f(o["unhedged_qty"]), _f(o["locked_pnl"])
        bag_key = (o["placed_ts"], o["rest_id"])
        settled_bag = bags.get(bag_key)
        game_over = o.get("event_time", "9999") < now
        open_risk = q * _f(o["rest_px"]) if q > 0 and settled_bag is None else 0.0
        # hedged contracts are locked in, but stay "unrealized" until the game is over and both sides have settled
        realized = (locked if game_over else 0.0) + (settled_bag or 0.0)
        unrealized = 0.0 if game_over else locked
        pos.append(dict(
            fill_time=o.get("first_fill_ts", "")[:19] + "Z" if o.get("first_fill_ts") else o.get("resolved_ts", ""),
            sport=o["sport"], market=o["market_type"], who=o.get("player") or o.get("teams", ""), teams=o.get("teams", ""),
            line=o.get("rest_line", ""), side=o.get("rest_norm_side", ""), game_start=o.get("event_time", ""), rest_price=_f(o["rest_px"]), filled=round(_f(o["filled"]), 2),
            hedged=round(_f(o["hedged_qty"]), 2), unhedged=round(q, 2), status=o["hedge_status"],
            locked=round(locked, 2), open_risk=round(open_risk, 2), realized=round(realized, 2),
            unrealized=round(unrealized, 2), conservative=round(realized + unrealized - open_risk, 2),
            in_rules="yes" if share else "no", rules_conservative=round((realized + unrealized - open_risk) * share, 2),
            state="open" if (open_risk or unrealized) else "settled", era=o["era"]))
    _write(out, "positions.csv", pos, list(pos[0]) if pos else
           ["fill_time", "sport", "market", "who", "teams", "line", "side", "game_start", "rest_price", "filled", "hedged", "unhedged", "status",
            "locked", "open_risk", "realized", "unrealized", "conservative", "in_rules", "rules_conservative", "state", "era"])

    # cumulative conservative P&L by fill time
    cum_full = cum_rules = 0.0
    curve = [dict(time=min(stamps) if stamps else now, full_size=0.0, within_rules=0.0)]
    for p in sorted(pos, key=lambda p: p["fill_time"]):
        cum_full += p["conservative"]
        cum_rules += p["rules_conservative"]
        curve.append(dict(time=p["fill_time"], full_size=round(cum_full, 2), within_rules=round(cum_rules, 2)))
    curve.append(dict(time=now, full_size=round(cum_full, 2), within_rules=round(cum_rules, 2)))
    _write(out, "equity.csv", curve, ["time", "full_size", "within_rules"])

    last = runs[-1] if runs else {}
    in_use = {"kalshi": _f(last.get("deployed_kalshi")), "polymarket_us": _f(last.get("deployed_pmus"))}
    led = _read(d, "trader_ledger.csv")
    taker_real = sum(_f(h["pnl"]) for h in led if h.get("pnl") not in ("", None))
    rules = dict(realized=sum(p["realized"] * (p["in_rules"] == "yes") for p in pos) + taker_real,
                 unrealized=sum(p["unrealized"] * (p["in_rules"] == "yes") for p in pos),
                 open_risk=sum(p["open_risk"] * (p["in_rules"] == "yes") for p in pos))
    full = dict(realized=sum(p["realized"] for p in pos) + taker_real, unrealized=sum(p["unrealized"] for p in pos),
                open_risk=sum(p["open_risk"] for p in pos))
    span_days = ((datetime.strptime(max(stamps)[:19], "%Y-%m-%dT%H:%M:%S") - datetime.strptime(min(stamps)[:19], "%Y-%m-%dT%H:%M:%S"))
                 .total_seconds() / 86400) if stamps else 0
    summary = []
    for view, m in (("within_rules", rules), ("full_size", full)):
        total = m["realized"] + m["unrealized"] - m["open_risk"]
        summary.append(dict(
            view=view, as_of=now, data_from=min(stamps) if stamps else "", data_to=max(stamps) if stamps else "",
            days=round(span_days, 2), snapshots=len(stamps), runs=len(runs), start_bankroll=100.0,
            balance=round(100 + m["realized"], 2), equity=round(100 + total, 2), realized=round(m["realized"], 2),
            unrealized=round(m["unrealized"], 2), open_risk=round(m["open_risk"], 2), conservative_total=round(total, 2),
            return_pct=round(total, 2), plans_checked=len(outcomes), fills=sum(1 for p in pos if (p["in_rules"] == "yes") or view == "full_size"),
            kalshi_in_use=round(in_use["kalshi"], 2), pmus_in_use=round(in_use["polymarket_us"], 2),
            kalshi_free=round(max(0, START["kalshi"] - RESERVE - in_use["kalshi"]), 2),
            pmus_free=round(max(0, START["polymarket_us"] - RESERVE - in_use["polymarket_us"]), 2),
            last_run=last.get("ts_utc", ""), trader_status=last.get("status", ""), mode=last.get("mode", "")))
    _write(out, "summary.csv", summary, list(summary[0]))

    # resting orders the practice trader would have on the book right now (latest run)
    latest = max((r["ts_utc"] for r in orders), default="")
    rest = []
    by_key = defaultdict(dict)
    for r in orders:
        if r["ts_utc"] == latest and r["strategy"] == "maker":
            by_key[r["hedge_key"]][r["leg"]] = json.loads(r["payload"])
    for key, legs in by_key.items():
        _, rid, side, hid = key.split("|")
        p = plans_now.get((rid, side, hid), {})
        rb, hb = legs.get("rest", {}), legs.get("hedge_on_fill", {})
        c = _f(rb.get("count"))
        yes_px = _f(rb.get("price"))
        rest.append(dict(placed=latest, sport=p.get("sport", ""), market=p.get("market_type", ""),
                         who=p.get("player") or p.get("teams", ""), teams=p.get("teams", ""), line=p.get("rest_line", ""),
                         game_start=p.get("event_time", ""), kalshi_market=rid, side=side, price=_f(p.get("rest_px")) or (yes_px if rb.get("side") == "bid" else round(1 - yes_px, 4)),
                         contracts=c, cost=round(c * _f(p.get("rest_px")), 2), hedge_market=hid,
                         hedge_limit=_f(hb.get("price", {}).get("value")), edge_cents=round(_f(p.get("edge_at_plan")) * 100, 2)))
    rest.sort(key=lambda r: -r["edge_cents"])
    _write(out, "resting.csv", rest, ["placed", "sport", "market", "who", "teams", "line", "game_start", "kalshi_market", "side", "price",
                                      "contracts", "cost", "hedge_market", "hedge_limit", "edge_cents"])

    _write(out, "runs.csv", [dict(time=r["ts_utc"], status=r["status"], resting_checked=r["maker_candidates"],
                                  resting_placed=r["maker_accepted"], instant_checked=r["taker_candidates"],
                                  instant_taken=r["taker_accepted"], kalshi_in_use=r["deployed_kalshi"],
                                  pmus_in_use=r["deployed_pmus"]) for r in runs],
           ["time", "status", "resting_checked", "resting_placed", "instant_checked", "instant_taken", "kalshi_in_use", "pmus_in_use"])

    rej = Counter()
    for r in runs:
        rej.update(json.loads(r.get("rejections") or "{}"))
    _write(out, "rejections.csv", [dict(reason=k, count=v) for k, v in rej.most_common()], ["reason", "count"])

    mp = d / "paper" / "learning" / "model.json"
    model = json.loads(mp.read_text()).get("groups", {}) if mp.exists() else {}
    lrows = [dict(group=g, strategy=g.split("|")[0], level=g.count("|"), n=m["n"], hit_rate=m["hit_rate"],
                  raw_cents=round(m["raw_value_per_contract"] * 100, 3), learned_cents=round(m["value_per_contract"] * 100, 3),
                  low_cents=round(m["low"] * 100, 3), high_cents=round(m["high"] * 100, 3), action=m["action"])
             for g, m in model.items() if g.count("|") <= 2]
    lrows.sort(key=lambda r: (r["level"], -r["n"]))
    _write(out, "learning.csv", lrows, ["group", "strategy", "level", "n", "hit_rate", "raw_cents", "learned_cents",
                                        "low_cents", "high_cents", "action"])

    cands = _read(d, "taker_candidates.csv")
    per = defaultdict(lambda: [0, 0])
    for c in cands:
        per[c["ts_utc"]][0] += 1
        per[c["ts_utc"]][1] += _f(c["contracts"]) >= 1 and _f(c["net_per_contract"]) > 0
    _write(out, "instant.csv", [dict(time=t, gaps_checked=v[0], tradable=v[1]) for t, v in sorted(per.items())],
           ["time", "gaps_checked", "tradable"])
    print(f"wrote {', '.join(sorted(p.name for p in out.glob('*.csv')))} to {out}")


if __name__ == "__main__":
    main()
