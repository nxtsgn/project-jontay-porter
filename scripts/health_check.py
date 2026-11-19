"""Daily "all cylinders" check of the whole bot (run by .github/workflows/data-check.yml at 9:00 Arizona time).

Looks at the last 24 hours of data on the price-data branch and checks each moving part:
  prices      collector ran on schedule, newest snapshot is fresh, both venues are in it
  workflow    collect-prices runs on GitHub mostly succeeded (counts passed in by the workflow)
  resting     maker_sim made plans and checked them against Kalshi's trade tape
  trader      the practice trader ran, recently, and isn't stopped by a kill switch or loss limit
  learner     the learning model was updated recently
  grading     finished games got graded (no unhedged leftovers or instant hedges stuck ungraded)

Prints a markdown table. Exit code 0 = everything passed, 2 = at least one check failed (the workflow then opens or
updates a "Bot health check failed" issue, which emails you, and closes it once everything passes again).

    python scripts/health_check.py [--runs-total N --runs-failed N]
"""
import argparse
import csv
import glob
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "data" / "paper"
EXPECTED_PER_DAY = 720  # runs: cron-job.org fires every 2 minutes (every 5 until 2026-11-17, every 3 until 2026-11-19)
MIN_PER_DAY = 450       # below this, runs are being missed (a run takes ~1.5 min, so a few queue up)
EXPECTED_SNAPS = 288    # price snapshots are saved every ~5 min even though runs are every 2
MIN_SNAPS = 200
MAX_AGE_MIN = 30        # newest run/snapshot must be younger than this
GRADE_AFTER = timedelta(hours=36)


def ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def read(name):
    p = PAPER / name
    return list(csv.DictReader(p.open())) if p.exists() else []


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def age_min(now, s):
    return (now - ts(s)).total_seconds() / 60


def checks(now, runs_total=None, runs_failed=None):
    day_ago = now - timedelta(hours=24)
    out = []

    def add(part, ok, detail):
        out.append((part, ok, detail))

    # prices
    venues_at = {}  # snapshot time -> venues in it
    for d in {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in (0, 1)}:
        for path in glob.glob(str(ROOT / "data" / "prices" / f"prices_us_{d}.csv.gz")):
            with gzip.open(path, "rt") as fh:
                for r in csv.DictReader(fh):
                    if r.get("sport") and r["ts_utc"] >= day_ago.strftime("%Y-%m-%dT%H:%M:%S"):
                        venues_at.setdefault(r["ts_utc"], set()).add(r["venue"])
    stamps = set(venues_at)
    newest = max(stamps) if stamps else None
    venues_newest = venues_at.get(newest, set())
    add("Prices: fresh", bool(newest) and age_min(now, newest) <= MAX_AGE_MIN,
        f"newest snapshot {newest or 'none'}" + (f" ({age_min(now, newest):.0f} min old)" if newest else ""))
    add("Prices: on schedule", len(stamps) >= MIN_SNAPS, f"{len(stamps)} snapshots in 24 h (expect ~{EXPECTED_SNAPS})")
    add("Prices: both venues", {"kalshi", "polymarket_us"} <= venues_newest,
        f"venues in newest snapshot: {', '.join(sorted(venues_newest)) or 'none'}")

    # GitHub workflow runs
    if runs_total is not None:
        bad = runs_failed or 0
        ok = runs_total >= MIN_PER_DAY and bad <= max(3, 0.05 * runs_total)
        add("Collector workflow", ok, f"{runs_total} runs in 24 h, {bad} failed")

    # resting-order simulator
    mr = [r for r in read("maker_runs.csv") if ts(r["ts_utc"]) >= day_ago]
    planned, resolved = sum(int(f(r["new_plans"])) for r in mr), sum(int(f(r["plans_resolved"])) for r in mr)
    add("Resting orders: planning", len(mr) >= MIN_PER_DAY and planned > 0, f"{len(mr)} runs, {planned} orders planned")
    add("Resting orders: fill checks", planned == 0 or resolved >= 0.5 * planned,
        f"{resolved} of {planned} planned orders checked against Kalshi's trade tape")

    # practice trader
    tr = read("trader_runs.csv")
    tr24 = [r for r in tr if ts(r["ts_utc"]) >= day_ago]
    last = tr[-1] if tr else {}
    add("Trader: running", len(tr24) >= MIN_PER_DAY and bool(last) and age_min(now, last["ts_utc"]) <= MAX_AGE_MIN,
        f"{len(tr24)} runs in 24 h, last {last.get('ts_utc', 'never')}")
    add("Trader: not stopped", last.get("status") == "ok",
        f"status: {last.get('status', 'none')}; equity ${f(last.get('equity')) or 100:.2f}, bet size x{last.get('scale') or 1}")
    add("Trader: placing orders", sum(int(f(r["maker_accepted"])) + int(f(r["taker_accepted"])) for r in tr24) > 0,
        f"{sum(int(f(r['maker_accepted'])) for r in tr24)} resting orders, "
        f"{sum(int(f(r['taker_accepted'])) for r in tr24)} instant hedges in 24 h")

    # bad quotes: the trader must skip "profits" too big to be real; any that got through is a failure
    skipped = sum(json.loads(r.get("rejections") or "{}").get("edge too good to be real (bad quote?)", 0) for r in tr24)
    took = [h for h in read("trader_ledger.csv") if ts(h["ts_utc"]) >= day_ago and f(h.get("net_per_contract")) > 0.15]
    add("Bad-quote guard", not took, f"{skipped} too-good-to-be-real quotes skipped in 24 h, {len(took)} taken")

    # learner
    mp = PAPER / "learning" / "model.json"
    upd = json.loads(mp.read_text()).get("updated") if mp.exists() else None
    add("Learner: updating", bool(upd) and age_min(now, upd) <= 120, f"model updated {upd or 'never'}")

    # grading
    bags = {(b["placed_ts"], b["rest_id"]) for b in read("maker_bags.csv")}
    stuck = [o for o in read("maker_outcomes.csv") + read("maker_fills.csv")
             if f(o.get("unhedged_qty")) > 0 and o.get("event_time") and now - ts(o["event_time"]) > GRADE_AFTER
             and (o["placed_ts"], o["rest_id"]) not in bags]
    led = [h for h in read("trader_ledger.csv") if h.get("pnl") in ("", None) and now - ts(h["event_time"]) > GRADE_AFTER]
    add("Grading: settled games", not stuck and not led,
        f"{len(stuck)} unhedged leftovers and {len(led)} instant hedges still ungraded 36 h after their game")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-total", type=int)
    ap.add_argument("--runs-failed", type=int)
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    res = checks(now, a.runs_total, a.runs_failed)
    bad = [r for r in res if not r[1]]
    print(f"### Bot health {now.strftime('%Y-%m-%d %H:%M')} UTC: "
          + ("all checks passed" if not bad else f"**{len(bad)} of {len(res)} checks failed**"))
    print("\n| Part | Result | Detail |\n|---|---|---|")
    for part, ok, detail in res:
        print(f"| {part} | {'OK' if ok else '**FAILED**'} | {detail} |")
    if bad:
        print("\nOpen Claude Code in project-jontay-porter and ask it to fix the failed checks. "
              "This issue closes itself once a daily check passes.")
    sys.exit(2 if bad else 0)


if __name__ == "__main__":
    main()
