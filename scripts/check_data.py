"""Daily health + sufficiency check for the live price data (used by .github/workflows/data-check.yml).

Reads data/prices/prices_us_*.csv.gz and prints one line of key=value pairs for the workflow:
    status=stale|ready|collecting  newest=<ts>  snapshots=<n>  days=<n>
"stale": newest snapshot is older than --stale-hours (collector likely broken).
"ready": at least --min-days distinct days and --min-snapshots snapshots since --since.
"""
import argparse
import csv
import glob
import gzip
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-10")
    ap.add_argument("--min-days", type=int, default=21)
    ap.add_argument("--min-snapshots", type=int, default=1000)
    ap.add_argument("--stale-hours", type=float, default=6)
    a = ap.parse_args()
    stamps = set()
    for path in glob.glob(str(ROOT / "data" / "prices" / "prices_us_*.csv.gz")):
        with gzip.open(path, "rt") as fh:
            stamps |= {r["ts_utc"] for r in csv.DictReader(fh) if r["ts_utc"][:10] >= a.since}
    if not stamps:
        print("status=stale newest=none snapshots=0 days=0")
        return
    newest = max(stamps)
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(newest.replace("Z", "+00:00"))).total_seconds() / 3600
    days = len({s[:10] for s in stamps})
    if age_h > a.stale_hours:
        status = "stale"
    elif days >= a.min_days and len(stamps) >= a.min_snapshots:
        status = "ready"
    else:
        status = "collecting"
    print(f"status={status} newest={newest} snapshots={len(stamps)} days={days}")


if __name__ == "__main__":
    main()
