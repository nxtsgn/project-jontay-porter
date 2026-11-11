"""Find guaranteed-payout gaps between Kalshi and Polymarket US NBA markets (and within each venue).

Every row from collect_prices.py is a YES/NO question "is X > line?" (player stat, combined total, or the canonical
team's margin; see collect_prices.py). For any two contracts on the same quantity with lines L1 <= L2:

    buy YES on "X > L1"  +  buy NO on "X > L2"   pays at least $1 in every outcome
                                                  (and $2 if L1 < X <= L2, the "middle").

So the hedge is a sure profit when  yes_ask(L1) + no_ask(L2) + fees < 1.  With L1 == L2 on two venues this is the classic
cross-venue arbitrage; with L1 < L2 it is a "ladder" gap (e.g. 25+ priced above 20+). Both venues and same-venue pairs count.

Fees are each venue's taker formula, coef x C x p x (1-p) per order: Kalshi 0.07, rounded up to the cent; Polymarket US uses
the market's feeCoefficient (0.095 base tier), banker's-rounded to the cent. Resting (maker) orders would be cheaper; not modeled.

Player didn't play: Kalshi settles to "fair price" and Polymarket US to "last fair market price", so neither leg is a forced
loss; the hedge is no longer guaranteed exactly $1 but there is no one-sided wipeout like the international site had.

    python scripts/find_arbs.py                        # newest snapshot in the newest prices file
    python scripts/find_arbs.py --file data/prices/prices_us_2026-11-10.csv.gz --top 20
"""
import argparse
import csv
import glob
import gzip
import json
import math
import re
import sys
import unicodedata
import urllib.request
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "paper" / "opportunities.csv"
CANDS = ROOT / "data" / "paper" / "taker_candidates.csv"  # every depth-checked candidate, profitable or not (for learning)
CAND_FIELDS = ["ts_utc", "sport", "market_type", "event_time", "a_venue", "a_id", "a_line", "a_price", "b_venue", "b_id", "b_line",
               "b_price", "net_top", "a_depth", "b_depth", "contracts", "net_per_contract"]
LOG_FIELDS = ["ts_utc", "sport", "game_date", "teams", "market_type", "player", "event_time",
              "a_venue", "a_id", "a_line", "a_flipped", "a_price",
              "b_venue", "b_id", "b_line", "b_flipped", "b_price",
              "contracts", "cost", "fees", "net_per_contract", "net_total", "middle", "a_depth", "b_depth"]
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_name(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def fee(venue, coef, price, contracts):
    """Total taker fee in dollars for one order."""
    raw = coef * contracts * price * (1 - price)
    if venue == "kalshi":
        return math.ceil(round(raw * 100, 6)) / 100
    return float(Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


_SERIES_FEE = {}


def kalshi_maker_fee(ticker, price, contracts):
    """Kalshi fee on a resting (maker) fill. Only series with fee_type 'quadratic_with_maker_fees' charge it:
    roundup(0.0175 x C x P x (1-P) x multiplier). Checked 2026-11-11: most game/spread/total series charge it, props don't."""
    series = ticker.split("-")[0]
    if series not in _SERIES_FEE:
        d = _get(f"https://api.elections.kalshi.com/trade-api/v2/series/{series}") or {}
        s = d.get("series") or {}
        # unknown -> assume the fee applies (the cautious choice)
        _SERIES_FEE[series] = (s.get("fee_type", "quadratic_with_maker_fees"), float(s.get("fee_multiplier") or 1))
    kind, mult = _SERIES_FEE[series]
    if kind != "quadratic_with_maker_fees":
        return 0.0
    return math.ceil(round(0.0175 * mult * contracts * price * (1 - price) * 100, 6)) / 100


def usable(r, max_spread):
    """Both sides quoted, and the book isn't so wide that the quote is probably stale."""
    yb, ya, nb, na = (f(r[k]) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))
    if None in (ya, na) or not (0 < ya < 1) or not (0 < na < 1):
        return False
    return (ya - (yb or 0)) <= max_spread and (na - (nb or 0)) <= max_spread


def groups(rows, ts, max_spread):
    g = {}
    for r in rows:
        if (r.get("event_time") or "9999") <= ts or not usable(r, max_spread) or f(r["line"]) is None:
            continue  # game already started, or unusable quote
        key = (r.get("sport") or "nba", r["game_date"], r["teams"], r["market_type"], norm_name(r["player"]))
        g.setdefault(key, []).append(r)
    return g


def hedges(rows, ts, contracts=50, max_spread=0.10, min_net=0.0):
    """All YES(L1)+NO(L2) hedges with guaranteed net >= min_net per contract (fees at `contracts` per leg)."""
    out = []
    for key, rs in groups(rows, ts, max_spread).items():
        for a in rs:
            for b in rs:
                if a["market_id"] == b["market_id"]:
                    continue
                la, lb = f(a["line"]), f(b["line"])
                if la > lb:
                    continue
                pa, pb = f(a["yes_ask"]), f(b["no_ask"])
                fees = fee(a["venue"], f(a["fee_coef"]), pa, contracts) + fee(b["venue"], f(b["fee_coef"]), pb, contracts)
                net = 1 - pa - pb - fees / contracts
                if net < min_net:
                    continue
                out.append(dict(ts_utc=ts, sport=key[0], game_date=key[1], teams=key[2], market_type=key[3], player=a["player"] or b["player"],
                                event_time=min(a["event_time"], b["event_time"]),
                                a_venue=a["venue"], a_id=a["market_id"], a_line=la, a_flipped=a.get("flipped", 0), a_price=pa, a_fee_coef=f(a["fee_coef"]),
                                b_venue=b["venue"], b_id=b["market_id"], b_line=lb, b_flipped=b.get("flipped", 0), b_price=pb, b_fee_coef=f(b["fee_coef"]),
                                contracts=contracts, cost=round(pa + pb, 4), fees=round(fees, 2),
                                net_per_contract=round(net, 4), net_total=round(net * contracts, 2), middle=la < lb))
    out.sort(key=lambda o: -o["net_per_contract"])
    return out


# ---------------------------------------------------------------- order-book depth (contracts available at the quoted ask)
def _get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"WARN depth {url[:120]}: {e}", file=sys.stderr)
        return None


def depth(venue, market_id, side_yes, flipped, price):
    """Contracts offered at or below `price` for the row's YES (side_yes=True) or NO side."""
    raw_yes = side_yes != bool(int(flipped or 0))  # which side on the venue's own book
    if venue == "kalshi":
        d = _get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{market_id}/orderbook")
        ob = (d or {}).get("orderbook_fp") or {}
        # buying YES fills against NO bids (price 1 - bid); buying NO fills against YES bids
        bids = ob.get("no_dollars" if raw_yes else "yes_dollars") or []
        return round(sum(float(q) for p, q in bids if 1 - float(p) <= price + 1e-9), 2)
    d = _get(f"https://gateway.polymarket.us/v1/markets/{market_id}/book")
    md = (d or {}).get("marketData") or {}
    # buying YES lifts offers; buying NO (= selling YES) hits YES bids at 1 - price
    levels = md.get("offers" if raw_yes else "bids") or []
    ok = (lambda px: px <= price + 1e-9) if raw_yes else (lambda px: 1 - px <= price + 1e-9)
    return round(sum(float(l["qty"]) for l in levels if ok(float(l["px"]["value"]))), 2)


def size_up(o, budget):
    """Re-price a hedge at the size both books and the per-venue budget allow."""
    o["a_depth"] = depth(o["a_venue"], o["a_id"], True, o["a_flipped"], o["a_price"])
    o["b_depth"] = depth(o["b_venue"], o["b_id"], False, o["b_flipped"], o["b_price"])
    cap_a = budget / o["a_price"] if o["a_venue"] != o["b_venue"] else budget / (o["a_price"] + o["b_price"])
    cap_b = budget / o["b_price"] if o["a_venue"] != o["b_venue"] else cap_a
    c = math.floor(min(o["a_depth"], o["b_depth"], cap_a, cap_b))
    if c < 1:
        o.update(contracts=0, fees=0, net_total=0)
        return o
    fees = fee(o["a_venue"], f(o.get("a_fee_coef")) or (0.07 if o["a_venue"] == "kalshi" else 0.095), o["a_price"], c) + \
        fee(o["b_venue"], f(o.get("b_fee_coef")) or (0.07 if o["b_venue"] == "kalshi" else 0.095), o["b_price"], c)
    net = 1 - o["cost"] - fees / c
    o.update(contracts=c, fees=round(fees, 2), net_per_contract=round(net, 4), net_total=round(net * c, 2))
    return o


def detect_and_log(rows, ts, fetch_depth=True, budget=50.0, log=LOG, min_net=0.0):
    hs = hedges(rows, ts, min_net=min_net)
    print(f"gap detector: {len(hs)} hedges with guaranteed net >= {min_net:+.3f} per contract (before sizing)")
    if fetch_depth:
        top = [dict(o, net_top=o["net_per_contract"]) for o in hs[:40]]  # top 40 only, to keep the run fast
        hs = [size_up(o, budget) for o in top]
        if hs and log:
            new = not CANDS.exists()
            CANDS.parent.mkdir(parents=True, exist_ok=True)
            with CANDS.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=CAND_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerows(hs)
        hs = [o for o in hs if o["contracts"] >= 1 and o["net_per_contract"] > 0]
    for o in hs[:10]:
        print(f"  {o['net_per_contract']:+.3f}/contract x {o['contracts']} = ${o['net_total']:+.2f}  {o['market_type']:<9}"
              f"{o['player'] or o['teams']:<22} YES>{o['a_line']} {o['a_venue']}@{o['a_price']:.2f} + NO>{o['b_line']} "
              f"{o['b_venue']}@{o['b_price']:.2f}{' (middle)' if o['middle'] else ''}")
    if hs and log:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        new = not log.exists()
        with log.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(hs)
        print(f"logged {len(hs)} sized opportunities to {log}")
    return hs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="prices_us_*.csv.gz (default: newest)")
    ap.add_argument("--min-net", type=float, default=-0.02, help="show hedges down to this net per contract")
    ap.add_argument("--contracts", type=int, default=50)
    ap.add_argument("--max-spread", type=float, default=0.10)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--depth", action="store_true", help="fetch live order-book depth and size the top hedges ($50/venue)")
    a = ap.parse_args()
    path = a.file or sorted(glob.glob(str(ROOT / "data" / "prices" / "prices_us_*.csv.gz")))[-1]
    rows = [r for r in csv.DictReader(gzip.open(path, "rt")) if "teams" in r]
    ts = max(r["ts_utc"] for r in rows)
    rows = [r for r in rows if r["ts_utc"] == ts]
    hs = hedges(rows, ts, a.contracts, a.max_spread, a.min_net)
    same = [o for o in hs if o["a_line"] == o["b_line"]]
    print(f"{path} @ {ts}: {len(rows)} markets, {len(hs)} hedges with net >= {a.min_net:+.3f} "
          f"({len(same)} same-line, {len(hs) - len(same)} ladder); {sum(o['net_per_contract'] > 0 for o in hs)} profitable")
    if a.depth:
        hs = [size_up(o, 50.0) for o in hs[: a.top]]
    for o in hs[: a.top]:
        sz = f" depth {o.get('a_depth')}/{o.get('b_depth')} -> {o['contracts']} contracts ${o['net_total']:+.2f}" if a.depth else ""
        print(f"  {o['net_per_contract']:+.3f} {o['market_type']:<9}{o['player'] or o['teams']:<22} "
              f"YES>{o['a_line']} {o['a_venue']}@{o['a_price']:.2f} + NO>{o['b_line']} {o['b_venue']}@{o['b_price']:.2f}"
              f"{' (middle)' if o['middle'] else ''}{sz}")


if __name__ == "__main__":
    main()
