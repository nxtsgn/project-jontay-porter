"""Match Kalshi and Polymarket NBA markets and compute paper arbitrage after costs.

Reads snapshots written by collect_prices.py (data/prices/*.csv.gz), pairs identical propositions
(same game, same player + stat + line, same total line, same moneyline team) and, for each pair, prices the two
possible two-leg hedges. Nothing is traded. Opportunities above the threshold are appended to
data/paper/opportunities.csv for later settlement tracking.

    python scripts/find_arbs.py                      # latest snapshot in the newest file
    python scripts/find_arbs.py --min-edge 0.005 --size 100 --top 15
    python scripts/find_arbs.py --no-log             # print only

Edge is per $1 payout contract: edge = 1 - cost_of_both_legs - fees.
"""
import argparse
import csv
import glob
import gzip
import math
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "paper" / "opportunities.csv"
LOG_FIELDS = ["ts_utc", "market_type", "game", "player", "line", "combo", "kalshi_id", "poly_id", "k_leg", "k_price",
              "p_leg", "p_price", "gross_edge", "kalshi_fee", "poly_fee", "net_edge", "dnp_exposed", "expected_edge"]

MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
# Polymarket slug codes -> Kalshi codes where they differ (extend if the matcher reports unmatched games)
TEAM_ALIAS = {"no": "nop", "ny": "nyk", "sa": "sas", "gs": "gsw", "uta": "uta", "utah": "uta", "phx": "phx", "bkn": "bkn"}
STATS = {"points", "rebounds", "assists", "threes", "steals", "blocks"}


def norm_team(c):
    c = c.lower()
    return TEAM_ALIAS.get(c, c)


def norm_name(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def kalshi_key(r):
    """('2026-10-23', frozenset({'min','orl'}), suffix) from tickers like KXNBAPTS-26OCT23MINORL-MINADOSUNMU13-20."""
    parts = r["market_id"].split("-")
    m = re.match(r"(\d\d)([A-Z]{3})(\d\d)([A-Z]+)$", parts[1])
    if not m:
        return None
    date = f"20{m[1]}-{MONTHS[m[2]]:02d}-{m[3]}"
    codes = m[4]
    teams = frozenset({norm_team(codes[:3]), norm_team(codes[3:])})
    return date, teams, parts[-1]


def poly_key(r):
    """Polymarket slug: nba-<away>-<home>-YYYY-MM-DD[-...]."""
    m = re.match(r"nba-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})", r["market_id"])
    if not m:
        return None
    return m[3], frozenset({norm_team(m[1]), norm_team(m[2])}), norm_team(m[1])  # last = outcome1 team for moneyline


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kalshi_fee(price, size):
    """Per-contract taker fee: roundup(0.07*C*P*(1-P)) to the cent, spread over C contracts."""
    return math.ceil(round(0.07 * size * price * (1 - price) * 100, 6)) / 100 / size


def load_latest(path=None):
    path = path or sorted(glob.glob(str(ROOT / "data" / "prices" / "prices_*.csv.gz")))[-1]
    rows = list(csv.DictReader(gzip.open(path, "rt")))
    ts = max(r["ts_utc"] for r in rows)
    return ts, [r for r in rows if r["ts_utc"] == ts]


def build_pairs(rows):
    kal, pol = {}, {}
    for r in rows:
        t = r["market_type"]
        if r["venue"] == "kalshi":
            k = kalshi_key(r)
            if not k:
                continue
            date, teams, suffix = k
            if t in STATS:
                key = (t, date, teams, norm_name(r["player"]), f(r["line"]))
                kal[key] = (r, 1)
            elif t == "totals":
                kal[(t, date, teams, f(r["line"]))] = (r, 1)
            elif t == "moneyline":
                kal[(t, date, teams, norm_team(suffix))] = (r, 1)
        else:
            k = poly_key(r)
            if not k:
                continue
            date, teams, first = k
            if t in STATS:
                pol[(t, date, teams, norm_name(r["player"]), f(r["line"]))] = (r, 1)
            elif t == "totals" and r["outcome1"].lower() == "over":
                pol[(t, date, teams, f(r["line"]))] = (r, 1)
            elif t == "moneyline":
                other = next(iter(teams - {first}), None)
                pol[(t, date, teams, first)] = (r, 1)
                pol[(t, date, teams, other)] = (r, -1)  # same market seen from the other team: yes/no swap
    pairs = [(kal[k], pol[k]) for k in kal if k in pol]
    return pairs, len(kal), len(pol)


def liquid(k, p, min_k_vol, min_p_liq):
    """Skip pairs where either quote sits in a market nobody trades (the 'sign in a shop window' problem)."""
    return (f(k["volume"]) or 0) >= min_k_vol and (f(p["liquidity"]) or 0) >= min_p_liq


def evaluate(k, p, flip, size, poly_fee_rate, dnp_prob, is_prop):
    ky, kn = f(k["yes_ask"]), f(k["no_ask"])
    py, pn = f(p["yes_ask"]), f(p["no_ask"])
    if flip < 0:
        py, pn = pn, py
    out = []
    for combo, kleg, kp, pleg, pp in (("K-yes/P-no", "yes", ky, "no", pn), ("K-no/P-yes", "no", kn, "yes", py)):
        if kp is None or pp is None or not (0 < kp < 1) or not (0 < pp < 1):
            continue
        kf, pf = kalshi_fee(kp, size), poly_fee_rate * pp * (1 - pp)
        gross = 1 - kp - pp
        net = gross - kf - pf
        # Kalshi refunds a DNP prop; Polymarket resolves it "No". Holding Polymarket Yes leaves the Kalshi leg refunded
        # and the Polymarket leg lost.
        exposed = is_prop and pleg == "yes"
        expected = (1 - dnp_prob) * net + dnp_prob * (-pp - pf) if exposed else net
        out.append(dict(combo=combo, k_leg=kleg, k_price=kp, p_leg=pleg, p_price=pp, gross_edge=gross, kalshi_fee=kf,
                        poly_fee=pf, net_edge=net, dnp_exposed=exposed, expected_edge=expected))
    if len(out) == 2 and all(o["gross_edge"] > 0 for o in out):
        return []  # both directions "profitable" means the quotes are crossed or stale, not an opportunity
    return out


def injury_suspect(o, max_gap, min_yes):
    """A DNP-exposed hedge (we'd hold Polymarket Yes) whose Polymarket Yes is far cheaper than Kalshi implies is most often
    the market pricing in that the player will sit, not free money. Backtest since Oct 22: every such loser looked like this."""
    return o["dnp_exposed"] and (o["gross_edge"] > max_gap or o["p_price"] < min_yes)


def started(p, ts):
    """True once the game has tipped off (live prices move too fast for 15-minute snapshots)."""
    try:
        return (p.get("event_time") or "9999") <= ts
    except TypeError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="specific prices_*.csv.gz (default: newest)")
    ap.add_argument("--min-edge", type=float, default=0.01, help="min expected net edge per $1 contract to log")
    ap.add_argument("--size", type=int, default=100, help="contracts per leg, used for Kalshi fee rounding")
    ap.add_argument("--poly-fee-rate", type=float, default=0.0, help="Polymarket fee as rate*p*(1-p); 0 = zero fees")
    ap.add_argument("--dnp-prob", type=float, default=0.05, help="chance a player prop's player doesn't play")
    ap.add_argument("--min-kalshi-volume", type=float, default=10, help="min contracts traded on the Kalshi market")
    ap.add_argument("--min-poly-liquidity", type=float, default=500, help="min $ liquidity on the Polymarket market")
    ap.add_argument("--max-exposed-gap", type=float, default=-1,
                    help="injury filter: drop DNP-exposed hedges with a gross gap above this. Default -1 drops every DNP-exposed hedge (pure arbitrage only); e.g. 0.05 keeps small gaps; 1 disables")
    ap.add_argument("--min-exposed-yes", type=float, default=0.12,
                    help="injury filter: drop DNP-exposed hedges where Polymarket Yes costs less than this")
    ap.add_argument("--include-started", action="store_true", help="keep games that have already tipped off")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--log", default=str(LOG), help="where to append logged opportunities")
    a = ap.parse_args()

    ts, rows = load_latest(a.file)
    pairs, nk, npol = build_pairs(rows)
    print(f"snapshot {ts}: {nk} Kalshi / {npol} Polymarket candidate keys, {len(pairs)} matched pairs")
    results = []
    thin = live = injured = 0
    for (k, _), (p, flip) in pairs:
        if not a.include_started and started(p, ts):
            live += 1
            continue
        if not liquid(k, p, a.min_kalshi_volume, a.min_poly_liquidity):
            thin += 1
            continue
        is_prop = k["market_type"] in STATS
        for o in evaluate(k, p, flip, a.size, a.poly_fee_rate, a.dnp_prob, is_prop):
            if injury_suspect(o, a.max_exposed_gap, a.min_exposed_yes):
                injured += 1
                continue
            o.update(ts_utc=ts, market_type=k["market_type"], game=k["event_id"], player=k["player"],
                     line=k["line"], kalshi_id=k["market_id"], poly_id=p["market_id"])
            results.append(o)
    print(f"skipped {live} pairs in games already started, {thin} thinly traded pairs (Kalshi volume < {a.min_kalshi_volume:g} "
          f"or Polymarket liquidity < ${a.min_poly_liquidity:g}), {injured} hedges flagged as likely injury/DNP")
    results.sort(key=lambda o: o["expected_edge"], reverse=True)
    hits = [o for o in results if o["expected_edge"] >= a.min_edge]
    print(f"{len(results)} hedges priced; {len(hits)} with expected net edge >= {a.min_edge:.3f}")
    print(f"\nTop {a.top} by expected net edge (per $1 contract):")
    for o in results[: a.top]:
        tag = " DNP-exposed" if o["dnp_exposed"] else ""
        print(f"  {o['expected_edge']:+.3f} (net {o['net_edge']:+.3f}, gross {o['gross_edge']:+.3f}) {o['market_type']:<9}"
              f"{o['player'] or o['game']:<26} line {o['line'] or '-':<6} {o['combo']} K {o['k_price']:.2f} + P {o['p_price']:.2f}{tag}")
    if hits and not a.no_log:
        log = Path(a.log)
        log.parent.mkdir(parents=True, exist_ok=True)
        new = not log.exists()
        with log.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(hits)
        print(f"\nlogged {len(hits)} paper opportunities to {log}")


if __name__ == "__main__":
    main()
