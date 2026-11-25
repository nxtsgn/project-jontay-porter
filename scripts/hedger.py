"""Always-on hedger, PRACTICE MODE: sends no orders and needs no API keys.

The 5-minute collector only notices a resting Kalshi fill minutes after it happens, which is too slow to hedge. This
process runs all the time (on the free Oracle Cloud server, see deploy/oracle/README.md) and measures what hedging
within seconds would have done:

  every 20 s   fetch the price-data branch (commits and file lists only; the few CSVs read are downloaded on demand) and load EVERY resting order the planner made in its latest run
               (data/paper/maker_plans.csv), marking the ones the practice trader chose (trader_latest.csv, chosen=1).
               A newer run replaces (cancels) the older orders.
  every ~5 s   read Kalshi's public trade tape (one call, all markets) and match it to the watched markets. A taker trading into our side at or through
               our price is a fill (same rule as maker_sim). If the Polymarket US hedge price moves far enough that
               the trade would lose money, the order is pulled (cancel_if_hedge_moves). On a fill, immediately read the Polymarket US book and buy
               the hedge at no worse than plan price + max_hedge_slippage_cents (walking the book), then log the result.
  every ~5 s   also re-price the pairs closest to an instant-hedge gap and log how long each gap lasts
               (scripts/gap_scanner.py, gap_episodes.csv).
  every 10 min commit the logs to the hedger-data branch.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import fee, kalshi_maker_fee  # noqa: E402
from maker_sim import DEFAULT_DELAY_S, KALSHI, ORDER_LIFETIME_S, epoch, get, hedge_levels  # noqa: E402
from gap_scanner import GapScanner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SYNC_S, POLL_S, PUSH_S, UPDATE_S = 20, 5, 600, 1800  # sync: a new run's orders are known ~20 s sooner
STOP_BEFORE_START_S = 10 * 60
FIELDS = ["placed_ts", "chosen", "sport", "market_type", "player", "event_time", "rest_id", "rest_raw_side", "rest_px", "order_qty",
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


def rule(key, default):
    try:
        return json.loads((ROOT / "config" / "trading_rules.json").read_text())["per_hedge"].get(key, default)
    except Exception:  # noqa: BLE001
        return default


def slippage():
    return float(rule("max_hedge_slippage_cents", 3.0)) / 100


class Hedger:
    """Watches EVERY resting order the planner made each run (maker_plans.csv), not only the ones the practice trader
    picked (chosen=1, at the trader's size; others at the plan's size), so there are ~3x more fills to measure. Fills come
    from one call to Kalshi's public trade tape across all markets per poll, matched to the watched markets."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.log = self.data_dir / "fast_hedges.csv"
        self.run_ts = None
        self.orders = {}  # key -> order dict
        self.seen = {}  # trade id -> epoch seen (pruned after an hour)
        self.tape_t = None  # tape read up to here
        self.used = {}  # (hedge market, side, price) -> (time, contracts already taken at that level)
        self.rr = 0  # round-robin position for hedge-price checks
        self.pool = ThreadPoolExecutor(max_workers=4)
        if self.log.exists():
            rows = list(csv.DictReader(self.log.open()))
            if rows and list(rows[0]) != FIELDS:  # columns changed: rewrite the log with the new header
                with self.log.open("w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(rows)
            now = time.time()
            self.seen = {r["trade_id"]: now for r in rows}

    # ------------------------------------------------------------------ orders
    def sync(self):
        # shallow: only the newest price-data commit (the server keeps no history)
        # --filter=blob:none: without it every fetch downloaded the whole branch, ~22 MB price file included, and filled
        # the server's disk (38 GB of objects by 2026-11-21). `git show` then fetches only the CSVs it reads.
        if git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin",
               "+price-data:refs/remotes/origin/price-data").returncode != 0:
            print("WARN fetch failed; keeping current orders", file=sys.stderr)
            return
        plans = branch_csv("data/paper/maker_plans.csv")
        if not plans:
            return
        ts = plans[0]["placed_ts"]
        if ts == self.run_ts:
            return
        chosen = {}
        for r in branch_csv("data/paper/trader_latest.csv"):
            if r.get("strategy") == "maker" and r.get("leg") == "rest" and r.get("ts_utc") == ts:
                _, rid, side, hid = r["hedge_key"].split("|")
                chosen[(rid, side, hid)] = float(json.loads(r["payload"])["count"])
        watch_from = epoch(now_iso())
        delays = {r["ts_utc"]: float(r["seconds"]) for r in branch_csv("data/paper/order_delay.csv")}
        med = sorted(delays.values())[len(delays) // 2] if delays else DEFAULT_DELAY_S
        slip, placed = slippage(), epoch(ts)
        live = placed + delays.get(ts, med)  # when this run's orders would reach Kalshi (same as maker_sim)
        for o in self.orders.values():  # the new run's orders replace the old ones once they go out
            o["until"] = min(o["until"], live)
        for p in plans:
            k = (p["rest_id"], p["rest_norm_side"], p["hedge_id"])
            until = placed + ORDER_LIFETIME_S  # same window maker_sim counts fills in; the next run replaces them sooner
            if p.get("event_time"):
                until = min(until, epoch(p["event_time"]) - STOP_BEFORE_START_S)
            self.orders[(ts, *k)] = dict(
                p, placed_ts=ts, placed=placed, live_from=live, watch_from=watch_from, until=until, chosen=int(k in chosen),
                order_qty=chosen.get(k) or float(p["contracts"]), filled=0.0, rest_px=float(p["rest_px"]),
                hedge_limit=round(min(0.99, float(p["hedge_px_at_plan"]) + slip), 4))
        self.run_ts = ts
        # catch up on fills between the orders going out and this server hearing of them (~1 min, waiting on the
        # collector's commit). They are hedged at today's book and flagged late; the reconciliation ignores them.
        open_ = [o for o in self.orders.values() if o["filled"] < o["order_qty"] - 1e-9]
        back = self.tape_range(int(live) - 5, int(watch_from))  # Kalshi rejects fractional timestamps (HTTP 400)
        if back:
            self.match(back, open_)
        live = [o for o in self.orders.values() if o["until"] > watch_from]
        print(f"{now_iso()} watching {len(live)} planned orders ({sum(o['chosen'] for o in live)} picked by the trader) "
              f"from run {ts}")

    # ------------------------------------------------------------------ fills
    def tape(self, t):
        """Kalshi's public trades across ALL markets since the last read (a few seconds of overlap; ids dedupe)."""
        out = self.tape_range(int(self.tape_t or t - 30) - 5, int(t))
        if out is not None:
            self.tape_t = t
        return out

    def tape_range(self, since, t):
        out, cursor = [], None
        for _ in range(10):
            params = {"min_ts": since, "max_ts": t, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = get(f"{KALSHI}/markets/trades", params)
            if d is None:
                return None
            out += d.get("trades", [])
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    def poll(self):
        t = epoch(now_iso())
        for key, o in list(self.orders.items()):
            if o["until"] + 120 < t:  # done, with a little slack for the tape to catch up
                del self.orders[key]
        open_ = [o for o in self.orders.values() if o["filled"] < o["order_qty"] - 1e-9]
        if not open_:
            return
        trades = self.tape(t)
        if trades:
            self.match(trades, open_)
        cut = time.time() - 3600
        self.seen = {k: v for k, v in self.seen.items() if v > cut}
        # pull orders whose hedge moved: up to 30 per poll, round robin, books read in parallel
        if rule("cancel_if_hedge_moves", False):
            live = [o for o in open_ if o["until"] > t]
            if live:
                batch = [live[(self.rr + i) % len(live)] for i in range(min(30, len(live)))]
                self.rr = (self.rr + len(batch)) % max(1, len(live))
                for o, lv in zip(batch, self.pool.map(hedge_levels, batch)):
                    self.check_hedge(o, t, lv)

    def match(self, trades, open_):
        """Kalshi trades that would have filled our resting orders -> record and hedge each one."""
        by = {}
        for o in open_:
            by.setdefault(o["rest_id"], []).append(o)
        for tr in sorted((x for x in trades if x["ticker"] in by), key=lambda x: x["created_time"]):
            if tr["trade_id"] in self.seen:
                continue
            tt = epoch(tr["created_time"][:19] + "Z")
            for o in by[tr["ticker"]]:
                side = o["rest_raw_side"]
                price = float(tr["yes_price_dollars"] if side == "yes" else tr["no_price_dollars"])
                if not (o.get("live_from", o["placed"]) <= tt <= o["until"]) or tr.get("taker_side") == side or price > o["rest_px"] + 1e-9:
                    continue
                qty = min(float(tr.get("count_fp") or 0), o["order_qty"] - o["filled"])
                if qty <= 0:
                    continue
                self.seen[tr["trade_id"]] = time.time()
                o["filled"] += qty
                self.record(o, tr, qty)
                break

    def check_hedge(self, o, t, lv):
        """Pull the Kalshi order once the Polymarket US hedge price makes the trade lose money (same rule as maker_sim)."""
        if not lv:
            return
        px, rest, n = lv[0][0], o["rest_px"], max(1, int(o["order_qty"]))
        edge = 1 - rest - px - float(o["hedge_fee_coef"]) * px * (1 - px) - kalshi_maker_fee(o["rest_id"], rest, n) / n
        if edge < float(rule("cancel_below_edge_cents", 0.0)) / 100:
            o["until"] = t
            if o["chosen"]:
                print(f"{now_iso()} pulled {o['rest_id']} {o['rest_raw_side']} @ {rest}: hedge now {px} (edge {edge * 100:+.1f}c)")

    def record(self, o, tr, qty):
        seen = now_iso()
        left, got, spend = qty, 0.0, 0.0
        # practice hedges don't remove real depth, so remember what we "took" for a minute and don't take it twice
        t, self.used = time.time(), {k: v for k, v in self.used.items() if time.time() - v[0] < 60}
        side = (o["hedge_id"], (o["hedge_norm_side"] == "yes") != bool(int(o.get("hedge_flipped") or 0)))
        for px, avail in hedge_levels(o):  # the live Polymarket US book, right now
            if left <= 1e-9 or px > o["hedge_limit"] + 1e-9:
                break
            key = (*side, px)
            take = min(left, max(0.0, avail - self.used.get(key, (t, 0.0))[1]))
            if take > 0:
                self.used[key] = (t, self.used.get(key, (t, 0.0))[1] + take)
            got, spend, left = got + take, spend + take * px, left - take
        hedged = now_iso()
        n = max(1, math.ceil(qty))
        kfee = kalshi_maker_fee(o["rest_id"], o["rest_px"], n)
        avg = spend / got if got else None
        pfee = fee("polymarket_us", float(o["hedge_fee_coef"]), avg, max(1, math.ceil(got))) if got else 0.0
        locked = (1 - o["rest_px"] - avg) * got - pfee - kfee * (got / qty) if got else -kfee
        fill_ts = tr["created_time"]
        row = dict(placed_ts=o["placed_ts"], chosen=o.get("chosen", 1), sport=o["sport"], market_type=o["market_type"], player=o.get("player", ""),
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
        logs = [n for n in ("fast_hedges.csv", "gap_episodes.csv") if (d / n).exists()]
        if not (d / ".git").exists() or not logs:
            return
        git("add", *logs, cwd=d)
        if git("diff", "--cached", "--quiet", cwd=d).returncode != 0:
            git("commit", "-qm", f"hedger {now_iso()}", cwd=d)
            r = git("push", "-q", "origin", "HEAD:hedger-data", cwd=d)
            if r.returncode != 0:
                print(f"WARN push failed: {r.stderr.strip()[:200]}", file=sys.stderr)


def code_changed():
    """Pull the latest code from main; True if it changed (systemd then restarts us on the new code)."""
    before = git("rev-parse", "HEAD").stdout.strip()
    if git("fetch", "-q", "--depth", "1", "origin", "+main:refs/remotes/origin/main").returncode == 0:
        git("reset", "-q", "--hard", "origin/main")  # shallow clone: a pull can't fast-forward, so jump to main
    git("prune", "--expire=2.hours.ago")  # objects from older syncs are unreachable once the shallow refs move on
    return git("rev-parse", "HEAD").stdout.strip() != before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="worktree of the hedger-data branch (the log is committed there)")
    ap.add_argument("--once", action="store_true", help="one sync and one poll, then exit (testing)")
    a = ap.parse_args()
    h = Hedger(a.data_dir)
    gaps = GapScanner(a.data_dir)  # how long instant-hedge gaps last (see scripts/gap_scanner.py)
    last = dict(sync=0.0, push=time.time(), update=time.time())
    print(f"{now_iso()} hedger started (practice mode, sends no orders)")
    while True:
        t = time.time()
        if t - last["sync"] >= SYNC_S:
            h.sync()
            gaps.sync(branch_csv)
            last["sync"] = t
        h.poll()
        try:
            gaps.poll()
        except Exception as e:  # noqa: BLE001  (the scanner must never stop the hedger)
            print(f"WARN gap scanner: {e}", file=sys.stderr)
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
