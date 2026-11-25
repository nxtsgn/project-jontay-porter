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
    Saved to data/paper/maker_plans.csv (replaced every run; unfilled plans are treated as cancelled). Each plan is
    resolved one run later (maker_plans_prev.csv), over the time its orders were really up.
Every run appends a line to data/paper/maker_runs.csv (plans, fills, fill rate) so fill rates can be measured.

Kalshi charges a maker fee only on series with fee_type "quadratic_with_maker_fees" (most game lines; props don't):
find_arbs.kalshi_maker_fee looks it up per series and it's netted out of every edge and locked_pnl. Polymarket US doesn't publish its trades,
so only the Kalshi leg can be tested as the resting one.

    python scripts/maker_sim.py --file data/prices/prices_us_2026-11-10.csv.gz     # plan from a saved snapshot only
"""
import argparse
import csv
import glob
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import f, fee, groups, kalshi_maker_fee  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "data" / "paper"
PLANS, FILLS, RUNS = PAPER / "maker_plans.csv", PAPER / "maker_fills.csv", PAPER / "maker_runs.csv"
# A plan's orders stay up until the NEXT run's orders replace them (~2 min after that run's snapshot), after that run
# has already resolved: so each plan is resolved one run later, from this file (since 2026-11-21).
PREV_PLANS = PAPER / "maker_plans_prev.csv"
BAGS = PAPER / "maker_bags.csv"  # unhedged leftovers, graded at settlement
BAG_FIELDS = ["placed_ts", "sport", "rest_id", "player", "unhedged_qty", "rest_px", "payout", "bag_pnl"]
OUTCOMES = PAPER / "maker_outcomes.csv"  # every resolved plan, filled or not (what the learner trains on)
OUTCOME_FIELDS = ["placed_ts", "resolved_ts", "sport", "market_type", "player", "event_time", "rest_id", "rest_norm_side",
                  "rest_line", "hedge_id", "hedge_line", "rest_px", "rest_bid", "rest_ask", "hedge_px_at_plan", "edge_at_plan",
                  "contracts", "middle", "filled", "hedge_status", "hedged_qty", "unhedged_qty", "locked_pnl"]
# A resting order only counts as filled within 15 minutes of being placed. The bot re-places live plans every run (~5 min),
# so this matches continuous quoting. Before 2026-11-11 the window ran until the next collector run, which was sometimes
# hours later: too generous.
ORDER_LIFETIME_S = 15 * 60
MAKER_FEE_FIX_TS = "2026-11-11T19:10:00Z"  # fills resolved before this didn't net the Kalshi maker fee
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}
PLAN_FIELDS = ["placed_ts", "sport", "game_date", "teams", "market_type", "player", "event_time",
               "rest_id", "rest_norm_side", "rest_raw_side", "rest_line", "rest_px", "rest_bid", "rest_ask",
               "hedge_id", "hedge_norm_side", "hedge_flipped", "hedge_line", "hedge_px_at_plan", "hedge_fee_coef",
               "contracts", "edge_at_plan", "middle"]
FILL_FIELDS = PLAN_FIELDS + ["resolved_ts", "filled", "first_fill_ts", "hedge_px_at_fill", "hedge_status",
                             "hedged_qty", "unhedged_qty", "cost", "hedge_fee", "locked_pnl", "locked_per_contract"]
RUN_FIELDS = ["ts_utc", "plans_resolved", "plans_filled", "contracts_filled", "locked_pnl", "new_plans"]
# Unhedged-fill fixes (2026-11-16), both set in config/trading_rules.json -> per_hedge:
#  1. cancel_if_hedge_moves: a live bot watches the Polymarket US hedge and pulls the Kalshi order the moment the hedge
#     price makes the trade a loser. Simulated with Polymarket US's minute-by-minute price history: the order counts as
#     cancelled CANCEL_DELAY_S after the first minute the hedge crossed that line, and fills after that don't count.
#     Logged to maker_cancels.csv (how many contracts each cancel kept off our books).
#  2. min_hedge_depth_multiple: only plan as many contracts as half (for 2x) of what Polymarket US has at a profitable
#     price, so a fill can still be hedged after other traders take some of the book.
UNHEDGED_FIX_TS = "2026-11-16T23:00:00Z"  # plans resolved from here on use both fixes (for before/after comparisons)
CANCEL_DELAY_S = 60  # price history is per minute; the live bot reacts within seconds, so this is conservative
CANCELS = PAPER / "maker_cancels.csv"
# A real order reaches Kalshi only when the trader runs, ~a minute after the price snapshot. Fills before that can't
# happen, so they are not counted (since 2026-11-21; before, fills were counted from the snapshot time).
DELAYS = PAPER / "order_delay.csv"  # ts_utc, seconds from snapshot to the trader acting, one row per run
DEFAULT_DELAY_S = 60
RECONCILED = PAPER / "hedge_reconciled.csv"  # every simulated hedge replaced by what the Oracle hedger saw live
RECONCILED_FIELDS = ["reconciled_ts", "placed_ts", "rest_id", "player", "first_fill_ts", "hedger_fill_ts",
                     "seconds_fill_to_hedge", "old_status", "new_status", "old_hedge_px", "new_hedge_px",
                     "old_locked_pnl", "new_locked_pnl", "old_unhedged_qty", "new_unhedged_qty"]
RECONCILE_LOOKBACK_S = 48 * 3600
# Until this time the hedger re-read the book for every trade without removing what it had just taken, so several trades
# close together could all "hedge" against the same contracts. Such rows can't support a better result.
DEPTH_FIX_TS = "2026-11-25T17:00:00Z"

CANCEL_FIELDS = ["resolved_ts", "placed_ts", "sport", "market_type", "player", "rest_id", "rest_raw_side", "rest_px",
                 "hedge_id", "hedge_px_at_plan", "hedge_px_at_cancel", "cancel_ts", "contracts", "filled_with_cancel",
                 "filled_without_cancel"]
RULES = ROOT / "config" / "trading_rules.json"


def rule(key, default):
    try:
        return json.loads(RULES.read_text())["per_hedge"].get(key, default)
    except Exception:  # noqa: BLE001
        return default


def epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def get(url, params, tries=4):
    """GET with backoff; Kalshi rate-limits (HTTP 429) when many plans are resolved at once."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"WARN {url}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (i + 1))
    return None


def plan(rows, ts, min_edge=0.01, budget=50.0, max_spread=0.25, max_plans=150, max_book_checks=250, min_contracts=5):
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
                    edge = 1 - px - hp - (hfee + kalshi_maker_fee(k["market_id"], px, c)) / c
                    if edge < min_edge:
                        continue
                    raw = rest_side if not int(k.get("flipped") or 0) else ("no" if rest_side == "yes" else "yes")
                    o = dict(placed_ts=ts, sport=key[0], game_date=key[1], teams=key[2], market_type=key[3], player=k["player"] or p["player"],
                             event_time=min(k["event_time"], p["event_time"]),
                             rest_id=k["market_id"], rest_norm_side=rest_side, rest_raw_side=raw, rest_line=f(k["line"]),
                             rest_px=px, rest_bid=bid, rest_ask=ask,
                             hedge_id=p["market_id"], hedge_norm_side=hedge_side, hedge_flipped=int(p.get("flipped") or 0), hedge_line=f(p["line"]),
                             hedge_px_at_plan=hp, hedge_fee_coef=f(p["fee_coef"]),
                             contracts=c, edge_at_plan=round(edge, 4), middle=f(a["line"]) < f(b["line"]))
                    slot = (k["market_id"], rest_side)
                    if slot not in best or o["edge_at_plan"] > best[slot]["edge_at_plan"]:
                        best[slot] = o
    # Top-of-book quotes on Polymarket US are often dust (e.g. 0.07 contracts), so re-price the hedge against the real
    # book for the planned size before keeping a plan: average price for `contracts`, shrinking size to what's there.
    checked, mult = [], max(1.0, float(rule("min_hedge_depth_multiple", 1.0)))
    for o in sorted(best.values(), key=lambda o: -o["edge_at_plan"])[:max_book_checks]:
        lv, need, depth, usable = hedge_levels(o), o["contracts"], 0.0, []
        mk = kalshi_maker_fee(o["rest_id"], o["rest_px"], need) / need  # Kalshi maker fee per contract
        for px, qty in lv:  # profitable depth, up to mult x what we'd order
            if 1 - o["rest_px"] - px - o["hedge_fee_coef"] * px * (1 - px) - mk < min_edge or depth >= need * mult:
                break
            take = min(need * mult - depth, qty)
            usable.append((px, take))
            depth += take
        c = math.floor(min(need, depth / mult))  # order at most 1/mult of the profitable depth
        if c < min_contracts:
            continue
        got, spend = 0.0, 0.0
        for px, qty in usable:  # hedge price = average of the cheapest c contracts
            take = min(c - got, qty)
            got, spend = got + take, spend + take * px
            if got >= c:
                break
        hp = spend / got
        edge = 1 - o["rest_px"] - hp - (fee("polymarket_us", o["hedge_fee_coef"], hp, c) + kalshi_maker_fee(o["rest_id"], o["rest_px"], c)) / c
        if edge >= min_edge:
            checked.append(dict(o, contracts=c, hedge_px_at_plan=round(hp, 4), edge_at_plan=round(edge, 4)))
    # best plans first, but no sport may take more than 60 slots, so football and college get tested too
    out, per = [], {}
    for o in sorted(checked, key=lambda o: -o["edge_at_plan"]):
        if per.get(o["sport"], 0) < 60 and len(out) < max_plans:
            per[o["sport"]] = per.get(o["sport"], 0) + 1
            out.append(o)
    return out


def fills_since(ticker, raw_side, px, since_ts, until_ts):
    """Contracts and first time that takers sold into a resting `raw_side` bid at <= px on Kalshi."""
    qty, first, cursor = 0.0, None, None
    for _ in range(5):  # a few pages is plenty for a 15-minute window
        params = {"ticker": ticker, "min_ts": since_ts, "max_ts": until_ts, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets/trades", params)
        if not d:
            return None, None  # couldn't read the tape: unknown, NOT "didn't fill"
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


def _rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def record_order_delay(ts, seconds, keep=2000):
    """Log how long after the snapshot this run's orders would have gone out (the trader runs right after)."""
    old = _rows(DELAYS)
    rows = (old + [dict(ts_utc=ts, seconds=round(seconds, 1))])[-keep:]
    _write(DELAYS, ["ts_utc", "seconds"], rows, "w")


def order_delays():
    """{snapshot ts: seconds until the order was live}, plus the median for runs not logged (before 2026-11-21)."""
    d = {r["ts_utc"]: float(r["seconds"]) for r in _rows(DELAYS)}
    v = sorted(d.values())
    return d, (v[len(v) // 2] if v else DEFAULT_DELAY_S)


def live_from(o, delays=None):
    d, med = delays or order_delays()
    return epoch(o["placed_ts"]) + d.get(o["placed_ts"], med)


def resolve(prev, rows, ts, end=None):
    """Count each plan's fills from when its orders went out until `end` (when the next run's replaced them)."""
    out, unknown, cancels = [], 0, []
    cancel_on = bool(rule("cancel_if_hedge_moves", False))
    delays = order_delays()
    for o in prev:
        until = min(int(end or epoch(ts)), epoch(ts), epoch(o["placed_ts"]) + ORDER_LIFETIME_S)
        if o.get("event_time"):
            until = min(until, epoch(o["event_time"]))
        start = int(live_from(o, delays))
        if start >= until:
            unknown += 1  # never live before it was replaced (shouldn't happen at ~1 min vs a 2-min cycle)
            continue
        qty, first = fills_since(o["rest_id"], o["rest_raw_side"], float(o["rest_px"]), start, until)
        if qty is None:
            unknown += 1
            continue  # left out of results and learning rather than guessed
        time.sleep(0.1)
        if qty > 0 and cancel_on:  # would we have pulled the order before these fills?
            pulled = cancel_time(o, start, until)
            if pulled:
                q2, f2 = fills_since(o["rest_id"], o["rest_raw_side"], float(o["rest_px"]), start, pulled[0])
                if q2 is not None:
                    c = float(o["contracts"])
                    cancels.append(dict(o, resolved_ts=ts, hedge_px_at_cancel=pulled[1],
                                        cancel_ts=datetime.fromtimestamp(pulled[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        filled_with_cancel=round(min(q2, c), 2), filled_without_cancel=round(min(qty, c), 2)))
                    qty, first = q2, f2
        filled = min(qty, float(o["contracts"]))
        r = dict(o, resolved_ts=ts, filled=round(filled, 2), first_fill_ts=first or "")
        if filled <= 0:
            r.update(hedge_px_at_fill="", hedge_status="not filled", hedged_qty=0, unhedged_qty=0, cost="", hedge_fee=0,
                     locked_pnl=0, locked_per_contract="")
        else:
            r.update(hedge(o, filled, first))
        out.append(r)
    if unknown:
        print(f"WARN resting-order test: {unknown} plans skipped (Kalshi trade tape unavailable)", file=sys.stderr)
    if cancels:
        _write(CANCELS, CANCEL_FIELDS, cancels, "a")
        kept = sum(c["filled_without_cancel"] - c["filled_with_cancel"] for c in cancels)
        print(f"resting-order test: {len(cancels)} orders pulled because the hedge moved ({kept:g} contracts not filled)")
    return out


def hedge_history(o, start, end):
    """Polymarket US price for the hedge side, minute by minute: [(epoch, price)]. Empty if unavailable."""
    raw_yes = (o["hedge_norm_side"] == "yes") != bool(int(o.get("hedge_flipped") or 0))
    d = get("https://gateway.polymarket.us/v1/price-history", {"symbol": o["hedge_id"], "timestamp.startTimestamp": start - 60,
                                                                 "timestamp.endTimestamp": end, "fidelity": 1})
    out = []
    for p in (d or {}).get("history") or []:
        v = p.get("longPrice" if raw_yes else "shortPrice")
        if p.get("timestamp") and v not in (None, 0):
            out.append((int(p["timestamp"]), float(v)))
    return sorted(out)


def cancel_time(o, start, end):
    """(when, price) a bot watching Polymarket US would have pulled the Kalshi order: the first minute the hedge price
    made the trade lose money after both fees, plus CANCEL_DELAY_S to react. None if the hedge never moved that far
    (or the history is unavailable, in which case no cancel is assumed: no credit for a fix we can't check)."""
    rest, coef, n = float(o["rest_px"]), float(o["hedge_fee_coef"]), max(1, int(float(o["contracts"])))
    mk = kalshi_maker_fee(o["rest_id"], rest, n) / n
    line = float(rule("cancel_below_edge_cents", 0.0)) / 100
    for t, px in hedge_history(o, start, end):
        if 1 - rest - px - coef * px * (1 - px) - mk < line:
            when = max(t, start) + CANCEL_DELAY_S
            return (when, px) if when < end else None
    return None


def hedge_levels(o):
    """Polymarket US book for the hedge side as [(our price, qty)], cheapest first. Uses the live book at resolve time."""
    h = get(f"https://gateway.polymarket.us/v1/markets/{o['hedge_id']}/book", {})
    md = (h or {}).get("marketData") or {}
    raw_yes = (o["hedge_norm_side"] == "yes") != bool(int(o.get("hedge_flipped") or 0))
    if raw_yes:  # buy YES: lift offers
        lv = [(float(x["px"]["value"]), float(x["qty"])) for x in md.get("offers") or []]
    else:  # buy NO = sell YES: hit bids, our price is 1 - bid
        lv = [(round(1 - float(x["px"]["value"]), 4), float(x["qty"])) for x in md.get("bids") or []]
    return sorted(lv)


def price_at(slug, raw_yes, when_iso):
    """Polymarket US display price (top of book) for one side at the minute of `when_iso`, from public price history."""
    t = epoch(when_iso[:19] + "Z")
    d = get("https://gateway.polymarket.us/v1/price-history", {"symbol": slug, "timestamp.startTimestamp": t - 300,
                                                                 "timestamp.endTimestamp": t + 60, "fidelity": 1})
    pts = sorted((p for p in (d or {}).get("history") or [] if p.get("timestamp", 0) <= t), key=lambda p: p["timestamp"])
    if not pts:
        return None
    v = pts[-1].get("longPrice" if raw_yes else "shortPrice")
    return float(v) if v not in (None, 0) else None


def hedge(o, filled, first_fill_ts=None, max_loss=0.03):
    """Buy the other leg against the real book: walk price levels while each contract locks in no worse than -max_loss
    (cutting a small loss beats holding a one-sided bet). Whatever can't be hedged stays as an open 'bag' until settlement."""
    rest = float(o["rest_px"])
    mfee = kalshi_maker_fee(o["rest_id"], rest, max(1, math.ceil(filled)))  # charged on the Kalshi fill either way
    mk = mfee / max(1, math.ceil(filled))
    left, got, spend = filled, 0.0, 0.0
    for px, qty in hedge_levels(o):
        if left <= 0 or 1 - rest - px - float(o["hedge_fee_coef"]) * px * (1 - px) - mk < -max_loss:
            break
        take = min(left, qty)
        got, spend, left = got + take, spend + take * px, left - take
    if got <= 0:
        return dict(hedge_px_at_fill="", hedge_status="UNHEDGED", hedged_qty=0, unhedged_qty=round(filled, 2), cost=rest,
                    hedge_fee=0, locked_pnl=-mfee, locked_per_contract="")
    avg = spend / got
    # Conservative: the book above is read when this run resolves (up to ~15 min after the fill). Use whichever is worse:
    # that book, or the top-of-book price at the minute the fill happened (public price history).
    if first_fill_ts:
        raw_yes = (o["hedge_norm_side"] == "yes") != bool(int(o.get("hedge_flipped") or 0))
        at_fill = price_at(o["hedge_id"], raw_yes, first_fill_ts)
        if at_fill is not None:
            avg = max(avg, at_fill)
    hfee = fee("polymarket_us", float(o["hedge_fee_coef"]), avg, max(1, math.ceil(got)))
    pnl = (1 - rest - avg) * got - hfee - mfee  # locked_pnl includes the Kalshi maker fee where the series charges one
    return dict(hedge_px_at_fill=round(avg, 4), hedge_status="hedged" if left <= 1e-9 else "PARTIAL",
                hedged_qty=round(got, 2), unhedged_qty=round(max(left, 0), 2), cost=round(rest + avg, 4), hedge_fee=hfee,
                locked_pnl=round(pnl, 2), locked_per_contract=round(pnl / got, 4))


def settle_bags():
    """Unhedged leftovers are one-sided Kalshi positions held to settlement. Grade them once the market settles."""
    if not FILLS.exists():
        return
    from grade import venue_yes_settlement
    done = {(r["placed_ts"], r["rest_id"]) for r in csv.DictReader(BAGS.open())} if BAGS.exists() else set()
    out = []
    for r in csv.DictReader(FILLS.open()):
        q = float(r.get("unhedged_qty") or 0)
        if q <= 0 or (r["placed_ts"], r["rest_id"]) in done:
            continue
        y = venue_yes_settlement("kalshi", r["rest_id"])
        if y is None:
            continue
        pay = y if r["rest_raw_side"] == "yes" else 1 - y
        out.append(dict(placed_ts=r["placed_ts"], sport=r.get("sport", ""), rest_id=r["rest_id"], player=r["player"],
                        unhedged_qty=q, rest_px=r["rest_px"], payout=pay, bag_pnl=round((pay - float(r["rest_px"])) * q, 2)))
    if out:
        _write(BAGS, BAG_FIELDS, out, "a")
    return out


def hedger_log():
    """The Oracle hedger's log (fast_hedges.csv on the hedger-data branch), if this run fetched it."""
    p = os.environ.get("HEDGER_LOG")
    return _rows(Path(p)) if p and os.path.exists(p) and os.path.getsize(p) else []


def reused_depth(same, fast):
    """True if any of these hedges may have shared depth with another hedge of the same market within a minute."""
    for h in same:
        if (h.get("hedged_ts") or h["fill_ts"]) >= DEPTH_FIX_TS:
            continue
        near = [g for g in fast if g.get("hedge_id") == h.get("hedge_id") and g.get("fill_ts")
                and abs(epoch(g["fill_ts"][:19] + "Z") - epoch(h["fill_ts"][:19] + "Z")) <= 60]
        if len(near) > 1:
            return True
    return False


def undo_overstated(fast, now):
    """Put back the simulation's result where a past replacement isn't supported: the hedger filled fewer contracts than
    the simulation (its result was scaled up), or its hedges may have reused the same depth. Logged like any other change."""
    rec = _rows(RECONCILED)
    if not rec:
        return []
    last = {}
    for x in rec:
        last[(x["placed_ts"], x["rest_id"])] = x
    fills, changed, log = _rows(FILLS), {}, []
    for r in fills:
        x = last.get((r["placed_ts"], r["rest_id"]))
        if not x or x["reconciled_ts"] >= DEPTH_FIX_TS or float(x["new_locked_pnl"]) <= float(x["old_locked_pnl"] or 0):
            continue
        same, fq, _ = hedger_rows(r, x["hedger_fill_ts"], fast)
        if not same or (fq >= float(r["filled"]) - 0.011 and not reused_depth(same, fast)):
            continue
        left, got = float(x["old_unhedged_qty"]), round(float(r["filled"]) - float(x["old_unhedged_qty"]), 2)
        px = float(x["old_hedge_px"]) if x["old_hedge_px"] else None
        pnl, rest = float(x["old_locked_pnl"] or 0), float(r["rest_px"])
        new = dict(hedge_px_at_fill=px if px is not None else "", hedge_status=x["old_status"], hedged_qty=got if px else 0,
                   unhedged_qty=left, cost=round(rest + (px or 0), 4), locked_pnl=pnl,
                   hedge_fee=fee("polymarket_us", float(r["hedge_fee_coef"]), px, max(1, math.ceil(got))) if px else 0,
                   locked_per_contract=round(pnl / got, 4) if px and got else "")
        log.append(dict(x, reconciled_ts=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        old_status=x["new_status"], new_status=x["old_status"], old_hedge_px=x["new_hedge_px"],
                        new_hedge_px=x["old_hedge_px"], old_locked_pnl=x["new_locked_pnl"], new_locked_pnl=x["old_locked_pnl"],
                        old_unhedged_qty=x["new_unhedged_qty"], new_unhedged_qty=x["old_unhedged_qty"]))
        r.update(new)
        changed[(r["placed_ts"], r["rest_id"])] = new
    _apply(fills, changed, log)
    return log


def hedger_rows(r, fill_ts, fast):
    """The hedger's on-time rows for the order that got this fill: (rows, contracts filled, contracts hedged)."""
    h0 = next((h for h in fast if h["rest_id"] == r["rest_id"] and h.get("fill_ts") == fill_ts and str(h.get("late")) == "0"), None)
    same = [h for h in fast if h0 and h["rest_id"] == r["rest_id"] and h["placed_ts"] == h0["placed_ts"]
            and str(h.get("late")) == "0"]
    return same, sum(float(h["fill_qty"]) for h in same), sum(float(h["hedged_qty"]) for h in same)


def _apply(fills, changed, log):
    if not changed:
        return
    _write(FILLS, FILL_FIELDS, fills, "w")
    if OUTCOMES.exists():  # what the learner trains on
        outs = _rows(OUTCOMES)
        for o in outs:
            c = changed.get((o["placed_ts"], o["rest_id"]))
            if c and o.get("filled") not in ("", "0", "0.0"):
                o.update(hedge_status=c["hedge_status"], hedged_qty=c["hedged_qty"], unhedged_qty=c["unhedged_qty"],
                         locked_pnl=c["locked_pnl"])
        _write(OUTCOMES, OUTCOME_FIELDS, outs, "w")
    _write(RECONCILED, RECONCILED_FIELDS, log, "a")
    for x in log:
        print(f"hedger reconcile: {x['player'] or x['rest_id']} {x['old_status']} ${x['old_locked_pnl']} -> "
              f"{x['new_status']} ${x['new_locked_pnl']} (hedger saw it in {x['seconds_fill_to_hedge']}s)")


def reconcile(fast=None, now=None):
    """Where the Oracle hedger saw a fill live (within seconds) the simulation's hedge is replaced by the hedger's.

    The simulation only reads the Polymarket US book when its next run resolves the plan (~1-2 min after the fill) and
    keeps the worse of that and the at-fill minute price; the hedger reads the book seconds after the fill. It is the
    better measure of what a live bot gets, in both directions: a simulated hedge the hedger couldn't get becomes a
    one-sided bag, and a simulated bag the hedger did hedge becomes a locked pair. Fills the hedger only caught late,
    or didn't see, keep the simulation's conservative result. Every replacement is logged in hedge_reconciled.csv."""
    fast = hedger_log() if fast is None else fast
    if not fast or not FILLS.exists():
        return []
    now = now or time.time()
    undo_overstated(fast, now)
    by = {}
    for h in fast:
        if h.get("fill_ts") and str(h.get("late")) == "0":
            by.setdefault(h["rest_id"], []).append(h)
    done = {(r["placed_ts"], r["rest_id"]) for r in _rows(RECONCILED)}
    fills = _rows(FILLS)
    changed, log = {}, []
    for r in fills:
        k = (r["placed_ts"], r["rest_id"])
        if k in done or not r.get("first_fill_ts") or now - epoch(r["resolved_ts"]) > RECONCILE_LOOKBACK_S:
            continue
        t0 = epoch(r["first_fill_ts"][:19] + "Z")
        hs = [h for h in by.get(r["rest_id"], []) if abs(epoch(h["fill_ts"][:19] + "Z") - t0) <= 5]
        if not hs:
            continue
        # the hedger's first fill and everything it logged for that same order (one row per Kalshi trade)
        h0 = min(hs, key=lambda h: h["fill_ts"])
        same = [h for h in by[r["rest_id"]] if h["placed_ts"] == h0["placed_ts"]]
        fq = sum(float(h["fill_qty"]) for h in same)
        hq = sum(float(h["hedged_qty"]) for h in same)
        if fq <= 0:
            continue
        filled, rest, coef = float(r["filled"]), float(r["rest_px"]), float(r["hedge_fee_coef"])
        got = round(filled * min(1.0, hq / fq), 2)
        avg = sum(float(h["hedged_qty"]) * float(h["hedge_avg_px"] or 0) for h in same) / hq if hq else None
        mfee = kalshi_maker_fee(r["rest_id"], rest, max(1, math.ceil(filled)))
        if got > 0:
            hfee = fee("polymarket_us", coef, avg, max(1, math.ceil(got)))
            pnl = (1 - rest - avg) * got - hfee - mfee
            new = dict(hedge_px_at_fill=round(avg, 4), hedge_status="hedged" if filled - got <= 1e-9 else "PARTIAL",
                       hedged_qty=got, unhedged_qty=round(filled - got, 2), cost=round(rest + avg, 4), hedge_fee=hfee,
                       locked_pnl=round(pnl, 2), locked_per_contract=round(pnl / got, 4))
        else:
            new = dict(hedge_px_at_fill="", hedge_status="UNHEDGED", hedged_qty=0, unhedged_qty=round(filled, 2),
                       cost=rest, hedge_fee=0, locked_pnl=round(-mfee, 2), locked_per_contract="")
        # a better result must be fully backed: the hedger filled at least as much, on depth it hadn't already used
        value = lambda d: float(d["locked_pnl"] or 0) - float(d["unhedged_qty"] or 0) * rest  # noqa: E731
        if value(new) > value(r) and (fq < filled - 0.011 or reused_depth(same, fast)):
            continue
        if new["hedge_status"] == r["hedge_status"] and abs(float(new["locked_pnl"]) - float(r["locked_pnl"] or 0)) < 0.005:
            continue
        log.append(dict(reconciled_ts=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        placed_ts=r["placed_ts"], rest_id=r["rest_id"], player=r.get("player", ""),
                        first_fill_ts=r["first_fill_ts"], hedger_fill_ts=h0["fill_ts"],
                        seconds_fill_to_hedge=h0["seconds_fill_to_hedge"], old_status=r["hedge_status"],
                        new_status=new["hedge_status"], old_hedge_px=r["hedge_px_at_fill"], new_hedge_px=new["hedge_px_at_fill"],
                        old_locked_pnl=r["locked_pnl"], new_locked_pnl=new["locked_pnl"],
                        old_unhedged_qty=r["unhedged_qty"], new_unhedged_qty=new["unhedged_qty"]))
        r.update(new)
        changed[k] = new
    _apply(fills, changed, log)
    return log


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
    prev, nxt = _rows(PREV_PLANS), _rows(PLANS)
    # prev = two runs ago; nxt = last run, whose orders replaced prev's once they went out
    end = live_from(nxt[0]) if nxt else epoch(ts)
    res = resolve(prev, rows, ts, end) if prev else []
    filled = [r for r in res if float(r["filled"]) > 0]
    if filled:
        _write(FILLS, FILL_FIELDS, filled, "a")
    if res:
        _write(OUTCOMES, OUTCOME_FIELDS, res, "a")
    try:
        reconcile()
    except Exception as e:  # noqa: BLE001  (never let the comparison break the simulation)
        print(f"WARN hedger reconcile: {e}", file=sys.stderr)
    settle_bags()
    new = plan(rows, ts)
    if PLANS.exists():
        PLANS.replace(PREV_PLANS)
    else:
        PREV_PLANS.unlink(missing_ok=True)
    _write(PLANS, PLAN_FIELDS, new, "w")
    locked = sum(float(r["locked_pnl"]) for r in filled)
    _write(RUNS, RUN_FIELDS, [dict(ts_utc=ts, plans_resolved=len(res), plans_filled=len(filled),
                                   contracts_filled=round(sum(float(r["filled"]) for r in filled), 2),
                                   locked_pnl=round(locked, 2), new_plans=len(new))], "a")
    print(f"resting-order test: {len(filled)}/{len(res)} previous plans filled, locked ${locked:+.2f}; {len(new)} new plans")
    return new


def summary():
    """Conservative by design: open unhedged contracts are counted as a total loss until they settle, and fills resolved
    before the maker-fee fix have the Kalshi maker fee taken off."""
    if not FILLS.exists():
        print("no fills yet")
        return
    fl = list(csv.DictReader(FILLS.open()))
    runs = list(csv.DictReader(RUNS.open())) if RUNS.exists() else []
    bags = {(b["placed_ts"], b["rest_id"]): b for b in csv.DictReader(BAGS.open())} if BAGS.exists() else {}
    plans = sum(int(r["plans_resolved"]) for r in runs)
    locked = fee_fix = settled = open_worst = 0.0
    for r in fl:
        locked += float(r["locked_pnl"] or 0)
        if r["resolved_ts"] < MAKER_FEE_FIX_TS:
            fee_fix += kalshi_maker_fee(r["rest_id"], float(r["rest_px"]), max(1, math.ceil(float(r["filled"]))))
        q = float(r.get("unhedged_qty") or 0)
        if q > 0:
            b = bags.get((r["placed_ts"], r["rest_id"]))
            if b:
                settled += float(b["bag_pnl"])
            else:
                open_worst += q * float(r["rest_px"])
    print(f"{len(fl)} fills from {plans} plans ({len(fl) / max(plans, 1):.1%} fill rate)")
    for st in ("hedged", "PARTIAL", "UNHEDGED"):
        print(f"  {st:<9} {sum(r['hedge_status'] == st for r in fl)}")
    cons = locked - fee_fix + settled - open_worst
    print(f"locked on hedged contracts ${locked:+.2f}, maker-fee correction ${-fee_fix:+.2f}, settled bags ${settled:+.2f}, "
          f"open bags at worst case ${-open_worst:+.2f}")
    print(f"CONSERVATIVE TOTAL ${cons:+.2f}   (open bags valued at $0 until they settle)")


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
