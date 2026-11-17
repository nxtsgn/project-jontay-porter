"""Gap scanner, PRACTICE MODE (sends no orders, needs no keys): how long do instant-hedge gaps really last?

The 5-minute collector sees thousands of "buy both sides for under $1" gaps, but almost none are still there 5 minutes
later, so we can't tell a gap that lasted 1 second from one that lasted 4 minutes. This runs inside the always-on hedger
on the Oracle server (scripts/hedger.py) and re-prices the closest pairs every few seconds:

  every collector run   collect_prices.py writes data/paper/gap_watch.csv: the WATCH_N cross-venue pairs closest to a
                        gap (within 3c, after fees) in that snapshot. write_watchlist() below.
  every ~60 s           the scanner loads that watch list (sync)
  every ~5 s            one batched Kalshi call + the Polymarket US book for each watched market (in parallel), then for
                        each pair: net per contract = 1 - YES price - NO price - fees. A gap "opens" when net > 0; at
                        that moment both books' depth at those prices is fetched. It "closes" when net <= 0 again (or the
                        pair leaves the watch list / its game is about to start).

One row per gap in gap_episodes.csv (hedger-data branch): when it opened and closed, how many seconds it lasted (to within
one scan cycle, logged as resolution_s), the best net, contracts available, the profit at your per-leg limit, and whether
it's plausible (net <= max_plausible_edge_cents; bigger is almost always a bad quote). progress_report.py summarizes it.
"""
import csv
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_arbs import depth, fee, hedges  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "data" / "paper" / "gap_watch.csv"
WATCH_N, WATCH_MIN_NET = 40, -0.03
WATCH_FIELDS = ["ts_utc", "sport", "game_date", "teams", "market_type", "player", "event_time",
                "a_venue", "a_id", "a_line", "a_flipped", "a_fee_coef", "b_venue", "b_id", "b_line", "b_flipped",
                "b_fee_coef", "net_per_contract"]
EP_FIELDS = ["open_ts", "close_ts", "seconds_open", "cycles_open", "resolution_s", "close_reason", "sport", "market_type",
             "player", "teams", "event_time", "a_venue", "a_id", "a_price_open", "b_venue", "b_id", "b_price_open",
             "net_open", "net_best", "depth_a", "depth_b", "contracts", "profit_at_limit", "plausible"]
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us"
UA = {"User-Agent": "project-jontay-porter/0.2 (read-only research)"}
NOMINAL = 10  # fees are computed at this size per leg when deciding whether a gap is open
STOP_BEFORE_START = timedelta(minutes=10)


def now_utc():
    return datetime.now(timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def _get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def rules():
    try:
        r = json.loads((ROOT / "config" / "trading_rules.json").read_text())
        return r["per_hedge"]["max_cost_per_leg"], r["per_hedge"]["max_contracts"], r["per_hedge"].get("max_plausible_edge_cents", 15) / 100
    except Exception:  # noqa: BLE001
        return 10.0, 25, 0.15


# ------------------------------------------------------------------------------------------------ collector side
def write_watchlist(rows, ts, path=WATCH):
    """Called by collect_prices.py each run: the cross-venue pairs closest to a gap, for the server to watch."""
    hs = [h for h in hedges(rows, ts, contracts=NOMINAL, min_net=WATCH_MIN_NET) if h["a_venue"] != h["b_venue"]][:WATCH_N]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=WATCH_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(hs)
    return len(hs)


# ------------------------------------------------------------------------------------------------ server side
class GapScanner:
    def __init__(self, data_dir):
        self.log = Path(data_dir) / "gap_episodes.csv"
        self.watch, self.open, self.cycle_s = [], {}, []
        self.pool = ThreadPoolExecutor(max_workers=4)

    def sync(self, read_branch_csv):
        rows = read_branch_csv("data/paper/gap_watch.csv")
        t = now_utc()
        self.watch = [r for r in rows if r.get("event_time") and _ts(r["event_time"]) - STOP_BEFORE_START > t]
        keys = {self.key(r) for r in self.watch}
        for k in [k for k in self.open if k not in keys]:
            self.close(k, t, "left watch list")

    @staticmethod
    def key(r):
        return f"{r['a_id']}|{r['b_id']}"

    # quotes ---------------------------------------------------------------------------------------------------
    def quotes(self):
        """{market id: (raw YES ask, raw NO ask)} for every watched market, right now."""
        k_ids = sorted({r[f"{s}_id"] for r in self.watch for s in "ab" if r[f"{s}_venue"] == "kalshi"})
        p_ids = sorted({r[f"{s}_id"] for r in self.watch for s in "ab" if r[f"{s}_venue"] == "polymarket_us"})
        q = {}
        for i in range(0, len(k_ids), 50):
            d = _get(f"{KALSHI}/markets", {"tickers": ",".join(k_ids[i:i + 50]), "limit": 100}) or {}
            for m in d.get("markets", []):
                ya, na = float(m.get("yes_ask_dollars") or 0), float(m.get("no_ask_dollars") or 0)
                q[m["ticker"]] = (ya if 0 < ya < 1 else None, na if 0 < na < 1 else None)

        def pm(mid):
            md = (_get(f"{PMUS}/v1/markets/{mid}/book") or {}).get("marketData") or {}
            offers = [float(x["px"]["value"]) for x in md.get("offers") or []]
            bids = [float(x["px"]["value"]) for x in md.get("bids") or []]
            return mid, (min(offers) if offers else None, round(1 - max(bids), 4) if bids else None)
        for mid, v in self.pool.map(pm, p_ids):
            q[mid] = v
        return q

    @staticmethod
    def price(q, venue_id, side_yes, flipped):
        raw_yes = side_yes != bool(int(float(flipped or 0)))
        v = q.get(venue_id)
        return None if v is None else v[0 if raw_yes else 1]

    # episodes -------------------------------------------------------------------------------------------------
    def poll(self):
        if not self.watch:
            return
        t0 = time.time()
        q = self.quotes()
        t = now_utc()
        cap_leg, max_c, plausible = rules()
        for r in self.watch:
            k = self.key(r)
            pa = self.price(q, r["a_id"], True, r["a_flipped"])
            pb = self.price(q, r["b_id"], False, r["b_flipped"])
            if pa is None or pb is None:
                net = None
            else:
                fees = fee(r["a_venue"], float(r["a_fee_coef"]), pa, NOMINAL) + fee(r["b_venue"], float(r["b_fee_coef"]), pb, NOMINAL)
                net = 1 - pa - pb - fees / NOMINAL
            if net is not None and net > 0:
                e = self.open.get(k)
                if e is None:
                    da = depth(r["a_venue"], r["a_id"], True, r["a_flipped"], pa)
                    db = depth(r["b_venue"], r["b_id"], False, r["b_flipped"], pb)
                    c = max(0, math.floor(min(da or 0, db or 0, max_c, cap_leg / pa, cap_leg / pb)))
                    self.open[k] = dict(r, open_t=t, last_t=t, cycles=1, a_price_open=pa, b_price_open=pb,
                                        net_open=round(net, 4), net_best=round(net, 4), depth_a=da, depth_b=db,
                                        contracts=c, plausible=int(net <= plausible))
                else:
                    e.update(last_t=t, cycles=e["cycles"] + 1, net_best=round(max(e["net_best"], net), 4))
            elif k in self.open:
                self.close(k, t, "closed" if net is not None else "quote missing")
        self.cycle_s = (self.cycle_s + [time.time() - t0])[-200:]

    def close(self, k, t, reason):
        e = self.open.pop(k)
        res = round(sum(self.cycle_s) / len(self.cycle_s), 1) if self.cycle_s else ""
        row = dict(open_ts=iso(e["open_t"]), close_ts=iso(t), seconds_open=int((t - e["open_t"]).total_seconds()),
                   cycles_open=e["cycles"], resolution_s=res, close_reason=reason, sport=e["sport"],
                   market_type=e["market_type"], player=e.get("player", ""), teams=e["teams"], event_time=e["event_time"],
                   a_venue=e["a_venue"], a_id=e["a_id"], a_price_open=e["a_price_open"], b_venue=e["b_venue"],
                   b_id=e["b_id"], b_price_open=e["b_price_open"], net_open=e["net_open"], net_best=e["net_best"],
                   depth_a=e["depth_a"], depth_b=e["depth_b"], contracts=e["contracts"],
                   profit_at_limit=round(e["net_open"] * e["contracts"], 2), plausible=e["plausible"])
        new = not self.log.exists()
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=EP_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        print(f"{iso(t)} gap {row['market_type']} {row['player'] or row['teams']} lasted ~{row['seconds_open']}s "
              f"({row['cycles_open']} scans), net {row['net_open'] * 100:+.1f}c x {row['contracts']} = "
              f"${row['profit_at_limit']:+.2f}{'' if row['plausible'] else ' (too good to be real)'}")


def summary(rows):
    """Lines for the weekly report from gap_episodes.csv rows."""
    real = [r for r in rows if r.get("plausible") == "1"]
    if not rows:
        return ["- No gaps seen yet (the scanner on the Oracle server re-prices the closest pairs every few seconds)."]
    secs = sorted(int(r["seconds_open"]) for r in real) or [0]
    med = secs[len(secs) // 2]
    catchable = [r for r in real if int(r["cycles_open"]) >= 2 and int(float(r["contracts"] or 0)) >= 3]
    res = [float(r["resolution_s"]) for r in rows if r.get("resolution_s")]
    return [f"- Gaps seen: **{len(rows)}** ({len(rows) - len(real)} too good to be real, left out). Scan every ~"
            f"{sum(res) / max(len(res), 1):.0f} s.",
            f"- How long real gaps lasted: median ~{med} s; {sum(s >= 30 for s in secs)} lasted 30 s or more, "
            f"{sum(s >= 120 for s in secs)} lasted 2 min or more.",
            f"- Catchable (seen on 2+ scans with 3+ contracts on both sides): **{len(catchable)}**, worth "
            f"**${sum(float(r['profit_at_limit']) for r in catchable):+.2f}** at your per-leg limit if each was taken once."]


if __name__ == "__main__":
    # quick local test: scan the current watch list for ~1 minute and print what opens/closes
    sys.path.insert(0, str(ROOT / "scripts"))
    g = GapScanner(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/gaps"))
    g.watch = list(csv.DictReader(io.StringIO(WATCH.read_text()))) if WATCH.exists() else []
    print(f"watching {len(g.watch)} pairs")
    for _ in range(10):
        g.poll()
        print(f"scan took {g.cycle_s[-1]:.1f}s, open gaps {len(g.open)}")
        time.sleep(5)
