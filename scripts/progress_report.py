"""Progress report (markdown) for the weekly GitHub issue and the 21-day "enough data" issue. Conservative by design: see
docs/trading-rules.md "Counting results conservatively". Reads data/paper/* and data/prices/*; writes nothing.

    python scripts/progress_report.py > report.md
"""
import csv
import glob
import gzip
import io
import json
import math
import sys
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "data" / "paper"
BANKROLL = 100.0  # $50 per venue, what would actually be funded
MIN_DAYS_FOR_RATE, MIN_FILLS_FOR_RATE = 14, 30


def _read(name):
    p = PAPER / name
    return list(csv.DictReader(p.open())) if p.exists() else []


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def snapshot_span():
    stamps = set()
    for path in glob.glob(str(ROOT / "data" / "prices" / "prices_us_*.csv.gz")):
        with gzip.open(path, "rt") as fh:
            stamps |= {r["ts_utc"] for r in csv.DictReader(fh) if r.get("sport")}
    if not stamps:
        return None, None, 0
    return min(stamps), max(stamps), len(stamps)


def returns_line(pnl, start, end, fills):
    """Return on the $100 bankroll over the span, plus the compounding-consistent monthly equivalent."""
    days = max((_ts(end) - _ts(start)).total_seconds() / 86400, 1e-9)
    total = pnl / BANKROLL
    line = f"**{total:+.2%}** of the ${BANKROLL:.0f} bankroll over {days:.1f} days"
    if days < MIN_DAYS_FOR_RATE or fills < MIN_FILLS_FOR_RATE:
        return line + (f". Monthly equivalent not shown: needs {MIN_DAYS_FOR_RATE}+ days and {MIN_FILLS_FOR_RATE}+ fills "
                       f"(have {days:.1f} days, {fills} fills); a number from this little data would mislead.")
    if total <= -1:
        return line + "; monthly equivalent: bankroll lost."
    monthly = (1 + total) ** (30.44 / days) - 1
    return line + f"; monthly equivalent **{monthly:+.2%}** (compounded)."


def maker_section():
    outcomes = _read("maker_outcomes.csv")
    fills = _read("maker_fills.csv")
    bags = {(b["placed_ts"], b["rest_id"]): _f(b["bag_pnl"]) for b in _read("maker_bags.csv")}

    def value(o):  # conservative $ for a plan at its own size
        v = _f(o["locked_pnl"])
        q = _f(o["unhedged_qty"])
        if q > 0:
            k = (o["placed_ts"], o["rest_id"])
            v += bags[k] if k in bags else -q * _f(o["rest_px"])
        return v

    # What the practice trader would have made: only the plans it actually chose, at the size its rules allowed.
    chosen = {}
    for r in _read("trader_orders.csv"):
        if r["strategy"] == "maker" and r["leg"] == "rest":
            _, rid, side, hid = r["hedge_key"].split("|")
            chosen[(r["ts_utc"], rid, side, hid)] = _f(json.loads(r["payload"])["count"])
    trader_pnl, trader_fills, trader_n = 0.0, 0, 0
    for o in outcomes:
        k = (o["placed_ts"], o["rest_id"], o["rest_norm_side"], o["hedge_id"])
        if k in chosen:
            trader_n += 1
            trader_pnl += value(o) / max(_f(o["contracts"]), 1) * chosen[k]
            trader_fills += _f(o["filled"]) > 0
    # fills from before every plan was tracked (2026-11-11): not in outcomes, but their open risk still counts
    tracked = {(o["placed_ts"], o["rest_id"]) for o in outcomes}
    legacy = [r for r in fills if (r["placed_ts"], r["rest_id"]) not in tracked]
    all_pnl = sum(value(o) for o in outcomes + legacy)
    open_bag = sum(_f(o["unhedged_qty"]) * _f(o["rest_px"]) for o in outcomes + legacy
                   if _f(o["unhedged_qty"]) > 0 and (o["placed_ts"], o["rest_id"]) not in bags)
    st = Counter(o["hedge_status"] for o in outcomes if _f(o["filled"]) > 0)
    n = len(outcomes)
    lines = [
        "### Resting Kalshi orders, hedged on Polymarket US",
        f"- Practice orders checked against Kalshi's trade record: **{n}**; filled: **{sum(st.values())}** "
        f"({sum(st.values()) / max(n, 1):.1%}). Fully hedged {st.get('hedged', 0)}, partly {st.get('PARTIAL', 0)}, "
        f"not at all {st.get('UNHEDGED', 0)}.",
        f"- Within your rules (only the orders the practice trader chose, at its sizes): **${trader_pnl:+.2f}** "
        f"from {trader_n} orders, {trader_fills} filled.",
        f"- Every planned order at full size (not limited by your rules, for comparison): ${all_pnl:+.2f}.",
        f"- Unhedged contracts still open, counted as a total loss until they settle: ${open_bag:.2f}.",
    ]
    if legacy:
        lines.append(f"- Includes {len(legacy)} fills from before the practice trader existed: "
                     f"${sum(value(r) for r in legacy):+.2f} (in the full-size line above, not the within-rules line).")
    return lines, trader_pnl, trader_fills, all_pnl


def taker_section():
    cands = _read("taker_candidates.csv")
    samples = learn.taker_samples()
    real = sum(1 for _, _, ok in samples if ok)
    graded = _read("graded.csv")
    led = [h for h in _read("trader_ledger.csv") if h.get("pnl") not in ("", None)]
    pnl = sum(_f(h["pnl"]) for h in led)
    return [
        "### Instant hedges (buy both sides at once)",
        f"- Gaps checked against real order-book depth: **{len(cands)}**. Profitable at real depth AND still there 5 minutes "
        f"later: **{real}** of {len(samples)} that could be checked.",
        f"- Practice trader's instant hedges settled: {len(led)}, result **${pnl:+.2f}**. "
        f"(Earlier detector log, all sizes: {len(graded)} settled, ${sum(_f(g['pnl']) for g in graded):+.2f}.)",
    ], pnl


def trader_section():
    runs = _read("trader_runs.csv")
    rej = Counter()
    for r in runs:
        rej.update(json.loads(r.get("rejections") or "{}"))
    stops = Counter(r["status"].split(" (")[0] for r in runs if r["status"] != "ok")
    top = ", ".join(f"{k} ({v})" for k, v in rej.most_common(4)) or "none"
    return ["### Practice trader (sends nothing)",
            f"- Runs: {len(runs)}. Resting orders it would have placed: {sum(int(r['maker_accepted'] or 0) for r in runs)}; "
            f"instant hedges: {sum(int(r['taker_accepted'] or 0) for r in runs)}.",
            f"- Most common reasons it said no: {top}.",
            f"- Stops / kill switch: {dict(stops) if stops else 'none'}."]


def learning_section():
    model = learn.load()
    buf = io.StringIO()
    with redirect_stdout(buf):
        learn.report(model)
    text = buf.getvalue()
    sugg = text.split("SUGGESTIONS", 1)[1].split("\n", 1)[1].strip() if "SUGGESTIONS" in text else "none"
    skipped = [g for g, m in model.items() if m.get("action") == "skip"]
    lines = ["### What the bot has learned",
             f"- Groups tracked: {len(model)}; with 30+ results: {sum(m['n'] >= 30 for m in model.values())}; "
             f"skipped as confidently losing: {len(skipped)}{(' (' + ', '.join(skipped[:5]) + ')') if skipped else ''}.",
             "- Suggested rule changes (yours to approve):"]
    lines += [f"  - {s.strip()}" for s in sugg.splitlines() if s.strip()] or ["  - none"]
    changes = _read("learning/changes.csv")
    if changes:
        lines.append(f"- Changes it made on its own (re-ordering/skipping only): {len(changes)}; latest: "
                     f"{changes[-1]['group']} -> {changes[-1]['new_action']} ({changes[-1]['ts_utc']}).")
    return lines


def build(title):
    start, end, snaps = snapshot_span()
    out = [f"## {title}", ""]
    if not start:
        return "\n".join(out + ["No price snapshots found."])
    out.append(f"Data: {snaps} snapshots from {start} to {end} (UTC). All numbers are paper results, counted conservatively.")
    out.append("")
    m_lines, m_pnl, m_fills, m_all = maker_section()
    t_lines, t_pnl = taker_section()
    out += ["### Bottom line",
            f"- Paper result within your rules: **${m_pnl + t_pnl:+.2f}**: "
            + returns_line(m_pnl + t_pnl, start, end, m_fills),
            f"- Every planned order at full size plus instant hedges (comparison, not limited by your rules): "
            f"${m_all + t_pnl:+.2f}", ""]
    out += m_lines + [""] + t_lines + [""] + trader_section() + [""] + learning_section()
    out += ["", "_Generated by scripts/progress_report.py. Ask Claude Code in project-jontay-porter to go deeper._"]
    return "\n".join(out)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "Progress report"))
