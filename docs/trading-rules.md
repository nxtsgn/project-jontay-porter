# Trading rules (DRAFT, not approved)

This is the plain-English version of [`config/trading_rules.json`](../config/trading_rules.json). The bot will not place a real
order until you change `"approved"` to `true`, add your name and date, and set `"mode"` to `"live"`. Every number below is a
proposal. Change any of them.

## Money
- **$50 on Kalshi, $50 on Polymarket US.** $10 stays untouched on each venue so a hedge or fee can always be paid. That leaves
  **$40 per venue to trade with.**

## Each trade
- **Max $10 per leg** (one side of a hedge on one venue), and at most 25 contracts.
- **At least 3 contracts**, or the fees and effort aren't worth it.
- **Minimum profit after fees:**
  - Taker-taker hedges (both sides bought immediately): **1¢ per contract**.
  - Resting-order hedges (Kalshi order waits, Polymarket US hedge bought when it fills): **2¢ per contract**, because the hedge
    price can move while we wait.
- **The hedge can't cost more than 3¢ worse than planned.** If it would, the bot holds the unhedged leg, alerts you, and stops
  adding new trades.

## Across all trades
- At most **6 hedges open** at once, and at most **2 on the same game**.
- **Unhedged exposure** (a leg that filled but isn't hedged yet) is capped at **$5 total**, and no leg stays unhedged longer
  than **2 minutes**.
- **Player props are riskier.** If a player is active but never plays, the two venues may settle differently. So: at most
  **2 prop hedges open**, **$5 per leg**.

## Stop rules
- **Lose $5 in a day:** no new trades until the next day.
- **Down $15 in total (15% of $100):** cancel every open order, stop completely, and wait for you to restart it.
- **Kill switch:** a GitHub repo variable `TRADING_ENABLED`. Set it to `false` and the bot cancels all open orders on its next
  check and does nothing else. Existing hedged positions are left alone; they settle on their own.
- **It also kills itself if:**
  - either venue returns 3 errors in a row,
  - a venue's balance differs from the bot's records by more than $1,
  - a leg can't be hedged in time, or
  - a game we hold is postponed or cancelled. Postponements are where Kalshi and Polymarket US settle differently, see
    [the rules review §9b](../research/legal-and-rules-review.md).

## What's allowed
- **Strategies:** taker-taker arbitrage, and resting Kalshi orders hedged on Polymarket US.
- **Sports:** NBA, NFL, college football (P4 + Notre Dame), men's and women's college basketball (your conference list).
- **Markets:** moneylines, spreads, totals, player props (with the prop caps above).
- **Timing:** only games starting within the next 48 hours. No new trades in the last 10 minutes before tip-off/kickoff, and
  **no in-game trading.**

## One practical consequence
The resting-order strategy needs the bot to hedge within about **2 minutes** of a fill. The GitHub Actions collector only
runs every 15 minutes, so the live bot can't run there. It will need a small always-on process, like a cheap cloud VM or a
computer that stays on. We'll pick one when the data justifies going live. Taker-taker hedges have no waiting period, but they
still need both orders sent within seconds of each other.

## To approve
Edit the numbers you want to change, then tell me "approved" (or set the fields yourself). I'll record the date.
