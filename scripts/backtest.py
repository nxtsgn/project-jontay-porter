"""Grade the backfilled pre-game snapshots: would cross-venue hedges have made money after costs?

Uses the same matcher and fee math as find_arbs.py, and the real results stored by backfill.py (Kalshi's settled result is
the truth for both venues; if Kalshi settled to neither yes nor no, the player is treated as a DNP).

    python scripts/backtest.py                       # newest file in data/backfill
    python scripts/backtest.py --min-edge 0.02 --min-kalshi-volume 25
"""
import argparse
import csv
import glob
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import ROOT, STATS, build_pairs, evaluate, f, injury_suspect  # noqa: E402
from settle import pnl  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--min-edge", type=float, default=0.01)
    ap.add_argument("--min-kalshi-volume", type=float, default=10, help="Kalshi contracts traded before the snapshot")
    ap.add_argument("--min-poly-volume", type=float, default=2000,
                    help="Polymarket total $ volume (whole-market proxy; thin markets carry stale prices)")
    ap.add_argument("--max-kalshi-spread", type=float, default=0.05, help="skip Kalshi books wider than this at the snapshot")
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--poly-fee-rate", type=float, default=0.0)
    ap.add_argument("--dnp-prob", type=float, default=0.05)
    ap.add_argument("--max-exposed-gap", type=float, default=-1, help="injury filter (see find_arbs.py); -1 = drop all DNP-exposed, 1 disables")
    ap.add_argument("--min-exposed-yes", type=float, default=0.12, help="injury filter (see find_arbs.py); 0 disables")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    path = a.file or sorted(glob.glob(str(ROOT / "data" / "backfill" / "pregame_*.csv.gz")))[-1]
    rows = list(csv.DictReader(gzip.open(path, "rt")))
    result = {r["market_id"]: r["result"] for r in rows if r["venue"] == "kalshi"}
    pairs, nk, npol = build_pairs(rows)
    print(f"{path}\n{nk} Kalshi / {npol} Polymarket keys, {len(pairs)} matched pairs")

    graded, thin, injured, seen = [], 0, 0, set()
    for (k, _), (p, flip) in pairs:
        kspread = (f(k["yes_ask"]) or 1) - (f(k["yes_bid"]) or 0)
        if (f(k["volume"]) or 0) < a.min_kalshi_volume or (f(p["volume"]) or 0) < a.min_poly_volume or kspread > a.max_kalshi_spread:
            thin += 1
            continue
        res = result.get(k["market_id"])
        for o in evaluate(k, p, flip, a.size, a.poly_fee_rate, a.dnp_prob, k["market_type"] in STATS):
            if o["expected_edge"] < a.min_edge:
                continue
            if injury_suspect(o, a.max_exposed_gap, a.min_exposed_yes):
                injured += 1
                continue
            dnp = res not in ("yes", "no")
            if dnp and k["market_type"] not in STATS:
                continue  # voided game-level market: no clean grade
            dup = (k["event_id"], k["market_type"], k["line"], round(o["k_price"] + o["p_price"], 4)) if k["market_type"] == "moneyline" else None
            if dup and dup in seen:
                continue  # the same moneyline hedge seen from the other team's Kalshi market
            seen.add(dup)
            o.update(market_type=k["market_type"], game=k["event_id"], player=k["player"], line=k["line"],
                     result=res, pnl=pnl(o, res == "yes", dnp), dnp=dnp)
            graded.append(o)
    print(f"skipped {thin} thin/stale pairs (Kalshi volume/spread or Polymarket volume); {injured} flagged as likely injury/DNP; {len(graded)} hedges with expected edge >= {a.min_edge:g}")
    if not graded:
        return
    tot = sum(g["pnl"] for g in graded)
    pred = sum(g["expected_edge"] for g in graded)
    print(f"\nPredicted avg edge {pred / len(graded):+.4f}  |  realized avg {tot / len(graded):+.4f}  |  "
          f"winners {sum(g['pnl'] > 0 for g in graded)} / losers {sum(g['pnl'] <= 0 for g in graded)}  |  DNPs {sum(g['dnp'] for g in graded)}")
    for t in sorted({g["market_type"] for g in graded}):
        sub = [g for g in graded if g["market_type"] == t]
        print(f"  {t:<10} n={len(sub):<4} predicted {sum(g['expected_edge'] for g in sub) / len(sub):+.4f} realized {sum(g['pnl'] for g in sub) / len(sub):+.4f}")
    print(f"\nTop {a.top} by expected edge:")
    for g in sorted(graded, key=lambda g: -g["expected_edge"])[: a.top]:
        print(f"  exp {g['expected_edge']:+.3f} real {g['pnl']:+.3f} {g['market_type']:<9}{g['player'] or g['game']:<24} line {g['line'] or '-':<6}"
              f" {g['combo']} K{g['k_price']:.2f}+P{g['p_price']:.2f} result={g['result']}{' DNP' if g['dnp'] else ''}")


if __name__ == "__main__":
    main()
