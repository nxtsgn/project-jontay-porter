"""Tests for maker_sim's order-live delay and the hedger reconciliation:  python3 -m unittest discover tests"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import maker_sim as ms  # noqa: E402


def fill(placed, rid, first, status, hedged, unhedged, pnl, filled=10, px=0.31, hpx=""):
    return dict(placed_ts=placed, sport="nfl", game_date="", teams="", market_type="pass_yds", player="P", event_time="",
                rest_id=rid, rest_norm_side="yes", rest_raw_side="yes", rest_line="", rest_px=px, rest_bid="", rest_ask="",
                hedge_id="h", hedge_norm_side="no", hedge_flipped=0, hedge_line="", hedge_px_at_plan=0.65,
                hedge_fee_coef=0.095, contracts=filled, edge_at_plan=0.02, middle=False, resolved_ts=placed, filled=filled,
                first_fill_ts=first, hedge_px_at_fill=hpx, hedge_status=status, hedged_qty=hedged, unhedged_qty=unhedged,
                cost="", hedge_fee=0, locked_pnl=pnl, locked_per_contract="")


def hedger(placed, rid, ts, qty, hedged, px, late=0):
    return dict(placed_ts=placed, rest_id=rid, fill_ts=ts, late=late, fill_qty=qty, hedged_qty=hedged,
                hedge_avg_px=px, seconds_fill_to_hedge=3)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        ms.FILLS, ms.OUTCOMES, ms.RECONCILED, ms.DELAYS = d / "f.csv", d / "o.csv", d / "r.csv", d / "d.csv"
        self.now = ms.epoch("2026-11-21T10:00:00Z")
        rows = [fill("2026-11-21T09:07:42Z", "K-BECK", "2026-11-21T09:09:37.1Z", "UNHEDGED", 0, 10, -0.0),  # sim: bag
                fill("2026-11-21T09:00:00Z", "K-GOOD", "2026-11-21T09:01:00Z", "hedged", 10, 0, 0.18, hpx=0.65),  # sim: hedged
                fill("2026-11-21T09:00:00Z", "K-LATE", "2026-11-21T09:01:00Z", "UNHEDGED", 0, 10, 0.0),
                fill("2026-11-21T09:00:00Z", "K-NONE", "2026-11-21T09:01:00Z", "UNHEDGED", 0, 10, 0.0)]
        ms._write(ms.FILLS, ms.FILL_FIELDS, rows, "w")
        ms._write(ms.OUTCOMES, ms.OUTCOME_FIELDS, rows, "w")
        self.fast = [hedger("2026-11-21T09:05:06Z", "K-BECK", "2026-11-21T09:09:37.1Z", 22, 22, 0.65),  # other run's plan
                     hedger("2026-11-21T09:00:00Z", "K-GOOD", "2026-11-21T09:01:01Z", 10, 0, ""),      # hedger couldn't
                     hedger("2026-11-21T09:00:00Z", "K-LATE", "2026-11-21T09:01:00Z", 10, 10, 0.6, late=1)]

    def test_hedger_result_replaces_sim_both_ways(self):
        log = ms.reconcile(self.fast, now=self.now)
        got = {x["rest_id"]: (x["old_status"], x["new_status"]) for x in log}
        self.assertEqual(got, {"K-BECK": ("UNHEDGED", "hedged"), "K-GOOD": ("hedged", "UNHEDGED")})  # late/unseen kept
        rows = {r["rest_id"]: r for r in csv.DictReader(ms.FILLS.open())}
        self.assertEqual(float(rows["K-BECK"]["unhedged_qty"]), 0)
        self.assertGreater(float(rows["K-BECK"]["locked_pnl"]), 0)
        self.assertEqual(float(rows["K-GOOD"]["unhedged_qty"]), 10)
        outs = {r["rest_id"]: r for r in csv.DictReader(ms.OUTCOMES.open())}
        self.assertEqual(outs["K-BECK"]["hedge_status"], "hedged")  # the learner sees the corrected result
        self.assertEqual(ms.reconcile(self.fast, now=self.now), [])  # idempotent

    def test_order_live_delay(self):
        o = {"placed_ts": "2026-11-21T09:00:00Z"}
        self.assertEqual(ms.live_from(o) - ms.epoch(o["placed_ts"]), ms.DEFAULT_DELAY_S)
        ms.record_order_delay("2026-11-21T09:00:00Z", 71.2)
        ms.record_order_delay("2026-11-21T09:02:00Z", 50)
        self.assertAlmostEqual(ms.live_from(o) - ms.epoch(o["placed_ts"]), 71.2)
        self.assertAlmostEqual(ms.live_from({"placed_ts": "2026-11-21T08:00:00Z"}) - ms.epoch("2026-11-21T08:00:00Z"), 71.2)

    def test_better_results_need_full_backing(self):
        big = [hedger("2026-11-21T09:05:06Z", "K-BECK", "2026-11-21T09:09:37.1Z", 4, 4, 0.65)]      # 4 of 10 filled
        self.assertEqual(ms.reconcile(big, now=self.now), [])
        burst = [dict(hedger("2026-11-21T09:05:06Z", "K-BECK", "2026-11-21T09:09:37.1Z", 5, 5, 0.65), hedge_id="h") for _ in range(2)]
        self.assertEqual(ms.reconcile(burst, now=self.now), [])  # same depth may have been used twice

    def test_overstated_replacement_is_undone_once(self):
        ms.reconcile(self.fast, now=self.now)
        small = [dict(self.fast[0], fill_qty=4, hedged_qty=4)]
        log = ms.undo_overstated(small, self.now)
        self.assertEqual([(x["rest_id"], x["new_status"]) for x in log], [("K-BECK", "UNHEDGED")])
        r = {r["rest_id"]: r for r in csv.DictReader(ms.FILLS.open())}["K-BECK"]
        self.assertEqual((float(r["unhedged_qty"]), float(r["hedged_qty"])), (10, 0))
        self.assertEqual(ms.undo_overstated(small, self.now), [])


class DepthTest(unittest.TestCase):
    def test_hedger_does_not_take_the_same_depth_twice(self):
        import hedger as hg
        h = hg.Hedger(tempfile.mkdtemp())
        o = dict(fill("2026-11-25T18:00:00Z", "K", "", "", 0, 0, 0), placed_ts="2026-11-25T18:00:00Z", order_qty=20,
                 rest_px=0.31, hedge_limit=0.68, watch_from=0, chosen=1)
        saved, hg.hedge_levels = hg.hedge_levels, (lambda _: [(0.65, 10.0)])
        try:
            for i in range(2):
                h.record(o, dict(trade_id=str(i), created_time="2026-11-25T18:01:00Z"), 8)
        finally:
            hg.hedge_levels = saved
        rows = list(csv.DictReader(h.log.open()))
        self.assertEqual([float(r["hedged_qty"]) for r in rows], [8, 2])


class WindowTest(unittest.TestCase):
    def test_plan_counts_fills_while_its_orders_were_really_up(self):
        d = Path(tempfile.mkdtemp())
        for n in ("PLANS", "PREV_PLANS", "FILLS", "OUTCOMES", "RUNS", "DELAYS", "RECONCILED", "CANCELS"):
            setattr(ms, n, d / f"{n}.csv")
        seen, saved = [], (ms.fills_since, ms.plan, ms.settle_bags, ms.time.sleep)
        try:
            ms.fills_since = lambda tk, side, px, a, b: (seen.append((a, b)) or (0.0, None))
            ms.plan = lambda rows, ts: [dict(placed_ts=ts, rest_id="K", rest_raw_side="yes", rest_px=0.4, contracts=5, event_time="")]
            ms.settle_bags, ms.time.sleep = (lambda: None), (lambda s: None)
            for ts, delay in (("2026-11-21T10:00:00Z", 100), ("2026-11-21T10:02:20Z", 110), ("2026-11-21T10:04:40Z", 90)):
                ms.step([], ts)
                ms.record_order_delay(ts, delay)
        finally:
            ms.fills_since, ms.plan, ms.settle_bags, ms.time.sleep = saved
        # placed 10:00:00, out at 10:01:40, replaced when the 10:02:20 run's orders went out (10:04:10)
        self.assertEqual(seen, [(ms.epoch("2026-11-21T10:01:40Z"), ms.epoch("2026-11-21T10:04:10Z"))])


if __name__ == "__main__":
    unittest.main()
