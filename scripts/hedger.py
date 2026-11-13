"""Always-on hedger, PRACTICE MODE: sends no orders and needs no API keys.

The 5-minute collector only notices a resting Kalshi fill minutes after it happens, which is too slow to hedge. This
process runs all the time (on the free Oracle Cloud server, see deploy/oracle/README.md) and measures what hedging
within seconds would have done:

  every 60 s   fetch the price-data branch and load the resting orders the practice trader chose in its latest run
               (data/paper/trader_latest.csv + maker_plans.csv). A newer run replaces (cancels) the older orders.
  every ~5 s   read Kalshi's public trade tape for each of those markets. A taker trading into our side at or through
               our price is a fill (same rule as maker_sim). On a fill, immediately read the Polymarket US book and buy
               the hedge at no worse than plan price + max_hedge_slippage_cents (walking the book), then log the result.
  every 10 min commit the log to the hedger-data branch.

Log: fast_hedges.csv in the hedger-data worktree, one row per fill: when it happened, when we saw it, how long the hedge
took, how much got hedged and at what price, how much was left unhedged, and the locked profit (after both venues' fees).
Fills that happened before the hedger was watching an order (it only learns of orders after the collector commits them)
are logged with late=1 and should be left out of speed comparisons; a live runner places its own orders, so it has no
such gap.

When real trading is approved, the live version swaps the trade-tape polling for Kalshi's authenticated fill feed
(websocket, ~instant) and the logging for venues.send(); the order checks in trader.live_allowed() still apply.

    python scripts/hedger.py --data-dir ~/hedger-data            # run forever
    python scripts/hedger.py --data-dir /tmp/hd --once           # one sync + one poll, for testing
"""
import argparse
import csv
import io
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import fee, kalshi_maker_fee  # noqa: E402
from maker_sim import KALSHI, epoch, get, hedge_levels  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SYNC_S, POLL_S, PUSH_S, UPDATE_S = 60, 5, 600, 1800
STOP_BEFORE_START_S = 10 * 60
FIELDS = ["placed_ts", "sport", "market_type", "player", "event_time", "rest_id", "rest_raw_side", "rest_px", "order_qty",
          "hedge_id", "hedge_norm_side", "hedge_plan_px", "hedge_limit_px", "trade_id", "fill_ts", "seen_ts", "hedged_ts",
          "seconds_fill_to_hedge", "late", "fill_qty", "hedged_qty", "hedge_avg_px", "unhedged_qty", "kalshi_fee",
          "pmus_fee", "locked_pnl", "status"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def branch_csv(path):
    r = git("show", f"origin/price-data:{path}")
    return list(csv.DictReader(io.StringIO(r.stdout))) if r.returncode == 0 else []


def slippage():
    try:
        return json.loads((ROOT / "config" / "trading_rules.json").read_text())["per_hedge"]["max_hedge_slippage_cents"] / 100
    except Exception:  # noqa: BLE001
        return 0.03


class Hedger:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.log = self.data_dir / "fast_hedges.csv"
        self.run_ts = None
        self.orders = {}  # key -> order dict
        self.seen = set()  # trade ids already counted
        if self.log.exists():
            self.seen = {r["trade_id"] for r in csv.DictReader(self.log.open())}

    # ------------------------------------------------------------------ orders
    def sync(self):
        if git("fetch", "-q", "origin", "price-data").returncode != 0:
            print("WARN fetch failed; keeping current orders", file=sys.stderr)
            return
        latest = branch_csv("data/paper/trader_latest.csv")
        rest = [r for r in latest if r["strategy"] == "maker" and r["leg"] == "rest"]
        if not rest:
            return
        ts = rest[0]["ts_utc"]
        if ts == self.run_ts:
            return
        plans = {(p["rest_id"], p["rest_norm_side"], p["hedge_id"]): p for p in branch_csv("data/paper/maker_plans.csv")}
        watch_from = epoch(now_iso())
        for o in self.orders.values():  # the new run replaces the old orders
            o["until"] = min(o["until"], watch_from)
        slip = slippage()
        for r in rest:
            _, rid, side, hid = r["hedge_key"].split("|")
            p = plans.get((rid, side, hid))
            if not p:
                continue
            body = json.loads(r["payload"])
            placed = epoch(ts)
            until = int(body.get("expiration_time") or placed + 6 * 60)
            if p.get("event_time"):
                until = min(until, epoch(p["event_time"]) - STOP_BEFORE_START_S)
            self.orders[(ts, r["hedge_key"])] = dict(
                p, placed_ts=ts, placed=placed, watch_from=watch_from, until=until, order_qty=float(body["count"]),
                filled=0.0, rest_px=float(p["rest_px"]), hedge_limit=round(min(0.99, float(p["hedge_px_at_plan"]) + slip), 4))
        self.run_ts = ts
        print(f"{now_iso()} watching {sum(o['until'] > watch_from for o in self.orders.values())} resting orders from run {ts}")

    # ------------------------------------------------------------------ fills
    def poll(self):
        t = epoch(now_iso())
        for key, o in list(self.orders.items()):
            if o["until"] + 120 < t:  # done, with a little slack for the tape to catch up
                del self.orders[key]
                continue
            if o["filled"] >= o["order_qty"] - 1e-9:
                continue
            d = get(f"{KALSHI}/markets/trades", {"ticker": o["rest_id"], "min_ts": o["placed"], "max_ts": min(t, o["until"]),
                                                  "limit": 200})
            if d is None:
                continue
            side = o["rest_raw_side"]
            for tr in sorted(d.get("trades", []), key=lambda x: x["created_time"]):
                if tr["trade_id"] in self.seen:
                    continue
                price = float(tr["yes_price_dollars"] if side == "yes" else tr["no_price_dollars"])
                if tr.get("taker_side") == side or price > o["rest_px"] + 1e-9:
                    continue
                self.seen.add(tr["trade_id"])
                qty = min(float(tr.get("count_fp") or 0), o["order_qty"] - o["filled"])
                if qty <= 0:
                    continue
                o["filled"] += qty
                self.record(o, tr, qty)
            time.sleep(0.15)  # stay well under Kalshi's read rate limit

    def record(self, o, tr, qty):
        seen = now_iso()
        left, got, spend = qty, 0.0, 0.0
        for px, avail in hedge_levels(o):  # the live Polymarket US book, right now
            if left <= 1e-9 or px > o["hedge_limit"] + 1e-9:
                break
            take = min(left, avail)
            got, spend, left = got + take, spend + take * px, left - take
        hedged = now_iso()
        n = max(1, math.ceil(qty))
        kfee = kalshi_maker_fee(o["rest_id"], o["rest_px"], n)
        avg = spend / got if got else None
        pfee = fee("polymarket_us", float(o["hedge_fee_coef"]), avg, max(1, math.ceil(got))) if got else 0.0
        locked = (1 - o["rest_px"] - avg) * got - pfee - kfee * (got / qty) if got else -kfee
        fill_ts = tr["created_time"]
        row = dict(placed_ts=o["placed_ts"], sport=o["sport"], market_type=o["market_type"], player=o.get("player", ""),
                   event_time=o.get("event_time", ""), rest_id=o["rest_id"], rest_raw_side=o["rest_raw_side"],
                   rest_px=o["rest_px"], order_qty=o["order_qty"], hedge_id=o["hedge_id"],
                   hedge_norm_side=o["hedge_norm_side"], hedge_plan_px=o["hedge_px_at_plan"], hedge_limit_px=o["hedge_limit"],
                   trade_id=tr["trade_id"], fill_ts=fill_ts, seen_ts=seen, hedged_ts=hedged,
                   seconds_fill_to_hedge=epoch(hedged) - epoch(fill_ts[:19] + "Z"),
                   late=int(epoch(fill_ts[:19] + "Z") < o["watch_from"]), fill_qty=round(qty, 2), hedged_qty=round(got, 2),
                   hedge_avg_px=round(avg, 4) if avg else "", unhedged_qty=round(left, 2), kalshi_fee=kfee,
                   pmus_fee=pfee, locked_pnl=round(locked, 2),
                   status="hedged" if left <= 1e-9 else ("PARTIAL" if got else "UNHEDGED"))
        new = not self.log.exists()
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        print(f"{seen} fill {qty:g} @ {o['rest_px']} {o['rest_id']} -> {row['status']} {got:g} @ {row['hedge_avg_px']} "
              f"in {row['seconds_fill_to_hedge']}s, locked ${row['locked_pnl']:+.2f}{' (late)' if row['late'] else ''}")

    # ------------------------------------------------------------------ housekeeping
    def push(self):
        d = self.data_dir
        if not (d / ".git").exists() or not self.log.exists():
            return
        git("add", "fast_hedges.csv", cwd=d)
        if git("diff", "--cached", "--quiet", cwd=d).returncode != 0:
            git("commit", "-qm", f"hedger {now_iso()}", cwd=d)
            r = git("push", "-q", "origin", "HEAD:hedger-data", cwd=d)
            if r.returncode != 0:
                print(f"WARN push failed: {r.stderr.strip()[:200]}", file=sys.stderr)


def code_changed():
    """Pull the latest code from main; True if it changed (systemd then restarts us on the new code)."""
    before = git("rev-parse", "HEAD").stdout.strip()
    git("pull", "-q", "--ff-only", "origin", "main")
    return git("rev-parse", "HEAD").stdout.strip() != before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="worktree of the hedger-data branch (the log is committed there)")
    ap.add_argument("--once", action="store_true", help="one sync and one poll, then exit (testing)")
    a = ap.parse_args()
    h = Hedger(a.data_dir)
    last = dict(sync=0.0, push=time.time(), update=time.time())
    print(f"{now_iso()} hedger started (practice mode, sends no orders)")
    while True:
        t = time.time()
        if t - last["sync"] >= SYNC_S:
            h.sync()
            last["sync"] = t
        h.poll()
        if a.once:
            return
        if t - last["push"] >= PUSH_S:
            h.push()
            last["push"] = t
        if t - last["update"] >= UPDATE_S:
            last["update"] = t
            if code_changed():
                h.push()
                print("code updated; exiting so systemd restarts on the new version")
                return
        time.sleep(max(0.0, POLL_S - (time.time() - t)))


if __name__ == "__main__":
    main()
