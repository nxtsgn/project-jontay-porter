# Legal, fee and rules review (2026-10-22)

Not legal advice. Sources were web searches and live API reads; many search hits were affiliate/review sites, so
anything marked **verify** must be confirmed on the platform's own pages (or by a lawyer) before real money.

## 1. Do you need accounts for price collection? No.
- Kalshi: market data endpoints (`/markets`, `/series`) answer without login. Tested.
- Polymarket: Gamma API (`/events`, `/markets`, `/sports`) and CLOB `/time` answer without login. Tested.
- Accounts matter only for trading. Budget is $0, so none are needed now.
- Collecting public prices doesn't touch either platform's trading terms, but **verify** each API's terms of use / rate limits
  (collector sleeps 0.2s between calls, ~25 calls per run).

## 2. Legal status (fast-moving, state by state)
- **Kalshi** is a CFTC-regulated exchange, but states contest sports contracts. Reported: geofenced in Nevada, Michigan,
  Washington; Massachusetts injunction on appeal; Utah AG sued (Oct 2026) and a federal court allowed Utah to enforce its
  gambling ban; Connecticut denied Kalshi an injunction; New Jersey and the Third Circuit sided with Kalshi, with a possible
  Supreme Court question on federal preemption. NBA player props are exactly what Utah calls "indistinguishable from sportsbook bets".
- **Polymarket** has two faces. The international exchange (what `gamma-api` shows) is **close-only / geoblocked for US users**
  (one source, read 10/10/2026). **Polymarket US** (QCX, a CFTC-designated contract market, approved Nov 2025) is a separate
  exchange with separate account, KYC and brokers. Nevada obtained a TRO; Massachusetts, Connecticut, Arizona, Illinois are in litigation.
- **Implication:** which state you live in decides whether the strategy is even available. Tell me your state and I'll check it.
  **Verify** on kalshi.com and polymarket.us eligibility pages and your state regulator before opening anything.
- Taxes: gains are likely taxable; treatment of event contracts is unsettled. Ask a tax professional before real money.

### Arizona update (user is in Arizona)
- AG Kris Mayes filed 20 misdemeanor counts against Kalshi (Mar 2026). A federal district judge blocked enforcement in May, but the
  Ninth Circuit (which covers Arizona) ruled 2026-08-28 in the Nevada case that sports contracts are not federal "swaps" and states may
  regulate them. Kalshi is seeking further review; Arizona's next step was undecided as of early Sept. Polymarket has not been charged
  (reportedly only a warning letter). Both sources are secondary; check the dockets and AG announcements. **Arizona is a high-risk state for Kalshi.**

## 3. Data caveat that matters for the whole project
Prices from `gamma-api.polymarket.com` are the **international** book. If you could only ever trade **Polymarket US**,
its prices, liquidity and market list may differ. Before trusting any "edge", confirm Polymarket US has a read API and
compare its prices to the international ones. (I couldn't find Polymarket US developer docs in search; **open item**.)

## 4. Fees (verify against official schedules)
- Kalshi taker fee = roundup(0.07 x contracts x P x (1-P)), P in dollars; peaks at ~1.75c per contract at 50c, ~0.34c per
  contract at 5c/95c. Maker fee 0.0175 factor only on listed series. Some series carry a multiplier. No settlement fee reported.
  Official PDF: kalshi.com/docs/kalshi-fee-schedule.pdf.
- Polymarket: market data shows `feeType: zero_fees` on NBA markets today (read from the API); secondary sources say no taker fee.
  Fees can change by category; re-check at execution time. Also budget for deposit/withdrawal (USDC/fiat) costs. **Verify.**
- Rule of thumb: a 1-2c screen edge near 50c is usually eaten by Kalshi's fee alone. The paper tracker must net fees, not use raw spreads.

## 5. Resolution-rule mismatches found in live NBA markets (the real arbitrage risk)
| Issue | Kalshi | Polymarket | Consequence |
|---|---|---|---|
| Player inactive / never plays | Settles to "fair price" (refund-like) | Resolves **No** | Kalshi Yes + Polymarket No: on a DNP Kalshi refunds and Polymarket No pays $1, so you win. Kalshi No + Polymarket Yes: on a DNP Kalshi refunds and Polymarket Yes loses, so you lose that leg's cost. Only the first pairing is a true arb; model both |
| Threshold wording | "20+" (strike 19.5) | "Over 19.5" | Same for integers; confirm half-point mapping per pair |
| Postponement | Open if begins within 48h, else fair price | Stays open until played; cancelled = 50-50 | Different lockup and payout on edge cases |
| Overtime | Includes OT per rules (verify per series) | Includes OT | Likely aligned; check each |
| Stat source | Official box score per rules (verify) | NBA.com box score | Stat corrections could diverge |
| Oracle | Exchange determines | UMA-style disputes possible on international venue | Settlement delay / dispute risk |
Rule: a pair is only "arbitrage" if every edge case above leaves you >= 0. Otherwise it is a bet with a hedge.

## 6. Other risks
- Execution: two legs, two venues; one leg may fill and the other not (legging risk). Pre-funded balances needed on both.
- Capital lockup until game settlement; thin liquidity on props (Polymarket NBA props show wide spreads, e.g. bid 0.03 / ask 0.52).
- Counterparty/regulatory: a venue can geofence you or freeze access mid-position.
- NBA's own prop restrictions (first-basket, low-minute unders, same-player under parlays) bind sportsbook partners, not these venues.
- Bots: official APIs exist and automation is normal, but both ban manipulation, wash trading, spoofing. **Verify** terms before trading.
- Integrity: this project name nods to a banned NBA player; never trade on non-public information, and note both venues bar
  trading by players, coaches and staff of the league.

## 7. Open items for you
1. Which US state are you in? (decides Kalshi and Polymarket US availability)
2. OK to keep Polymarket US access/eligibility as a "check later" item until paper results justify accounts?

## 8. Update 2026-11-10: Polymarket US (the venue actually usable from Arizona)
- Public market data: `https://gateway.polymarket.us` (no key). Trading: `https://api.polymarket.us` (API key from polymarket.us/developer after KYC).
- Fees (docs.polymarket.us/fees): taker coef 0.095/0.085/0.075/0.0695 by 30-day volume tier (since Nov 6, 2026), banker's rounding to the cent; makers free, rebates at higher tiers.
- Availability: secondary sources say every state except Nevada; confirm Arizona in the app before funding.
- Prop DNP rule: settles to the last fair market price (comparable to Kalshi's fair-price rule).
- Open question: the listing marks the "No" side `tradable: false`; buying NO may mean selling YES. Confirm how short/NO orders work before trading.
