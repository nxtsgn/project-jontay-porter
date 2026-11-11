"""Trading engine, PRACTICE MODE. Applies config/trading_rules.json to each run's opportunities and builds the exact orders
it would send, but sends nothing. Runs inside the collector (collect_prices.py calls cycle()) every ~5 minutes.

What it does each run:
  1. Kill switch / loss stops: TRADING_ENABLED=false (GitHub repo variable), daily loss stop, total drawdown kill.
  2. Taker-taker hedges (from find_arbs): resize to the rules' caps, re-check profit after fees, check open-hedge / per-game /
     prop / per-venue capital limits, then build both IOC orders. Accepted hedges go into the practice ledger as if both
     legs filled at the quoted price (optimistic; depth was checked when the hedge was found).
  3. Resting Kalshi orders (from maker_sim): same checks, build the post-only Kalshi order plus the Polymarket US hedge
     order the bot would send on a fill. Fills themselves are tracked by maker_sim, not here.
  4. Grade ledger hedges once both markets settle, so the loss stops use real practice results.
Files (data/paper/): trader_orders.csv (every payload), trader_ledger.csv (practice positions), trader_runs.csv (one line
per run, including how many candidates each rule rejected).

Live trading is refused by live_allowed() until: rules approved + mode "live" + TRADING_ENABLED=true + all four API keys
present + Polymarket US NO-price convention verified + the always-on runner exists (the 5-minute collector is too slow to
hedge a resting fill within the 2-minute limit).

    python scripts/trader.py --status        # rules, gate, ledger summary
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import venues  # noqa: E402
from find_arbs import f, fee, kalshi_maker_fee  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "config" / "trading_rules.json"
PAPER = ROOT / "data" / "paper"
ORDERS, LEDGER, RUNS = PAPER / "trader_orders.csv", PAPER / "trader_ledger.csv", PAPER / "trader_runs.csv"
ORDER_FIELDS = ["ts_utc", "mode", "strategy", "hedge_key", "leg", "venue", "market_id", "payload"]
LEDGER_FIELDS = ["ts_utc", "hedge_key", "sport", "game_date", "teams", "market_type", "player", "event_time",
                 "a_venue", "a_id", "a_flipped", "a_price", "b_venue", "b_id", "b_flipped", "b_price",
                 "contracts", "cost", "fees", "net_per_contract", "net_total", "a_settle", "b_settle", "payout", "pnl"]
RUN_FIELDS = ["ts_utc", "mode", "status", "taker_candidates", "taker_accepted", "maker_candidates", "maker_accepted",
              "open_hedges", "deployed_kalshi", "deployed_pmus", "realized_today", "realized_total", "rejections"]
SETTLE_AFTER = timedelta(hours=4)
LIVE_RUNNER_BUILT = False  # the always-on process that can hedge within max_unhedged_minutes


def load_rules(path=RULES):
    r = json.loads(Path(path).read_text())
    for k in ("bankroll", "per_hedge", "portfolio", "loss_limits", "allowed", "kill_switch"):
        if k not in r:
            raise ValueError(f"trading_rules.json is missing '{k}'")
    if r["mode"] not in ("paper", "dry_run", "live"):
        raise ValueError(f"unknown mode {r['mode']!r}")
    return r


def kill_switch_off():
    """True when someone has turned trading off (repo variable TRADING_ENABLED=false)."""
    return os.environ.get("TRADING_ENABLED", "").strip().lower() == "false"


def live_allowed(rules):
    """True, or the first reason live trading is refused."""
    checks = [
        (rules.get("approved") is True, "rules not approved (config/trading_rules.json approved=false)"),
        (rules.get("mode") == "live", f"mode is {rules.get('mode')!r}, not 'live'"),
        (os.environ.get("TRADING_ENABLED", "").strip().lower() == "true", "TRADING_ENABLED is not 'true'"),
        (not venues.keys_present(), f"missing API keys: {', '.join(venues.keys_present())}"),
        (venues.PMUS_SHORT_PRICE_IS_YES_PRICE_VERIFIED, "Polymarket US NO-order price convention not verified"),
        (LIVE_RUNNER_BUILT, "always-on runner not built (5-minute collector can't hedge within 2 minutes)"),
    ]
    for ok, why in checks:
        if not ok:
            return why
    return True


# ------------------------------------------------------------------------------------------------------ helpers
def market_kind(market_type, a_line=None, b_line=None):
    if market_type == "margin":
        return "moneyline" if (a_line in (0, 0.0) or b_line in (0, 0.0)) else "spread"
    if market_type == "totals":
        return "total"
    return "prop"


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _read(path):
    return list(csv.DictReader(path.open())) if path.exists() else []


def _write(path, fields, rows, mode="a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = mode == "w" or not path.exists()
    with path.open(mode, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


class Book:
    """Practice positions and capital, rebuilt from the ledger each run."""

    def __init__(self, rules, now):
        self.rules, self.now = rules, now
        self.ledger = _read(LEDGER)
        self.open = [h for h in self.ledger if h.get("pnl") in ("", None) and _ts(h["event_time"]) + SETTLE_AFTER > now]
        self.deployed = Counter()
        for h in self.open:
            c = float(h["contracts"])
            self.deployed[h["a_venue"]] += c * float(h["a_price"])
            self.deployed[h["b_venue"]] += c * float(h["b_price"])
        self.per_game = Counter((h["sport"], h["game_date"], h["teams"]) for h in self.open)
        self.props = sum(market_kind(h["market_type"]) == "prop" for h in self.open)
        self.keys = {h["hedge_key"] for h in self.open}
        today = now.strftime("%Y-%m-%d")
        graded = [h for h in self.ledger if h.get("pnl") not in ("", None)]
        self.realized_total = sum(float(h["pnl"]) for h in graded)
        self.realized_today = sum(float(h["pnl"]) for h in graded if h["ts_utc"].startswith(today))

    def room(self, venue):
        b = self.rules["bankroll"]
        start = b["kalshi_start"] if venue == "kalshi" else b["polymarket_us_start"]
        return max(0.0, start - b["reserve_per_venue"] - self.deployed[venue])

    def add(self, venue_costs, game, prop, key):
        for v, c in venue_costs:
            self.deployed[v] += c
        self.per_game[game] += 1
        self.props += prop
        self.keys.add(key)


def common_checks(rules, book, o, kind, now, rej):
    al, pf = rules["allowed"], rules["portfolio"]
    start = _ts(o["event_time"])
    if o["sport"] not in al["sports"]:
        return rej.update(["sport not allowed"])
    if kind not in al["market_types"]:
        return rej.update(["market type not allowed"])
    if start - now > timedelta(hours=al["enter_only_if_game_starts_within_hours"]):
        return rej.update(["game too far away"])
    if start - now < timedelta(minutes=al["stop_new_entries_minutes_before_start"]):
        return rej.update(["too close to start / started"])
    if len(book.open) >= pf["max_open_hedges"]:
        return rej.update(["max open hedges"])
    if book.per_game[(o["sport"], o["game_date"], o["teams"])] >= pf["max_open_per_game"]:
        return rej.update(["max per game"])
    if kind == "prop" and book.props >= pf["max_prop_hedges_open"]:
        return rej.update(["max prop hedges"])
    return True


def size(rules, book, kind, legs):
    """Largest contract count within per-leg caps and each venue's remaining capital. legs = [(venue, price, depth)]."""
    ph, pf = rules["per_hedge"], rules["portfolio"]
    cap = pf["max_cost_per_prop_leg"] if kind == "prop" else ph["max_cost_per_leg"]
    c = ph["max_contracts"]
    need = Counter()
    for v, px, depth in legs:
        need[v] += px
        c = min(c, math.floor(cap / px + 1e-9), math.floor(depth + 1e-9) if depth is not None else c)
    for v, px_sum in need.items():
        c = min(c, math.floor(book.room(v) / px_sum + 1e-9))
    return max(c, 0)


# ------------------------------------------------------------------------------------------------------ strategies
def taker(rules, book, opps, ts, now, rej):
    out = []
    for o in opps:
        kind = market_kind(o["market_type"], f(o["a_line"]), f(o["b_line"]))
        key = f"T|{o['a_id']}|{o['b_id']}"
        if key in book.keys:
            rej.update(["already holding this hedge"])
            continue
        if common_checks(rules, book, o, kind, now, rej) is not True:
            continue
        pa, pb = float(o["a_price"]), float(o["b_price"])
        c = size(rules, book, kind, [(o["a_venue"], pa, f(o.get("a_depth"))), (o["b_venue"], pb, f(o.get("b_depth")))])
        if c < rules["per_hedge"]["min_contracts"]:
            rej.update(["too few contracts after caps"])
            continue
        fees = fee(o["a_venue"], f(o.get("a_fee_coef")) or (0.07 if o["a_venue"] == "kalshi" else 0.095), pa, c) + \
            fee(o["b_venue"], f(o.get("b_fee_coef")) or (0.07 if o["b_venue"] == "kalshi" else 0.095), pb, c)
        net = 1 - pa - pb - fees / c
        if net * 100 < rules["per_hedge"]["min_net_edge_taker_cents"]:
            rej.update(["edge below minimum after resize"])
            continue
        legs = [("a", venues.build_leg(o["a_venue"], o["a_id"], "yes", o["a_flipped"], pa, c)),
                ("b", venues.build_leg(o["b_venue"], o["b_id"], "no", o["b_flipped"], pb, c))]
        out.append(dict(key=key, legs=legs, ledger=dict(
            ts_utc=ts, hedge_key=key, **{k: o[k] for k in ("sport", "game_date", "teams", "market_type", "player", "event_time",
                                                           "a_venue", "a_id", "a_flipped", "b_venue", "b_id", "b_flipped")},
            a_price=pa, b_price=pb, contracts=c, cost=round(pa + pb, 4), fees=round(fees, 2),
            net_per_contract=round(net, 4), net_total=round(net * c, 2))))
        book.add([(o["a_venue"], c * pa), (o["b_venue"], c * pb)], (o["sport"], o["game_date"], o["teams"]), kind == "prop", key)
        book.open.append(out[-1]["ledger"])
    return out


def maker(rules, book, plans, ts, now, rej):
    out = []
    expires = (now + timedelta(minutes=6)).timestamp()  # replaced next run, so never left stale on the book
    for o in plans:
        kind = market_kind(o["market_type"], f(o["rest_line"]), f(o["hedge_line"]))
        key = f"M|{o['rest_id']}|{o['rest_norm_side']}|{o['hedge_id']}"
        if common_checks(rules, book, o, kind, now, rej) is not True:
            continue
        rp, hp = float(o["rest_px"]), float(o["hedge_px_at_plan"])
        c = size(rules, book, kind, [("kalshi", rp, None), ("polymarket_us", hp, float(o["contracts"]))])
        if c < rules["per_hedge"]["min_contracts"]:
            rej.update(["too few contracts after caps"])
            continue
        edge = 1 - rp - hp - (fee("polymarket_us", float(o["hedge_fee_coef"]), hp, c) + kalshi_maker_fee(o["rest_id"], rp, c)) / c
        if edge * 100 < rules["per_hedge"]["min_net_edge_maker_cents"]:
            rej.update(["edge below minimum after resize"])
            continue
        worst_hedge = min(0.99, hp + rules["per_hedge"]["max_hedge_slippage_cents"] / 100)
        legs = [("rest", venues.kalshi_order(o["rest_id"], o["rest_raw_side"], rp, c, resting=True, expires_ts=expires)),
                ("hedge_on_fill", venues.build_leg("polymarket_us", o["hedge_id"], o["hedge_norm_side"], o["hedge_flipped"],
                                                   worst_hedge, c))]
        out.append(dict(key=key, legs=legs))
        # a resting order ties up Kalshi capital while it sits; the hedge capital is needed only on a fill
        book.deployed["kalshi"] += c * rp
    return out


# ------------------------------------------------------------------------------------------------------ grading
def grade_ledger(now):
    rows = _read(LEDGER)
    if not rows:
        return
    from grade import venue_yes_settlement
    changed = False
    for h in rows:
        if h.get("pnl") not in ("", None) or _ts(h["event_time"]) + SETTLE_AFTER > now:
            continue
        sa, sb = venue_yes_settlement(h["a_venue"], h["a_id"]), venue_yes_settlement(h["b_venue"], h["b_id"])
        if sa is None or sb is None:
            continue
        ya = 1 - sa if h["a_flipped"] in ("1", 1) else sa
        yb = 1 - sb if h["b_flipped"] in ("1", 1) else sb
        payout = ya + (1 - yb)
        h.update(a_settle=sa, b_settle=sb, payout=round(payout, 4),
                 pnl=round((payout - float(h["cost"])) * float(h["contracts"]) - float(h["fees"]), 2))
        changed = True
    if changed:
        _write(LEDGER, LEDGER_FIELDS, rows, "w")


# ------------------------------------------------------------------------------------------------------ one run
def cycle(taker_opps, maker_plans, ts, rules=None):
    rules = rules or load_rules()
    now = _ts(ts)
    mode = rules["mode"] if live_allowed(rules) is True else ("paper" if rules["mode"] == "paper" else "dry_run")
    grade_ledger(now)
    book = Book(rules, now)
    rej, status = Counter(), "ok"
    ll = rules["loss_limits"]
    if kill_switch_off():
        status = "KILLED (TRADING_ENABLED=false): would cancel all open orders on both venues"
    elif -book.realized_total >= ll["total_drawdown_kill"]:
        status = f"KILLED (drawdown ${-book.realized_total:.2f}): would cancel all open orders and wait for a reset"
    elif -book.realized_today >= ll["daily_loss_stop"]:
        status = f"DAILY STOP (lost ${-book.realized_today:.2f} today)"
    t_acc, m_acc = [], []
    if status == "ok":
        t_acc = taker(rules, book, taker_opps or [], ts, now, rej)
        if "kalshi_maker_pmus_hedge" in rules["allowed"]["strategies"]:
            m_acc = maker(rules, book, maker_plans or [], ts, now, rej)
    orders = [dict(ts_utc=ts, mode=mode, strategy="taker" if a["key"][0] == "T" else "maker", hedge_key=a["key"], leg=leg,
                   venue=p["venue"], market_id=p["body"].get("ticker") or p["body"].get("marketSlug"), payload=json.dumps(p["body"]))
              for a in t_acc + m_acc for leg, p in a["legs"]]
    if orders:
        _write(ORDERS, ORDER_FIELDS, orders)
    if t_acc:
        _write(LEDGER, LEDGER_FIELDS, [a["ledger"] for a in t_acc])
    _write(RUNS, RUN_FIELDS, [dict(ts_utc=ts, mode=mode, status=status, taker_candidates=len(taker_opps or []),
                                   taker_accepted=len(t_acc), maker_candidates=len(maker_plans or []), maker_accepted=len(m_acc),
                                   open_hedges=len(book.open), deployed_kalshi=round(book.deployed["kalshi"], 2),
                                   deployed_pmus=round(book.deployed["polymarket_us"], 2),
                                   realized_today=round(book.realized_today, 2), realized_total=round(book.realized_total, 2),
                                   rejections=json.dumps(dict(rej)))])
    print(f"trader [{mode}] {status}: taker {len(t_acc)}/{len(taker_opps or [])}, resting {len(m_acc)}/{len(maker_plans or [])} "
          f"accepted; open {len(book.open)}; Kalshi ${book.deployed['kalshi']:.2f} / PM-US ${book.deployed['polymarket_us']:.2f} "
          f"in use; rejected {dict(rej.most_common(4))}")
    return t_acc, m_acc


def status():
    rules = load_rules()
    print(f"rules: approved={rules['approved']} mode={rules['mode']}")
    print(f"live trading: {live_allowed(rules)}")
    led, runs = _read(LEDGER), _read(RUNS)
    graded = [h for h in led if h.get("pnl") not in ("", None)]
    print(f"practice ledger: {len(led)} hedges, {len(graded)} graded, expected ${sum(float(h['net_total']) for h in graded):+.2f}, "
          f"realized ${sum(float(h['pnl']) for h in graded):+.2f}")
    if runs:
        rej = Counter()
        for r in runs:
            rej.update(json.loads(r["rejections"] or "{}"))
        print(f"{len(runs)} runs; resting orders built {sum(int(r['maker_accepted']) for r in runs)}; "
              f"most common rejections: {dict(rej.most_common(6))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.parse_args()
    status()
