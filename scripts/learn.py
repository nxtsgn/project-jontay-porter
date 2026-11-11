"""The bot's learning loop. After every collector run it re-learns, from EVERY opportunity (taken by the practice trader or
not), which kinds of trades actually make money, and the trader uses that to decide where the capital goes first.

What it learns from (all in data/paper/):
  maker_outcomes.csv     every resting-order plan, resolved against Kalshi's trade tape: filled or not, hedged or not,
                         locked profit, unhedged contracts. Unfilled plans count as $0, so a group that rarely fills ranks low.
  maker_bags.csv         what unhedged leftovers paid at settlement (open bags count as a TOTAL LOSS until they settle)
  taker_candidates.csv   every depth-checked taker hedge: was it still profitable at real depth, and was it still there one
                         run later (a gap that vanishes in 5 minutes can't be traded by a slow bot)
Model: value per contract offered, by group (strategy > market kind > sport > price band > time to start). Small groups are
pulled toward their parent group and every group starts slightly pessimistic, so a few lucky fills can't make the bot
chase noise. Rankings use the LOWER end of an 80% range (conservative).

What it is allowed to change (the "can only tighten" rule):
  * the ORDER in which the trader spends its limited capital (best proven groups first)
  * SKIP a group once it has enough samples and is confidently losing money
It can never raise a limit, loosen a rule or turn a strategy on. Rule changes it thinks would help go into a
suggestions list for you to approve. Every time a group's action changes, a line is added to learning/changes.csv.

    python scripts/learn.py              # rebuild the model from data/paper and print the report
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "data" / "paper"
OUT = PAPER / "learning"
MODEL, CHANGES = OUT / "model.json", OUT / "changes.csv"
CHANGE_FIELDS = ["ts_utc", "group", "old_action", "new_action", "n", "value_per_contract", "low", "high"]

PRIOR_VALUE = -0.005   # unproven = assumed to lose half a cent per contract offered
PRIOR_WEIGHT = 20      # pseudo-samples: how strongly small groups are pulled toward their parent
MIN_SD = 0.05          # floor on per-sample spread (5c) so a few similar results can't look certain
Z80 = 1.2816


def market_kind(market_type, a_line=None, b_line=None):
    if market_type == "margin":
        return "moneyline" if (a_line in (0, 0.0) or b_line in (0, 0.0)) else "spread"
    if market_type == "totals":
        return "total"
    return "prop"


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def price_band(p):
    return "<20c" if p < 0.2 else "20-50c" if p < 0.5 else "50-80c" if p < 0.8 else ">80c"


def time_band(hours):
    return "<3h" if hours < 3 else "3-12h" if hours < 12 else ">12h"


def groups_for(strategy, kind, sport, price, hours):
    """Most general first. The trader looks up the most specific group that has data."""
    g = [strategy, f"{strategy}|{kind}", f"{strategy}|{kind}|{sport}"]
    if price is not None and hours is not None:
        g.append(f"{strategy}|{kind}|{sport}|{price_band(price)}|{time_band(hours)}")
    return g


def maker_groups(o):
    hours = (_ts(o["event_time"]) - _ts(o["placed_ts"])).total_seconds() / 3600 if o.get("event_time") else None
    kind = market_kind(o["market_type"], _f(o.get("rest_line"), None), _f(o.get("hedge_line"), None))
    return groups_for("maker", kind, o["sport"], _f(o["rest_px"], None), hours)


def taker_groups(o):
    hours = (_ts(o["event_time"]) - _ts(o["ts_utc"])).total_seconds() / 3600 if o.get("event_time") else None
    kind = market_kind(o["market_type"], _f(o.get("a_line"), None), _f(o.get("b_line"), None))
    return groups_for("taker", kind, o["sport"], _f(o["a_price"], None), hours)


def _read(path):
    return list(csv.DictReader(path.open())) if path.exists() else []


# ------------------------------------------------------------------------------------------------------ samples
def maker_samples():
    """(groups, value per contract offered, filled?) for every resolved plan. Conservative valuation."""
    bags = {(b["placed_ts"], b["rest_id"]): _f(b["bag_pnl"]) for b in _read(PAPER / "maker_bags.csv")}
    out = []
    for o in _read(PAPER / "maker_outcomes.csv"):
        c = max(_f(o["contracts"], 1), 1)
        filled = _f(o["filled"])
        v = _f(o["locked_pnl"])
        q = _f(o["unhedged_qty"])
        if q > 0:
            key = (o["placed_ts"], o["rest_id"])
            v += bags[key] if key in bags else -q * _f(o["rest_px"])  # open bag = total loss until it settles
        out.append((maker_groups(o), v / c, filled > 0))
    return out


def taker_samples():
    """(groups, value per contract, executable?) for every depth-checked candidate. Executable means profitable at real
    depth with >= 1 contract AND still listed in the next run (a 5-minute bot could actually have caught it)."""
    rows = _read(PAPER / "taker_candidates.csv")
    by_ts = defaultdict(set)
    for r in rows:
        by_ts[r["ts_utc"]].add((r["a_id"], r["b_id"]))
    times = sorted(by_ts)
    nxt = {t: (by_ts[times[i + 1]] if i + 1 < len(times) else None) for i, t in enumerate(times)}
    out = []
    for r in rows:
        later = nxt[r["ts_utc"]]
        if later is None:
            continue  # newest run: persistence not known yet
        ok = _f(r["contracts"]) >= 1 and _f(r["net_per_contract"]) > 0 and (r["a_id"], r["b_id"]) in later
        out.append((taker_groups(r), _f(r["net_per_contract"]) if ok else 0.0, ok))
    return out


# ------------------------------------------------------------------------------------------------------ model
def fit(samples):
    """Hierarchical shrinkage: each group's mean is pulled toward its parent's by PRIOR_WEIGHT pseudo-samples."""
    stats = defaultdict(lambda: [0, 0.0, 0.0, 0])  # n, sum, sum of squares, hits
    for gs, v, hit in samples:
        for g in gs:
            s = stats[g]
            s[0] += 1
            s[1] += v
            s[2] += v * v
            s[3] += hit
    model = {}
    for g in sorted(stats, key=lambda g: g.count("|")):  # parents before children
        n, sm, sq, hits = stats[g]
        parent = model.get(g.rsplit("|", 1)[0]) if "|" in g else None
        prior = parent["value_per_contract"] if parent else PRIOR_VALUE
        mean = (sm + PRIOR_WEIGHT * prior) / (n + PRIOR_WEIGHT)
        raw_mean = sm / n
        sd = max(math.sqrt(max(sq / n - raw_mean ** 2, 0)), MIN_SD)
        se = sd / math.sqrt(n)  # uncertainty from the real sample size only (the prior doesn't make us more sure)
        model[g] = dict(n=n, hit_rate=round(hits / n, 4), raw_value_per_contract=round(raw_mean, 5),
                        value_per_contract=round(mean, 5), low=round(mean - Z80 * se, 5), high=round(mean + Z80 * se, 5))
    return model


def decide(model, min_samples):
    for g, m in model.items():
        m["action"] = "skip" if (m["n"] >= min_samples and m["high"] < 0) else "normal"
    return model


def update(ts=None, min_samples=30):
    """Rebuild the model; log any group whose action changed. Called once per collector run."""
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    model = decide(fit(maker_samples() + taker_samples()), min_samples)
    old = json.loads(MODEL.read_text()).get("groups", {}) if MODEL.exists() else {}
    changes = [dict(ts_utc=ts, group=g, old_action=old.get(g, {}).get("action", "new"), new_action=m["action"], n=m["n"],
                    value_per_contract=m["value_per_contract"], low=m["low"], high=m["high"])
               for g, m in model.items() if old.get(g, {}).get("action", "normal") != m["action"]]
    OUT.mkdir(parents=True, exist_ok=True)
    MODEL.write_text(json.dumps({"updated": ts, "min_samples": min_samples, "groups": model}, indent=1))
    if changes:
        new = not CHANGES.exists()
        with CHANGES.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CHANGE_FIELDS)
            if new:
                w.writeheader()
            w.writerows(changes)
    n_m = model.get("maker", {}).get("n", 0)
    n_t = model.get("taker", {}).get("n", 0)
    print(f"learning: {n_m} resting-order and {n_t} taker outcomes, {len(model)} groups, "
          f"{sum(m['action'] == 'skip' for m in model.values())} skipped, {len(changes)} changes")
    return model


def load():
    return json.loads(MODEL.read_text()).get("groups", {}) if MODEL.exists() else {}


def score(model, groups, min_samples=30):
    """(conservative score, action, group used). The score comes from the most specific group with data; the action from
    the most specific group with enough samples to judge (so a thin subgroup of a losing group is skipped too)."""
    have = [g for g in reversed(groups) if g in model]
    if not have:
        return PRIOR_VALUE, "normal", None
    action = next((model[g]["action"] for g in have if model[g]["n"] >= min_samples), "normal")
    return model[have[0]]["low"], action, have[0]


# ------------------------------------------------------------------------------------------------------ report
def report(model):
    print(f"{'group':<44}{'n':>6}{'hit':>7}{'raw':>9}{'learned':>9}{'80% range':>20}  action")
    for g, m in sorted(model.items(), key=lambda kv: (kv[0].count("|"), -kv[1]["n"])):
        if g.count("|") <= 2:
            print(f"{g:<44}{m['n']:>6}{m['hit_rate']:>7.1%}{m['raw_value_per_contract'] * 100:>8.2f}c"
                  f"{m['value_per_contract'] * 100:>8.2f}c   {m['low'] * 100:>6.2f}c to {m['high'] * 100:>5.2f}c  {m['action']}")
    rej = defaultdict(int)
    for r in _read(PAPER / "trader_runs.csv"):
        for k, v in json.loads(r.get("rejections") or "{}").items():
            rej[k] += v
    print("\nSUGGESTIONS (for you to approve; the learner never changes the rules itself):")
    good = [g for g, m in model.items() if m["n"] >= 30 and m["low"] > 0]
    bad = [g for g, m in model.items() if m["action"] == "skip" and g.count("|") <= 2]
    if not good and not bad:
        print("  none yet: no group has enough results to be confidently good or bad.")
    for g in good:
        print(f"  {g} is confidently profitable ({model[g]['low'] * 100:.2f}c+ per contract).", end="")
        if "|prop" in g and rej.get("max prop hedges"):
            print(f" The prop cap rejected {rej['max prop hedges']} candidates; consider raising max_prop_hedges_open.", end="")
        print()
    for g in bad:
        print(f"  {g} is confidently losing; the bot already skips it. Consider removing it from 'allowed'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=30)
    a = ap.parse_args()
    report(update(min_samples=a.min_samples))
