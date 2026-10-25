"""Download NBA player stats for every player from a start season forward.

Writes, per season, to data/nba/:
  season_totals_<YYYY-YY>.csv.gz  one row per player (league-wide season totals)
  game_logs_<YYYY-YY>.csv.gz      one row per player per game

Defaults start at 2003-04 (LeBron James's rookie season). Existing files are
skipped, so an interrupted run can be resumed by running it again.

Run locally (stats.nba.com is unreachable from the cloud sandbox):
    pip install -r projects/jontay-porter/requirements.txt
    python projects/jontay-porter/scripts/fetch_nba_stats.py
"""
import argparse
import time
from pathlib import Path

from nba_api.stats.endpoints import leaguedashplayerstats, playergamelogs

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "nba"
TIMEOUT_S = 60
PAUSE_S = 1.5
RETRIES = 4


def season_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def with_retries(fetch):
    for attempt in range(RETRIES):
        try:
            return fetch()
        except Exception as exc:  # nba_api surfaces requests/JSON errors as-is
            if attempt == RETRIES - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  retry in {wait}s after: {exc}")
            time.sleep(wait)


def fetch_season(label: str, season_type: str) -> None:
    totals_path = DATA_DIR / f"season_totals_{label}.csv.gz"
    logs_path = DATA_DIR / f"game_logs_{label}.csv.gz"

    if not totals_path.exists():
        df = with_retries(lambda: leaguedashplayerstats.LeagueDashPlayerStats(
            season=label,
            season_type_all_star=season_type,
            per_mode_detailed="Totals",
            timeout=TIMEOUT_S,
        ).get_data_frames()[0])
        time.sleep(PAUSE_S)
        if df.empty:
            print(f"{label}: no totals yet, skipping")
            return
        df.to_csv(totals_path, index=False, compression="gzip")
        print(f"{label}: {len(df)} players -> {totals_path.name}")

    if not logs_path.exists():
        df = with_retries(lambda: playergamelogs.PlayerGameLogs(
            season_nullable=label,
            season_type_nullable=season_type,
            timeout=TIMEOUT_S,
        ).get_data_frames()[0])
        time.sleep(PAUSE_S)
        if df.empty:
            print(f"{label}: no game logs yet, skipping")
            return
        df.to_csv(logs_path, index=False, compression="gzip")
        print(f"{label}: {len(df)} game rows -> {logs_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start-year", type=int, default=2003,
                        help="first season's starting year (2003 = 2003-04)")
    parser.add_argument("--end-year", type=int, default=2026,
                        help="last season's starting year; empty seasons are skipped")
    parser.add_argument("--season-type", default="Regular Season",
                        choices=["Regular Season", "Playoffs"])
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for year in range(args.start_year, args.end_year + 1):
        fetch_season(season_label(year), args.season_type)


if __name__ == "__main__":
    main()
