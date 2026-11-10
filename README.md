# Project Jontay Porter: Prediction Market Arbitrage

**Status:** Not started (kickoff soon). Part of [Overarching Goals and Roadmap](../../docs/overarching-goals-and-roadmap.md).

## Goal
Find and capture risk-free (or near risk-free) price discrepancies across prediction markets, e.g.
the same event priced differently on two platforms, or outcomes in one market summing to less than 100%.

## Principles
- Paper-track every opportunity before risking money.
- Count all costs: fees, spreads, withdrawal limits, settlement timing, resolution-rule differences.
- Check platform terms and legal status (state and federal) before trading.
- Start small; size by what we can afford to lose entirely.

## Decisions so far (2026-10-22)
- **Platforms:** Polymarket and Kalshi.
- **Sport:** NBA first.
- **Learning from others:** study creators who publicly discuss prediction market arbitrage (see below).
- **NBA data stash:** every player in the league, every season from 2003-04 (LeBron James's rookie
  year, our reference point) to today. Players who debuted before 2003 are covered from 2003-04 on.
- **Accounts:** none yet on Kalshi or Polymarket. You're an eligible adult; we'll open them only when
  the paper-tracking phase says it's worth it.
- **Stash runs locally on your machine** (cloud sandbox can't reach the NBA/market sites).
- **Goal for the bot:** an autonomous, read-only monitor that watches Polymarket and Kalshi odds
  24/7 and logs/flags discrepancies. It places no trades unless you separately approve that later.

## Platform notes (verify before relying on them)
- Kalshi is a CFTC-regulated exchange with a public API.
- Polymarket has a public API for market data, but its availability and rules for US users have
  changed over time. **Check current eligibility for you before opening any account or trading.**
- Cross-platform arbitrage requires the two markets to resolve on identical terms; compare rule text
  for every pair.

## Learning from TikTok creators
I won't scrape TikTok: it violates their terms and is blocked by anti-bot measures. Instead:
- You send me creator handles, video links, or screen-recorded/transcribed clips, and I'll extract
  strategies, claimed edges and red flags into `research/creators.md`.
- I'll search the public web for write-ups on the same topic.
- Treat all creator claims as unverified until we reproduce them on paper-tracked data. Creator
  content is often marketing.

## NBA data stash plan
- Source candidates: the `nba_api` package (official stats endpoints) and Basketball-Reference.
  Check each source's terms and rate limits.
- Script: `scripts/fetch_nba_stats.py` (written, **not yet run or tested**). Per season it saves
  `data/nba/season_totals_<YYYY-YY>.csv.gz` (all players) and `data/nba/game_logs_<YYYY-YY>.csv.gz`
  (every player-game). Resumable; skips files that already exist.
- Run locally: `pip install -r projects/jontay-porter/requirements.txt`, then
  `python projects/jontay-porter/scripts/fetch_nba_stats.py`. It takes a while (about 45 requests with
  pauses). Then commit `data/nba/` and push so I can analyze it.
- Scope: every player, 2003-04 onward, regular season (`--season-type Playoffs` for playoffs).
- **Honest caveat:** historical player stats help us model player/game outcomes, but arbitrage
  itself needs *price history from both platforms* for the same NBA markets. Stats alone don't
  reveal arbitrage; we should start collecting Polymarket and Kalshi NBA prices alongside.
- **Environment limit:** this cloud session can't reach stats.nba.com, basketball-reference.com,
  Kalshi or Polymarket (network policy), hence the local run.

## 24/7 odds monitor (design, not built yet)
Options for running autonomously, read-only:
1. **GitHub Actions on a schedule** (like the trading bot's `live-poll.yml`). Free, needs no machine of
   yours, and GitHub's runners can reach both platforms. Minimum interval is about 5 minutes and runs
   can be delayed, so it's good for logging odds and flagging gaps, not for sub-minute arbitrage.
2. **Always-on small server (VPS) or a home machine** running a loop. True continuous monitoring and
   seconds-level data, at a few dollars a month plus upkeep. Needed if we ever want to act fast.
Recommendation: start with option 1 for analysis and paper tracking, move to option 2 only if the data
shows edges that vanish faster than 5 minutes.
Both platforms publish market data through public APIs; I'll confirm endpoints and rate limits
when I can reach them.

## Phases
1. **Research:** list candidate platforms, their fees, APIs, access rules and legality for you.
2. **Detection:** script that pulls prices and flags cross-market or within-market discrepancies.
3. **Paper tracking:** log flagged opportunities and what would have been earned after costs.
4. **Decision:** you review the results and decide whether to deploy real capital.

## Key risks to study
Resolution-wording mismatch between platforms, liquidity (can't fill both legs), platform/counterparty
risk, capital tied up until settlement, and regulatory limits.

## Next steps
- [x] Platforms chosen: Polymarket and Kalshi
- [ ] You run `fetch_nba_stats.py` locally and push `data/nba/`
- [ ] Send creator handles/links for the research file
- [ ] Review rules and legality for your location
- [ ] Define "worth acting on" thresholds (minimum net edge after fees)

## Open questions
- Approve option 1 (GitHub Actions) for the monitor?
- Starting budget, if any?


## Price collection (built 2026-10-22)
- `scripts/collect_prices.py` snapshots NBA moneyline, spread, total and player-prop markets (points, rebounds,
  assists, threes, steals, blocks) from both platforms into `data/prices/prices_<date>.csv.gz`. Public endpoints only,
  **no accounts or keys needed**. Tested locally: about 2,500 rows and 50 KB per snapshot.
- `.github/workflows/collect-prices.yml` runs it every 15 min and commits to a `price-data` branch.
  One-time setup: push this folder to a new GitHub repo, then `git checkout --orphan price-data`, commit, push.
  (Kalshi `KXNBA*` series via `api.elections.kalshi.com/trade-api/v2`; Polymarket via `gamma-api.polymarket.com`, NBA tag 745.)
- Legal, fee and rule-mismatch review: `research/legal-and-rules-review.md`.
- Next: a matcher that pairs Kalshi and Polymarket markets (same game, player, stat, line) and a paper-arb calculator.

## Matcher + paper calculator (built 2026-10-22)
- `scripts/find_arbs.py` pairs identical propositions across venues (player props, game totals, moneylines; spreads not yet)
  and prices both two-leg hedges per $1 contract: `edge = 1 - cost - fees`. Kalshi taker fee uses the official formula with
  per-order cent rounding (`--size`); Polymarket fee defaults to 0 (`--poly-fee-rate`). Player props where you'd hold
  Polymarket Yes are marked DNP-exposed and discounted by `--dnp-prob` (default 5%).
- First live run: 1,394 matched pairs across 12 games; only 4 hedges cleared +1c, all De'Aaron Fox props with Kalshi volume 0 and
  a Polymarket Yes leg, i.e. most likely injury/DNP pricing, not free money. Everything else was <= 0 after fees.
- Hits append to `data/paper/opportunities.csv`. Next: settle them against box scores to measure real paper P&L.
- Known gaps: no order-book depth (only best bid/ask), spreads unmatched, no volume filter yet.

## Volume filter + settlement (built 2026-10-25)
- `find_arbs.py` now skips pairs where Kalshi volume < 10 contracts or Polymarket liquidity < $500
  (`--min-kalshi-volume`, `--min-poly-liquidity`). On the 10-23 snapshot that removed 1,225 of 1,394 pairs.
- `scripts/settle.py` (run locally; needs nba_api) grades logged hedges against final box scores: props, totals, moneylines, with
  the DNP rules (Kalshi refund / Polymarket No). Writes `data/paper/settled.csv` and prints realized vs predicted edge.
- Validation on all 2,764 priced hedges from the 10-23 snapshot: realized P&L matched predicted net edge exactly for every non-DNP hedge.
  No DNPs appeared among 2,544 props, so the DNP branch is untested on real data.

## Backfill + backtest (built 2026-11-05)
- `scripts/backfill.py --since 2026-10-22` rebuilds a pre-game snapshot (default 1 hour before tip) for every finished game from both
  platforms' public price history, with real outcomes. Kalshi uses hourly candlestick bid/ask; Polymarket history is a single
  price per hour, so bid/ask is assumed as price -/+ 1c (`--poly-half-spread`). Output: `data/backfill/pregame_*.csv.gz`.
- `scripts/backtest.py` runs the same matcher and fee math over that file and grades each hedge with Kalshi's settled result.
  Filters out stale/illiquid quotes (Kalshi volume and spread, Polymarket total volume).
- Lesson from the first test: a Polymarket prop with $221 total volume showed Luka threes at 0.36 while Kalshi (8,000+ contracts)
  said 0.66. That is a stale quote, not an edge. Without volume filters the backtest reports fantasy profits.
- Limits: Polymarket volume is whole-market (not pre-game), hourly granularity, one snapshot per game, no order-book depth.

## Injury filter, retries, cleanup (2026-11-10)
- Injury filter (`--max-exposed-gap`, `--min-exposed-yes` in find_arbs.py and backtest.py). Default drops every hedge that needs
  Polymarket "Yes" on a player prop, because if the player sits Kalshi refunds and Polymarket pays "No", so that leg is lost.
  Backtest since Oct 22 with this default: 10 hedges, all won, avg +0.8c per $1 contract (pure arbitrage, but small).
  Looser settings, e.g. `--max-exposed-gap 0.05` (keep exposed hedges with gaps <= 5c), gave 14 winners / 0 losers, but that's a small sample.
- find_arbs.py skips games that have already tipped off (`--include-started` to keep them) and drops pairs whose quotes are crossed.
- backtest.py counts a moneyline hedge once (it appears twice, once per team's Kalshi market).
- backfill.py retries up to 5 times with backoff and has `--repair` to refetch only missing Kalshi markets.

## v2: Kalshi + Polymarket US only (2026-11-10)
The user can only trade Kalshi and **Polymarket US** from Arizona, so the international polymarket.com feed is dropped.
Everything before this section that mentions "Polymarket" used the international site; that code now lives in
`scripts/legacy/` and its results (backfill/backtest) are **not** valid for executable trading.

- `scripts/collect_prices.py`: Kalshi + Polymarket US (`gateway.polymarket.us`, public, no key). Every market becomes one
  "is X > line?" question: player props (points, rebounds, assists, threes, steals, blocks, PRA), totals, and a single
  **margin** axis that holds moneylines and all spreads. Writes `data/prices/prices_us_<date>.csv.gz`.
- `scripts/find_arbs.py`: finds guaranteed-payout pairs, YES on "X > L1" + NO on "X > L2" with L1 <= L2, within and across
  venues (same-line arbitrage and ladder gaps), after each venue's real taker fee, then sizes the top ones with live
  order-book depth and a $50-per-venue budget. Logged to `data/paper/opportunities.csv`.
- `scripts/grade.py`: grades logged hedges with each venue's own settlement (no box scores), into `data/paper/graded.csv`.
- The collect-prices workflow runs collect -> detect -> grade every 15 min and always uses the code from `main`.

Key facts learned from Polymarket US (verify before trading):
- Taker fee = coef x C x p x (1-p), coef 0.095 at the base volume tier since 2026-11-06 (higher than Kalshi's 0.07); makers pay no fee
  and higher tiers get rebates. So "Polymarket has no fees" was true only of the international site.
- Player props settle to the "last fair market price" if the player doesn't take the court (Kalshi: "fair price"), so the
  one-sided DNP loss from the international site doesn't apply.
- First live snapshot (6 games): 483 near-miss hedges, 1 profitable after fees (+0.1c), with < 1 contract of depth.
- `scripts/maker_sim.py` (runs every collection): paper test of resting a Kalshi limit order 1c above the best bid and,
  if Kalshi's public trade tape shows it would have filled, buying the other leg on Polymarket US at the next snapshot's
  ask. Logs `data/paper/maker_fills.csv` (locked P&L per fill) and `maker_runs.csv` (fill rate). Summary:
  `python scripts/maker_sim.py --summary` (on the price-data branch data). Polymarket US publishes no trade tape, so only
  Kalshi can be tested as the resting leg; Kalshi NBA series charge no maker fee.

## v3: more sports (2026-11-10)
NBA, NFL (with player props), college football (Power 4), men's and women's college basketball (Power 4 + Big East + WCC;
conference lists in `scripts/teams.py`, verify yearly). A college game is kept if at least one team is in those conferences,
and it is identified by that team (opponent names differ between venues). Polymarket US has no college player props, so
college is game lines only (moneyline, spreads, totals). First all-sport snapshot: NBA 33 / NFL 13 / CFB 34 / CBB 72 / WCBB 48
games matched on both venues; 3 tiny profitable taker hedges (NFL 2+ TD props, < 1 contract of depth); 150 resting-order plans
(max 60 per sport). Polymarket US markets are listed by end date (next 3 weeks) so games listed early aren't missed.
