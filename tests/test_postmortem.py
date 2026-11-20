"""Tests for scripts/postmortem.py on a tiny synthetic data set:  python3 -m unittest discover tests"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import postmortem as pm  # noqa: E402

OUT_F = ["placed_ts", "resolved_ts", "sport", "market_type", "player", "event_time", "rest_id", "rest_norm_side",
         "rest_line", "hedge_id", "hedge_line", "rest_px", "rest_bid", "rest_ask", "hedge_px_at_plan", "edge_at_plan",
         "contracts", "middle", "filled", "hedge_status", "hedged_qty", "unhedged_qty", "locked_pnl"]


def plan(ts, rid, px, hp, edge, contracts=20, filled=0, unhedged=0, locked=0.0, resolved=None, mtype="totals"):
    return dict(placed_ts=ts, resolved_ts=resolved or ts, sport="nba", market_type=mtype, player="", event_time="2026-11-20T02:00:00Z",
                rest_id=rid, rest_norm_side="yes", rest_line=200.5, hedge_id="h-" + rid, hedge_line=200.5, rest_px=px,
                rest_bid=px - 0.01, rest_ask=px + 0.05, hedge_px_at_plan=hp, edge_at_plan=edge, contracts=contracts,
                middle=False, filled=filled, hedge_status=("UNHEDGED" if unhedged else "hedged") if filled else "not filled",
                hedged_qty=filled - unhedged, unhedged_qty=unhedged, locked_pnl=locked)


def write(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields or list(rows[0]))
        w.writeheader()
        w.writerows(rows)


class PostMortemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paper, hd = self.tmp / "data" / "paper", self.tmp / "hd"
        run = "2026-11-19T18:00:00Z"  # 11:00 Arizona, Nov 19
        self.rows = [
            plan("2026-11-18T18:00:00Z", "K-OLD-1", 0.40, 0.55, 0.03, filled=10, locked=0.2),  # earlier day: trains the learner
            plan(run, "K-A", 0.40, 0.55, 0.03, filled=10, locked=0.30),        # picked, hedged: +0.30 at full size
            plan(run, "K-B", 0.30, 0.66, 0.025, filled=10, unhedged=10),        # skipped, unhedged, still open: -3.00
            plan(run, "K-C", 0.50, 0.45, 0.04),                                  # skipped, not filled
            plan(run, "K-D", 0.20, 0.75, 0.30, filled=5, locked=1.0),           # 'too good to be real': no policy takes it
            plan("2026-11-20T08:00:00Z", "K-NEXT", 0.40, 0.55, 0.03, filled=10, locked=0.5),  # Nov 20 Arizona: not this day
        ]
        write(paper / "maker_outcomes.csv", self.rows, OUT_F)
        write(paper / "maker_bags.csv", [dict(placed_ts="x", sport="", rest_id="x", player="", unhedged_qty=0, rest_px=0, payout=0, bag_pnl=0)])
        write(paper / "trader_runs.csv", [dict(ts_utc=run, scale=1.0, rejections=json.dumps({"too few contracts after caps": 2}))])
        payload = json.dumps({"count": 10})
        write(paper / "trader_orders.csv", [dict(ts_utc=run, mode="dry_run", strategy="maker", hedge_key=f"M|K-A|yes|h-K-A",
                                                 leg="rest", venue="kalshi", market_id="K-A", payload=payload)])
        write(hd / "fast_hedges.csv", [dict(placed_ts=run, chosen=1, rest_id="K-A", fill_ts="2026-11-19T18:01:00Z",
                                            seconds_fill_to_hedge=4, late=0, fill_qty=10, hedged_qty=10, status="hedged",
                                            locked_pnl=0.31)])
        write(hd / "gap_episodes.csv", [dict(open_ts="2026-11-19T19:00:00Z", seconds_open=40, cycles_open=8, contracts=5,
                                             profit_at_limit=0.25, plausible=1),
                                        dict(open_ts="2026-11-19T19:05:00Z", seconds_open=5, cycles_open=1, contracts=0,
                                             profit_at_limit=0, plausible=1)])
        self.data = pm.Data(self.tmp / "data", hd)
        pm.JOURNAL, pm.LATEST = self.tmp / "pm" / "postmortems.jsonl", self.tmp / "pm" / "latest.json"
        pm.OUT = self.tmp / "pm"

    def test_day_window_is_arizona(self):
        self.assertEqual(pm.day_bounds("2026-11-19"), ("2026-11-19T07:00:00", "2026-11-20T07:00:00"))
        self.assertEqual(self.data.days(), ["2026-11-18", "2026-11-19", "2026-11-20"])

    def test_conservative_value_and_take_vs_skip(self):
        m = pm.metrics("2026-11-19", self.data)
        t = m["take_vs_skip"]
        self.assertEqual(t["taken"]["plans"], 1)
        self.assertAlmostEqual(t["taken"]["value_usd"], 0.15)          # 10 of 20 contracts picked: half of +0.30
        self.assertEqual(t["skipped_at_trader_size"]["open_leftovers"], 1)  # open leftover counted as a total loss
        self.assertLess(t["skipped_at_trader_size"]["value_usd"], 0)
        self.assertAlmostEqual(t["full_size_all_plans_usd"], 0.30 - 3.00 + 1.0)
        self.assertGreater(t["skipping_saved_usd"], 0)

    def test_ranking_respects_edge_limits_and_budget(self):
        m = pm.metrics("2026-11-19", self.data)
        for name, r in m["ranking"].items():
            if name.startswith("used"):
                continue
            self.assertLessEqual(r["picks"], 3, name)  # K-D's 30c edge is never taken
        best = m["ranking"]["perfect hindsight (ceiling)"]["value_usd"]
        self.assertTrue(all(best >= v["value_usd"] - 1e-9 for k, v in m["ranking"].items() if not k.startswith("used")))

    def test_learner_is_scored_without_hindsight(self):
        model = pm.asof_model(self.data, pm.day_bounds("2026-11-19")[0])
        self.assertEqual(model["maker"]["n"], 1)  # only the Nov 18 outcome, not the day's own

    def test_hedger_and_gaps(self):
        m = pm.metrics("2026-11-19", self.data)
        self.assertEqual(m["hedger"]["fills_seen"], 1)
        self.assertEqual(m["hedger"]["median_seconds_fill_to_hedge"], 4)
        self.assertEqual(m["hedger"]["same_fill_vs_5min_sim"]["pairs"], 1)
        self.assertEqual(m["gaps"]["catchable"], 1)
        self.assertAlmostEqual(m["gaps"]["catchable_value_usd"], 0.25)

    def test_bundle_next_day_and_save(self):
        self.assertEqual(pm.next_day(self.data, today="2026-11-20"), "2026-11-18")
        b = pm.bundle("2026-11-19", self.data)
        self.assertEqual(b["trader_rejections_that_day"], {"too few contracts after caps": 2})
        m = pm.metrics("2026-11-19", self.data)
        entry = dict(day="2026-11-19", summary="s", root_causes="r", what_to_change=["w"], lesson="l",
                     hypotheses=[{"statement": "x", "how_to_test": "y"}])
        with self.assertRaises(ValueError):
            pm.save({**entry, "day": "2026-11-18"}, m)
        with self.assertRaises(ValueError):
            pm.save({**entry, "hypotheses": [{"statement": "x"}]}, m)
        row = pm.save(entry, m)
        self.assertEqual(row["hypotheses"][0]["status"], "untested")
        self.assertIn("taken_usd", row["numbers"])
        with self.assertRaises(ValueError):
            pm.save(entry, m)  # one post-mortem per day
        self.assertEqual(len(pm.journal()), 1)
        self.assertEqual(pm.next_day(self.data, today="2026-11-20"), "2026-11-18")


if __name__ == "__main__":
    unittest.main()
