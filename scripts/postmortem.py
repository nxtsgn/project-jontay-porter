"""Daily post-mortem of the practice arbitrage bot (practice only: reads data, never trades).

Design adapted from Project bnbgene's post-mortem (score every decision against the alternative it had, keep the
accounting conservative, then have Claude write a short, evidence-tied review with 0-3 testable hypotheses). The daily
cloud routine "Jontay Porter post-mortem" runs `bundle`, writes the review, and runs `save`.

For one Arizona calendar day (UTC-7) it scores EVERY planned resting order (maker_outcomes.csv, one row per resolved
plan from maker_plans.csv), whether the practice trader picked it or not:
  value       conservative $: locked hedge profit (hedge priced at the worse of the book and the price at the fill)
              + settled leftover (maker_bags.csv), or minus the full cost of any leftover still open (counted as lost)
  take/skip   what the trader's picks earned at its sizes, and what the skipped plans would have earned at the size
              the trader would have used (same caps as trader.size, without the capital already tied up)
  ranking     each run re-picked under the Kalshi budget by alternative orderings: biggest edge, edge per dollar tied
              up, edge + learner score (the formula the trader uses), and a perfect-hindsight ceiling
  hedger      the Oracle server's seconds-fast hedges (hedger-data branch, fast_hedges.csv) vs the 5-minute simulation
  gaps        instant-hedge gaps timed by the server (gap_episodes.csv): how long, how many catchable
  learner     calibration: the model refitted on outcomes resolved BEFORE the day (no peeking) predicts each plan's
              value per contract; compared with what happened
Settlement comes from maker_bags.csv (maker_sim.settle_bags / grade.py; there is no separate settle.py).

    python scripts/postmortem.py metrics [--day YYYY-MM-DD] [--data DIR] [--hedger-dir DIR]   # print one day's metrics
    python scripts/postmortem.py bundle  [--data DIR] [--hedger-dir DIR]   # next day without a post-mortem: metrics,
                                                                           # rejections, learner changes, previous review
    python scripts/postmortem.py save FILE.json [--data DIR] [--hedger-dir DIR]   # append the written review

Results are small: data/postmortem/postmortems.jsonl (one line a day, review + headline numbers) and
data/postmortem/latest_metrics.json (the full metrics of the newest day), on the main branch.
"""
import argparse
import csv
import io
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "postmortem"
JOURNAL, LATEST = OUT / "postmortems.jsonl", OUT / "latest_metrics.json"
AZ = timezone(timedelta(hours=-7))  # Arizona, no daylight saving
PM_KEYS = ["day", "summary", "root_causes", "what_to_change", "hypotheses", "lesson"]
MAX_BACKLOG_DAYS = 3


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def read_csv(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as fh:
        return list(csv.DictReader(fh))


def hedger_csv(name, hedger_dir=None):
    """A log from the hedger-data branch: a local folder if given, else `git show origin/hedger-data:<name>`."""
    if hedger_dir:
        return read_csv(Path(hedger_dir) / name)
    r = subprocess.run(["git", "show", f"origin/hedger-data:{name}"], cwd=ROOT, capture_output=True, text=True)
    return list(csv.DictReader(io.StringIO(r.stdout))) if r.returncode == 0 else []


def day_bounds(day):
    start = datetime.fromisoformat(day).replace(tzinfo=AZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def in_day(ts, bounds):
    return bool(ts) and bounds[0] <= ts[:19] < bounds[1]


def rules():
    r = json.loads((ROOT / "config" / "trading_rules.json").read_text())
    b, ph, pf = r["bankroll"], r["per_hedge"], r["portfolio"]
    return dict(kalshi=b["kalshi_start"] - b.get("reserve_kalshi", b["reserve_per_venue"]),
                leg=ph["max_cost_per_leg"], prop_leg=pf["max_cost_per_prop_leg"], max_c=ph["max_contracts"],
                min_c=ph["min_contracts"], min_edge=ph["min_net_edge_maker_cents"] / 100,
                max_edge=ph.get("max_plausible_edge_cents", 15) / 100)


# ------------------------------------------------------------------------------------------------ inputs
class Data:
    def __init__(self, data_dir=None, hedger_dir=None):
        paper = Path(data_dir) / "paper" if data_dir else ROOT / "data" / "paper"
        self.outcomes = read_csv(paper / "maker_outcomes.csv")
        self.bags = {(b["placed_ts"], b["rest_id"]): f(b["bag_pnl"]) for b in read_csv(paper / "maker_bags.csv")}
        self.runs = {r["ts_utc"]: r for r in read_csv(paper / "trader_runs.csv")}
        self.cancels = read_csv(paper / "maker_cancels.csv")
        self.changes = read_csv(paper / "learning" / "changes.csv")
        self.chosen = {}
        for r in read_csv(paper / "trader_orders.csv"):
            if r.get("strategy") == "maker" and r.get("leg") == "rest":
                _, rid, side, hid = r["hedge_key"].split("|")
                self.chosen[(r["ts_utc"], rid, side, hid)] = f(json.loads(r["payload"]).get("count"))
        self.names = {r["event"]: r["full"] for r in read_csv(paper / "event_names.csv")}
        self.fast = hedger_csv("fast_hedges.csv", hedger_dir)
        self.gaps = hedger_csv("gap_episodes.csv", hedger_dir)

    def days(self):
        return sorted({datetime.fromisoformat(o["placed_ts"][:19]).replace(tzinfo=timezone.utc).astimezone(AZ)
                      .strftime("%Y-%m-%d") for o in self.outcomes if o.get("placed_ts")})


def value(o, bags):
    """Conservative $ at the plan's full size: locked hedge profit, plus the settled leftover or minus its full cost."""
    if f(o["filled"]) <= 0:
        return 0.0
    v, q = f(o["locked_pnl"]), f(o["unhedged_qty"])
    if q > 0:
        k = (o["placed_ts"], o["rest_id"])
        v += bags[k] if k in bags else -q * f(o["rest_px"])
    return v


def kind(o):
    return learn.market_kind(o["market_type"], f(o.get("rest_line"), None), f(o.get("hedge_line"), None))


def trader_size(o, rl, k, room=None):
    """Contracts the practice trader would have used (trader.size, minus capital already tied up elsewhere)."""
    px, hp = f(o["rest_px"]), f(o["hedge_px_at_plan"])
    if px <= 0 or hp <= 0:
        return 0
    cap = (rl["prop_leg"] if kind(o) == "prop" else rl["leg"]) * k
    c = min(max(rl["min_c"], math.floor(rl["max_c"] * k)), math.floor(cap / px + 1e-9), math.floor(cap / hp + 1e-9),
            math.floor(f(o["contracts"]) + 1e-9))
    if room is not None:
        c = min(c, math.floor(room / px + 1e-9))
    return c if c >= rl["min_c"] else 0


# ------------------------------------------------------------------------------------------------ learner (as of)
def asof_model(data, cutoff):
    """The learner refitted on outcomes resolved before `cutoff` only, so the day is scored without hindsight."""
    samples = []
    for o in data.outcomes:
        if (o.get("resolved_ts") or "9999")[:19] >= cutoff:
            continue
        samples.append((learn.maker_groups(o), value(o, data.bags) / max(f(o["contracts"], 1), 1), f(o["filled"]) > 0))
    return learn.decide(learn.fit(samples), 30) if samples else {}


def predict(model, o):
    sc, action, g = learn.score(model, learn.maker_groups(o), 30)
    mean = model[g]["value_per_contract"] if g else learn.PRIOR_VALUE
    return sc, mean, action


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for t in range(i, j + 1):
            r[order[t]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return round(cov / math.sqrt(va * vb), 3) if va and vb else None


# ------------------------------------------------------------------------------------------------ metrics
def metrics(day, data):
    b = day_bounds(day)
    rl = rules()
    plans = [o for o in data.outcomes if in_day(o.get("placed_ts"), b)]
    model = asof_model(data, b[0])
    rows = []
    for o in plans:
        c = max(f(o["contracts"]), 1)
        key = (o["placed_ts"], o["rest_id"], o["rest_norm_side"], o["hedge_id"])
        k = f(data.runs.get(o["placed_ts"], {}).get("scale"), 1.0) or 1.0
        v_full = value(o, data.bags)
        chosen = data.chosen.get(key)
        size = chosen if chosen else trader_size(o, rl, k)
        sc, mean, action = predict(model, o) if model else (learn.PRIOR_VALUE, learn.PRIOR_VALUE, "normal")
        rows.append(dict(o=o, k=k, chosen=chosen, size=size, v_full=v_full, v_size=v_full * size / c,
                         per_contract=v_full / c, filled=f(o["filled"]) > 0, open_bag=f(o["unhedged_qty"]) > 0 and
                         (o["placed_ts"], o["rest_id"]) not in data.bags, edge=f(o["edge_at_plan"]),
                         px=f(o["rest_px"]), score=sc, mean=mean, action=action, kind=kind(o)))

    def block(rs):
        return {"plans": len(rs), "fills": sum(r["filled"] for r in rs),
                "unhedged_fills": sum(r["filled"] and f(r["o"]["unhedged_qty"]) > 0 for r in rs),
                "open_leftovers": sum(r["open_bag"] for r in rs),
                "value_usd": round(sum(r["v_size"] for r in rs), 2)}

    taken = [r for r in rows if r["chosen"]]
    skipped = [r for r in rows if not r["chosen"]]
    sized = [r for r in skipped if r["size"] >= rl["min_c"]]
    take_skip = {
        "taken": block(taken),
        "skipped_at_trader_size": block(sized),
        "skipped_too_small_to_size": len(skipped) - len(sized),
        "full_size_all_plans_usd": round(sum(r["v_full"] for r in rows), 2),
        "skipping_saved_usd": round(-sum(r["v_size"] for r in sized), 2),
        "by_kind": {kd: {"taken_usd": round(sum(r["v_size"] for r in taken if r["kind"] == kd), 2),
                         "skipped_usd": round(sum(r["v_size"] for r in sized if r["kind"] == kd), 2),
                         "fills": sum(r["filled"] for r in rows if r["kind"] == kd)}
                    for kd in sorted({r["kind"] for r in rows})},
    }

    # ranking alternatives: each run re-picked under the Kalshi budget at the trader's sizes
    by_run = defaultdict(list)
    for r in rows:
        by_run[r["o"]["placed_ts"]].append(r)
    policies = {
        "biggest edge first": lambda r: -r["edge"],
        "edge per dollar tied up": lambda r: -r["edge"] / max(r["px"], 0.01),
        "edge + learner score (the trader's formula)": lambda r: -(r["edge"] + r["score"]),
        "perfect hindsight (ceiling)": lambda r: -r["per_contract"],
    }
    rank = {"used (the trader's actual picks)": {"value_usd": take_skip["taken"]["value_usd"],
                                                 "picks": len(taken), "fills": take_skip["taken"]["fills"]}}
    for name, key in policies.items():
        tot, picks, fills = 0.0, 0, 0
        for ts, rs in by_run.items():
            room = rl["kalshi"] * rs[0]["k"]  # resting orders share the Kalshi budget (the prop/open-hedge caps apply to fills)
            for r in sorted((x for x in rs if rl["min_edge"] <= x["edge"] <= rl["max_edge"]), key=key):
                c = trader_size(r["o"], rl, r["k"], room)
                if not c:
                    continue
                tot += r["per_contract"] * c
                room -= c * r["px"]
                picks += 1
                fills += r["filled"]
        rank[name] = {"value_usd": round(tot, 2), "picks": picks, "fills": fills}

    # Oracle hedger: seconds-fast hedges vs the 5-minute simulation, for the same fills
    fast = [h for h in data.fast if in_day(h.get("fill_ts"), b)]
    timed = sorted(f(h["seconds_fill_to_hedge"]) for h in fast if h.get("late") in ("0", 0))
    sim = {(o["placed_ts"], o["rest_id"]): o for o in plans}
    pairs = [(h, sim.get((h["placed_ts"], h["rest_id"]))) for h in fast]
    pairs = [(h, o) for h, o in pairs if o is not None and f(o["filled"]) > 0]
    hedger = {"fills_seen": len(fast), "picked_by_trader": sum(h.get("chosen") == "1" for h in fast),
              "median_seconds_fill_to_hedge": timed[len(timed) // 2] if timed else None,
              "late_fills_excluded_from_speed": sum(h.get("late") == "1" for h in fast),
              "fully_hedged_share": round(sum(h["status"] == "hedged" for h in fast) / len(fast), 2) if fast else None,
              "locked_usd": round(sum(f(h["locked_pnl"]) for h in fast), 2),
              "same_fill_vs_5min_sim": {"pairs": len(pairs),
                                        "fast_hedged_share": round(sum(f(h["hedged_qty"]) for h, _ in pairs) /
                                                                   max(sum(f(h["fill_qty"]) for h, _ in pairs), 1e-9), 2) if pairs else None,
                                        "sim_hedged_share": round(sum(f(o["hedged_qty"]) for _, o in pairs) /
                                                                  max(sum(f(o["filled"]) for _, o in pairs), 1e-9), 2) if pairs else None}}

    gaps = [g for g in data.gaps if in_day(g.get("open_ts"), b)]
    real = [g for g in gaps if g.get("plausible") == "1"]
    secs = sorted(int(f(g["seconds_open"])) for g in real)
    catch = [g for g in real if int(f(g["cycles_open"])) >= 2 and int(f(g["contracts"])) >= 3]
    gap = {"gaps": len(gaps), "too_good_to_be_real": len(gaps) - len(real),
           "median_seconds": secs[len(secs) // 2] if secs else None, "lasted_30s_plus": sum(s >= 30 for s in secs),
           "catchable": len(catch), "catchable_value_usd": round(sum(f(g["profit_at_limit"]) for g in catch), 2)}

    # learner calibration (as-of model)
    cal = {"model_trained_on_outcomes_before": b[0] + "Z", "groups": len(model)}
    if rows:
        pred, real_v = [r["mean"] for r in rows], [r["per_contract"] for r in rows]
        bins = []
        srt = sorted(rows, key=lambda r: r["mean"])
        for i in range(5):
            part = srt[i * len(srt) // 5:(i + 1) * len(srt) // 5]
            if part:
                bins.append({"predicted_c": round(100 * sum(r["mean"] for r in part) / len(part), 3),
                             "actual_c": round(100 * sum(r["per_contract"] for r in part) / len(part), 3),
                             "plans": len(part), "fills": sum(r["filled"] for r in part)})
        skip = [r for r in rows if r["action"] == "skip"]
        cal.update(predicted_c_per_contract=round(100 * sum(pred) / len(pred), 3),
                   actual_c_per_contract=round(100 * sum(real_v) / len(real_v), 3),
                   rank_correlation=spearman(pred, real_v), bins_low_to_high=bins,
                   plans_in_skip_groups=len(skip), skip_groups_actual_usd=round(sum(r["v_full"] for r in skip), 2))
    fills = sorted((r for r in rows if r["filled"]), key=lambda r: r["v_size"])
    notable = [{"player_or_game": r["o"].get("player") or data.names.get("-".join(r["o"]["rest_id"].split("-")[:2]), ""), "market": r["o"]["market_type"],
                "sport": r["o"]["sport"], "taken": bool(r["chosen"]), "status": r["o"]["hedge_status"],
                "contracts": round(r["size"], 2), "value_usd": round(r["v_size"], 2)} for r in fills[:3] + fills[-3:]]
    return {"day": day, "utc_window": [b[0] + "Z", b[1] + "Z"], "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "take_vs_skip": take_skip, "ranking": rank, "hedger": hedger, "gaps": gap, "learner": cal,
            "orders_pulled_hedge_moved": sum(in_day(c.get("resolved_ts"), b) for c in data.cancels),
            "notable_fills": notable,
            "accounting": "conservative: hedges at the worse of book/price at fill; leftovers still open count as total "
                          "losses; skipped plans valued at the size the trader would have used"}


# ------------------------------------------------------------------------------------------------ journal
def journal():
    return [json.loads(x) for x in JOURNAL.read_text().splitlines() if x.strip()] if JOURNAL.exists() else []


def next_day(data, today=None):
    """Oldest complete Arizona day (up to yesterday, at most MAX_BACKLOG_DAYS back) without a post-mortem."""
    today = today or datetime.now(AZ).strftime("%Y-%m-%d")
    done = {r["day"] for r in journal()}
    first = (datetime.fromisoformat(today) - timedelta(days=MAX_BACKLOG_DAYS)).strftime("%Y-%m-%d")
    for d in data.days():
        if first <= d < today and d not in done:
            return d
    return None


def headline(m):
    t = m["take_vs_skip"]
    best = max(((k, v["value_usd"]) for k, v in m["ranking"].items() if "hindsight" not in k), key=lambda x: x[1])
    return {"taken_usd": t["taken"]["value_usd"], "taken_fills": t["taken"]["fills"],
            "skipped_usd": t["skipped_at_trader_size"]["value_usd"], "full_size_usd": t["full_size_all_plans_usd"],
            "best_ranking": best[0], "best_ranking_usd": best[1],
            "hindsight_usd": m["ranking"]["perfect hindsight (ceiling)"]["value_usd"],
            "hedger_fills": m["hedger"]["fills_seen"], "hedger_median_s": m["hedger"]["median_seconds_fill_to_hedge"],
            "catchable_gaps": m["gaps"]["catchable"], "catchable_gaps_usd": m["gaps"]["catchable_value_usd"],
            "learner_predicted_c": m["learner"].get("predicted_c_per_contract"),
            "learner_actual_c": m["learner"].get("actual_c_per_contract"),
            "learner_rank_corr": m["learner"].get("rank_correlation")}


def save(entry, m):
    missing = [k for k in PM_KEYS if k not in entry]
    if missing:
        raise ValueError(f"post-mortem is missing {missing}")
    if entry["day"] != m["day"]:
        raise ValueError(f"the day to write is {m['day']}, not {entry['day']}")
    if not isinstance(entry["hypotheses"], list) or len(entry["hypotheses"]) > 3:
        raise ValueError("hypotheses must be a list of 0-3 {statement, how_to_test}")
    for h in entry["hypotheses"]:
        if not {"statement", "how_to_test"} <= set(h):
            raise ValueError("each hypothesis needs statement and how_to_test")
        h.setdefault("status", "untested")
    if any(r["day"] == m["day"] for r in journal()):
        raise ValueError(f"{m['day']} already has a post-mortem")
    row = {**{k: entry[k] for k in PM_KEYS}, "numbers": headline(m),
           "written_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    OUT.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    LATEST.write_text(json.dumps(m, indent=1))
    return row


def bundle(day, data):
    b = day_bounds(day)
    rej = Counter()
    for ts, r in data.runs.items():
        if in_day(ts, b):
            rej.update(json.loads(r.get("rejections") or "{}"))
    prev = journal()
    rl = rules()
    return {"day": day, "metrics": metrics(day, data),
            "trader_rejections_that_day": dict(rej.most_common(8)),
            "learner_changes_that_day": [c for c in data.changes if in_day(c.get("ts_utc"), b)][:20],
            "rules": {"kalshi_budget": rl["kalshi"], "max_cost_per_leg": rl["leg"], "max_cost_per_prop_leg": rl["prop_leg"],
                      "min_edge_c": rl["min_edge"] * 100, "max_contracts": rl["max_c"]},
            "previous_post_mortems": [{k: p.get(k) for k in ("day", "lesson", "hypotheses", "numbers")} for p in prev[-3:]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["metrics", "bundle", "save"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--day")
    ap.add_argument("--data", help="the data/ folder from the price-data branch (default: ./data)")
    ap.add_argument("--hedger-dir", help="folder with fast_hedges.csv and gap_episodes.csv (default: origin/hedger-data)")
    a = ap.parse_args()
    data = Data(a.data, a.hedger_dir)
    if a.cmd == "metrics":
        day = a.day or (datetime.now(AZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        print(json.dumps(metrics(day, data), indent=1))
        return
    day = a.day or next_day(data)
    if not day:
        print("DONE: every complete day already has a post-mortem")
        return
    if a.cmd == "bundle":
        print(json.dumps(bundle(day, data), indent=1))
    else:
        row = save(json.loads(Path(a.file).read_text()), metrics(day, data))
        print(f"saved post-mortem for {row['day']}: {row['lesson']}")


if __name__ == "__main__":
    main()
