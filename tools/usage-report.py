#!/usr/bin/env python3
"""Build an HTML report of Claude plan-limit use from the usage-meter data.

    python tools/usage-report.py            # writes reports/usage-report.html and opens it
    python tools/usage-report.py --no-open  # just write it

Reads ~/.claude/usage-meter (every machine's request ledger, plan-limit readings and plan
history, and the 400-day token ledger) and shows:
  * limit % used / remaining over time, with the plan change marked;
  * usage by month, week, day, hour or minute: API-equivalent $, tokens, requests, limit % change;
  * what was running: sessions, projects, models, effort, main vs subagents, skills, MCP servers;
  * every step of the limit % with the requests that moved it;
  * how much usage 1% of the limit buys on each plan, and whether your use would fit on the
    cheaper plan (how often, and for how long, you would have hit its limit).

Edit the settings below (or pass the flags) to match your plans and prices.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import json
import os
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "collector"))
import umeter  # noqa: E402

# ------------------------------------------------------------------ settings

CURRENCY = "NZD"          # what you pay your plan in
USD_TO_LOCAL = 1.70       # 1 USD in CURRENCY, for API-equivalent $ (API list prices are in USD)
PLAN_BEFORE = "Max"       # the plan before the upgrade
PLAN_AFTER = "Ultra"      # the plan after it
PLAN_PRICES = {           # monthly price in CURRENCY; None = not set
    "Max": 200,
    "Ultra": 400,
}
UPGRADE_AT = None         # local time, e.g. "2026-11-13 13:40"; None = find it in the limit readings

HOUR = 3_600_000
DAY = 24 * HOUR
WINDOW = {"five_hour": 5 * HOUR, "seven_day": 7 * DAY}

# ------------------------------------------------------------------ loading


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def project_of(cwd: str) -> str:
    if not cwd:
        return "(unknown)"
    p = cwd.replace("/", "\\")
    if "\\.claude\\worktrees\\" in p:
        p = p.split("\\.claude\\worktrees\\")[0]
    return p.rstrip("\\").split("\\")[-1] or p


class Tables:
    def __init__(self):
        self.lists: dict = {}
        self.ix: dict = {}

    def get(self, kind: str, key, value=None) -> int:
        if key is None or key == "":
            return -1
        ix = self.ix.setdefault(kind, {})
        hit = ix.get(key)
        if hit is None:
            hit = len(self.lists.setdefault(kind, []))
            self.lists[kind].append(value if value is not None else key)
            ix[key] = hit
        return hit

    def out(self, kind):
        return self.lists.get(kind, [])


def load_requests(root: str, prices):
    t = Tables()
    files = sorted(glob.glob(os.path.join(root, "hosts", "*", "days", "*.json")))
    docs = [d for d in (load(f, None) for f in files) if d and "reqs" in d]
    own = set()
    for d in docs:
        sess = d.get("sessions", {})
        for r in d["reqs"]:
            if not (sess.get(d["s"][r[1]]) or {}).get("remote"):
                own.add(r[12])
    seen = set()
    rows = []
    for d in docs:
        host = t.get("host", d.get("host", "?"))
        sess = d.get("sessions", {})
        prompts = d.get("prompts", {})
        agents = d.get("agents", {})
        for r in d["reqs"]:
            mid = r[12]
            sid = d["s"][r[1]]
            meta = sess.get(sid) or {}
            if mid in seen or (meta.get("remote") and mid in own):
                continue
            seen.add(mid)
            si = t.get("sess", sid, [meta.get("title") or sid[:8], project_of(meta.get("cwd", "")),
                                     host, meta.get("entry", ""), meta.get("cwd", "")])
            pi = -1
            if r[2] >= 0:
                pid = d["p"][r[2]]
                pr = prompts.get(pid) or {}
                pi = t.get("prompt", pid, [pr.get("text", ""), si, pr.get("at", r[0])])
            ai = -1
            if r[3] >= 0:
                aid = d["a"][r[3]]
                ag = agents.get(aid) or {}
                ai = t.get("agent", aid, [ag.get("type") or "subagent", ag.get("desc", ""), si])
            model = d["m"][r[4]] if 0 <= r[4] < len(d["m"]) else ""
            mi = t.get("model", model or "(unknown)")
            ei = t.get("effort", d["e"][r[13]]) if len(r) > 13 and r[13] >= 0 else -1
            ki = t.get("skill", d["k"][r[14]]) if len(r) > 14 and r[14] >= 0 else -1
            xi = t.get("mcp", d["x"][r[15]]) if len(r) > 15 and r[15] >= 0 else -1
            usd = prices.usd(model, r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[0])
            pusd = prices.usd(model, r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[0], plan=True)
            rows.append([r[0], si, pi, ai, mi, ei, ki, xi, r[5], r[6], r[7], r[8], r[9],
                         round(usd, 5), round(pusd, 5), host, 1 if r[11] else 0])
    rows.sort(key=lambda r: r[0])
    return rows, t


def load_limits(root: str):
    samples = {}
    paths = glob.glob(os.path.join(root, "hosts", "*", "limits.json")) + glob.glob(os.path.join(root, "local", "limits", "*.json"))
    for p in paths:
        for s in load(p, {}).get("samples", []):
            if isinstance(s, list) and len(s) >= 4 and s[1] in WINDOW:
                samples[(s[0], s[1])] = [s[0], s[1], s[2], s[3]]
    return sorted(samples.values(), key=lambda s: (s[0], s[1]))


def load_plan_changes(root: str):
    rows = []
    for p in glob.glob(os.path.join(root, "hosts", "*", "plan.json")):
        rows += load(p, {}).get("changes", [])
    return sorted(rows, key=lambda r: r[0])


def load_token_ledger(root: str, prices):
    """The collector's 400-day ledger: per local day and model, priced at that day's list prices."""
    doc = load(os.path.join(root, "local", "tokens.json"), {})
    out = []
    for date, models in sorted((doc.get("days") or {}).items()):
        ts = int(dt.datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000) + 12 * HOUR
        calls = i = o = cr = cw = 0
        usd = 0.0
        for key, v in models.items():
            model, fast = key.split("|")[0], key.split("|")[1] == "1"
            calls += v[0]; i += v[1]; o += v[2]; cr += v[3]; cw += v[4] + v[5]
            usd += prices.usd(model, v[1], v[2], v[3], v[4], v[5], v[6], fast, ts, prompt=0)
        out.append([date, calls, i, o, cr, cw, round(usd, 2)])
    return out


# ------------------------------------------------------------------ analysis


def local_ms(text: str) -> int:
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() * 1000)


def find_upgrade(limits) -> int | None:
    """A 7-day reading that falls by 30+ points while its reset time stays put: the plan changed."""
    prev = None
    found = None
    for s in limits:
        if s[1] != "seven_day":
            continue
        if prev and abs(s[3] - prev[3]) < HOUR and prev[2] - s[2] >= 30:
            found = (prev[0] + s[0]) // 2
        prev = s
    return found


def plan_periods(limits, changes, args):
    up = local_ms(args.upgrade) if args.upgrade else find_upgrade(limits)
    prices = dict(PLAN_PRICES)
    if args.max_price is not None:
        prices[PLAN_BEFORE] = args.max_price
    if args.ultra_price is not None:
        prices[PLAN_AFTER] = args.ultra_price
    periods = []
    if up:
        periods = [{"name": PLAN_BEFORE, "from": 0, "to": up}, {"name": PLAN_AFTER, "from": up, "to": None}]
    else:
        periods = [{"name": (changes[-1][1] if changes else PLAN_AFTER) or PLAN_AFTER, "from": 0, "to": None}]
    # later recorded changes (a downgrade, say) start new periods
    for ch in changes:
        name = ch[1] or ch[3]
        if ch[0] > periods[-1]["from"] and name and name.lower() != periods[-1]["name"].lower():
            periods[-1]["to"] = ch[0]
            periods.append({"name": name, "from": ch[0], "to": None})
    for p in periods:
        p["price"] = prices.get(p["name"])
        p["tier"] = next((c[2] for c in reversed(changes) if (c[1] or "").lower() == p["name"].lower()), "")
    return periods, up, bool(args.upgrade)


def period_of(periods, ts):
    for i, p in enumerate(periods):
        if ts >= p["from"] and (p["to"] is None or ts < p["to"]):
            return i
    return len(periods) - 1


def segments(limits, kind, periods):
    """Runs of readings in one limit window and one plan period."""
    out = []
    cur = []
    for s in (x for x in limits if x[1] == kind):
        if cur:
            last = cur[-1]
            if abs(s[3] - last[3]) >= HOUR or period_of(periods, s[0]) != period_of(periods, last[0]):
                out.append(cur)
                cur = []
        cur.append(s)
    if cur:
        out.append(cur)
    return out


def capacity(rows, limits, periods):
    """API-equivalent $ (plan pricing) per 1% of each window, per plan period."""
    ts = [r[0] for r in rows]
    first_req = ts[0] if ts else 0
    cum = [0.0]
    for r in rows:
        cum.append(cum[-1] + r[14])

    def usd_between(a, b):
        return cum[bisect.bisect_right(ts, b)] - cum[bisect.bisect_right(ts, a)]

    cap = [{} for _ in periods]
    for kind in WINDOW:
        acc = [[0.0, 0.0] for _ in periods]
        for seg in segments(limits, kind, periods):
            if len(seg) < 2 or seg[0][0] < first_req:
                continue
            lo = min(seg, key=lambda s: s[2])
            hi = max((s for s in seg if s[0] >= lo[0]), key=lambda s: s[2])
            dp = hi[2] - lo[2]
            if dp < 3:
                continue
            u = usd_between(lo[0], hi[0])
            if u <= 0:
                continue
            pi = period_of(periods, lo[0])
            acc[pi][0] += u
            acc[pi][1] += dp
        for pi, (u, dp) in enumerate(acc):
            cap[pi][kind] = {"usdPer1": round(u / dp, 4) if dp else None, "usd": round(u, 2), "points": dp}
    return cap


def steps(rows, limits):
    """Every change of a limit reading, with the requests made since the % last changed
    (the use that moved it): [since, at, kind, from, to, resets, firstReq, endReq, isReset]."""
    ts = [r[0] for r in rows]
    out = []
    for kind in WINDOW:
        prev = None
        since = None  # when the current value was first read
        for s in (x for x in limits if x[1] == kind):
            if prev and s[2] != prev[2]:
                reset = abs(s[3] - prev[3]) >= HOUR
                i0 = bisect.bisect_right(ts, since)
                i1 = bisect.bisect_right(ts, s[0])
                out.append([since, s[0], 0 if kind == "five_hour" else 1, prev[2], s[2], s[3], i0, i1, 1 if reset else 0])
                since = s[0]
            elif prev is None:
                since = s[0]
            prev = s
    out.sort(key=lambda x: x[1])
    return out


def window_table(rows, limits, periods, cap):
    """Each 5-hour and 7-day window seen: the highest reading, the plan, and what the same use
    would have come to on each plan (from the $ per 1% the readings give)."""
    ts = [r[0] for r in rows]
    first_req = ts[0] if ts else 0
    out = []
    for kind, span in WINDOW.items():
        resets = {}
        for s in (x for x in limits if x[1] == kind):
            key = round(s[3] / (10 * 60_000))
            w = resets.setdefault(key, {"reset": s[3], "peak": 0, "first": s[0], "last": s[0]})
            w["peak"] = max(w["peak"], s[2])
            w["last"] = s[0]
        for w in sorted(resets.values(), key=lambda w: w["reset"]):
            start = w["reset"] - span
            pi = period_of(periods, max(w["first"], start))
            i0 = bisect.bisect_left(ts, start)
            i1 = bisect.bisect_right(ts, w["reset"])
            usd = sum(r[14] for r in rows[i0:i1])
            covered = start >= first_req
            est = {}
            lock = {}
            for qi, p in enumerate(periods):
                per1 = (cap[qi].get(kind) or {}).get("usdPer1")
                if not per1 or not covered:
                    continue
                est[p["name"]] = round(usd / per1, 1)
                # when would the 100% mark have been crossed, and how long until the reset
                run = 0.0
                for r in rows[i0:i1]:
                    run += r[14]
                    if run >= per1 * 100:
                        lock[p["name"]] = round((w["reset"] - r[0]) / HOUR, 1)
                        break
            out.append({"kind": kind, "start": start, "reset": w["reset"], "peak": w["peak"], "plan": periods[pi]["name"],
                        "usd": round(usd, 2), "covered": covered, "est": est, "lockHours": lock,
                        "open": w["reset"] > umeter.now_ms()})
    return out


# ------------------------------------------------------------------ irregularities

CHUNK_PCT = {0: 10, 1: 5}   # sum this many % points of steps into one measurement (5-hour, 7-day)
FLAG = 0.30                # flag a chunk whose $ per 1% is this far from the recent baseline
SHIFT = 0.35               # flag a model whose weekly $ per 1% moves this much


def local(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def nnls(X, y, iters=400):
    """Non-negative least squares by coordinate descent: y ~ X w, w >= 0. Small problems only."""
    m = len(X[0]) if X else 0
    w = [0.0] * m
    col2 = [sum(r[j] ** 2 for r in X) for j in range(m)]
    resid = list(y)
    for _ in range(iters):
        moved = 0.0
        for j in range(m):
            if not col2[j]:
                continue
            g = sum(X[i][j] * resid[i] for i in range(len(X)))
            new = max(0.0, w[j] + g / col2[j])
            d = new - w[j]
            if d:
                for i in range(len(X)):
                    resid[i] -= X[i][j] * d
                w[j] = new
                moved = max(moved, abs(d) / (abs(new) + 1e-12))
        if moved < 1e-6:
            break
    return w


def chunks_of(rows, step_list, periods):
    out = []
    for kind in (0, 1):
        cur = None
        for s in (x for x in step_list if x[2] == kind):
            pi = period_of(periods, s[1])
            drop = s[4] - s[3] <= -10  # the % fell a lot without a reset: a plan change
            if s[8] or drop or (cur and (cur["period"] != pi or abs(cur["reset"] - s[5]) >= HOUR)):
                cur = None  # a reset or plan change ends the run
                if s[8] or drop:
                    continue
            if cur is None:
                cur = {"kind": kind, "period": pi, "reset": s[5], "t0": s[0], "i0": s[6], "dp": 0}
            cur["dp"] += s[4] - s[3]
            cur["t1"], cur["i1"] = s[1], s[7]
            if cur["dp"] >= CHUNK_PCT[kind]:
                out.append(cur)
                cur = None
    for c in out:
        mu, mt = {}, {}
        usd = tok = outt = sub = 0.0
        for r in rows[c["i0"]:c["i1"]]:
            usd += r[14]
            t = r[8] + r[9] + r[10] + r[11] + r[12]
            tok += t
            outt += r[9]
            mu[r[4]] = mu.get(r[4], 0) + r[14]
            mt[r[4]] = mt.get(r[4], 0) + t
            if r[3] >= 0:
                sub += r[14]
        c.update(usd=usd, tok=tok, out=outt, mu=mu, mt=mt, sub=sub / usd if usd else 0,
                 rate=usd / c["dp"] if c["dp"] else None)
    out.sort(key=lambda c: c["t1"])
    return out


def irregularities(rows, step_list, periods, models):
    chunks = chunks_of(rows, step_list, periods)
    findings = []
    kname = {0: "5-hour", 1: "7-day"}

    def fnd(ts, sev, text):
        findings.append({"ts": ts, "sev": sev, "text": text})

    def mname(mi):
        return models[mi].replace("claude-", "") if 0 <= mi < len(models) else "?"

    # 1. each chunk against the median of the chunks just before it (same limit, same plan),
    #    then against what its model mix predicts: only what the mix can't explain is flagged
    def flag_chunks(pfits):
        for i, c in enumerate(chunks):
            prev = [p["rate"] for p in chunks[:i] if p["kind"] == c["kind"] and p["period"] == c["period"] and p["rate"]][-8:]
            base = median(prev) if len(prev) >= 3 else None
            c["base"] = base
            c["dev"] = (c["rate"] / base - 1) if base and c["rate"] is not None else None
            if c["dev"] is None or abs(c["dev"]) < FLAG:
                continue
            top = max(c["mu"].items(), key=lambda kv: kv[1])[0] if c["mu"] else -1
            pf = pfits.get((c["kind"], c["period"]))
            if pf and c["usd"] >= 0.5:
                per1 = {mm["model"]: mm["per1"] for mm in pf["models"] if mm["per1"] and mm["solid"]}
                mix = sum(u / per1.get(m, pf["overall"]) for m, u in c["mu"].items())
                if mix and abs(c["dp"] / mix - 1) < FLAG:
                    odd = [m for m in c["mu"] if m in per1 and c["mu"][m] / c["usd"] >= 0.25 and abs(per1[m] / pf["overall"] - 1) >= SHIFT]
                    names = ", ".join(f"{mname(m)} (${per1[m]:.2f} per 1% vs ${pf['overall']:.2f} for your usual mix)" for m in odd) or "its model mix"
                    c["why"] = f"expected: explained by {names}"
                    continue
            if c["usd"] < 0.5:
                why = "almost no Claude Code requests were logged while the % rose: claude.ai chat, a machine that hasn't synced, or a limit change"
            elif c["dev"] < 0:
                why = "the % rose faster than these tokens explain, even allowing for the model mix: untracked use (claude.ai chat, an unsynced machine) or the limit costing more per token"
            else:
                why = "the same % took more tokens than usual, even allowing for the model mix: the limit counted this use more cheaply (time of day, cache weighting, or a bigger limit)"
            expected = c["usd"] / base
            c["why"] = why
            fnd(c["t1"], "high" if abs(c["dev"]) >= 2 * FLAG else "med",
                f"{local(c['t0']):%d %b %H:%M}–{local(c['t1']):%H:%M} ({kname[c['kind']]}): +{c['dp']}% for ${c['usd']:.2f} "
                f"(${c['rate']:.2f} per 1% vs ${base:.2f} usual; these tokens should have moved it about {expected:.1f}%). "
                f"Mostly {mname(top)}. {why[0].upper() + why[1:]}.")

    # 2. per-model weight, per plan period and per week: % per $ of each model, by least squares
    fits = []

    def fit(group, label, pi, kind):
        tot = {}
        for c in group:
            for m, u in c["mu"].items():
                tot[m] = tot.get(m, 0) + u
        allu = sum(tot.values())
        if len(group) < 4 or not allu:
            return None
        main = [m for m, u in sorted(tot.items(), key=lambda kv: -kv[1]) if u / allu >= 0.02][:6]
        cols = main + ([-99] if len(main) < len(tot) else [])
        X = [[(c["mu"].get(m, 0) if m != -99 else sum(u for k, u in c["mu"].items() if k not in main)) for m in cols] for c in group]
        y = [c["dp"] for c in group]
        w = nnls(X, y)
        overall = sum(c["usd"] for c in group) / sum(y)
        res = {"label": label, "period": pi, "kind": kind, "chunks": len(group), "overall": round(overall, 3), "models": []}
        for m, wj in zip(cols, w):
            u = tot.get(m, sum(v for k, v in tot.items() if k not in main)) if m != -99 else sum(v for k, v in tot.items() if k not in main)
            t = sum(c["mt"].get(m, 0) for c in group) if m != -99 else sum(v for c in group for k, v in c["mt"].items() if k not in main)
            per1 = 1 / wj if wj > 1e-9 else None
            # a model whose $ only ever rode along with another can't be separated: say so
            solo = sum(1 for c in group if c["usd"] and c["mu"].get(m, 0) / c["usd"] >= 0.6) if m != -99 else 0
            res["models"].append({"model": m, "name": mname(m) if m != -99 else "other", "usd": round(u, 2), "share": round(u / allu, 3),
                                  "per1": round(per1, 3) if per1 else None, "tokPer1": round(per1 * t / u) if per1 and u else None,
                                  "solid": solo >= 2})
        fits.append(res)
        return res

    pfits = {}
    for kind in (0, 1):
        for pi, p in enumerate(periods):
            g = [c for c in chunks if c["kind"] == kind and c["period"] == pi]
            pf = fit(g, p["name"], pi, kind)
            if pf:
                pfits[(kind, pi)] = pf
                for mm in pf["models"]:
                    if mm["solid"] and mm["per1"] and mm["share"] >= 0.03 and abs(mm["per1"] / pf["overall"] - 1) >= SHIFT:
                        more = mm["per1"] < pf["overall"]
                        fnd(p["from"] or (g[0]["t1"] if g else 0), "info",
                            f"On {p['name']} ({kname[kind]}), {mm['name']} counts {'more' if more else 'less'} against your limit than its API price suggests: "
                            f"${mm['per1']:.2f} of it per 1% vs ${pf['overall']:.2f} for your mix overall.")
            weeks = {}
            for c in g:
                d = local(c["t1"])
                monday = (d - dt.timedelta(days=d.weekday())).strftime("%Y-%m-%d")
                weeks.setdefault(monday, []).append(c)
            last = {}
            for wk in sorted(weeks):
                wf = fit(weeks[wk], f"week of {wk}", pi, kind)
                if not wf:
                    continue
                for mm in wf["models"]:
                    if not mm["per1"] or not mm["solid"] or mm["usd"] < 20:
                        continue
                    before = last.get(mm["model"])
                    if before and abs(mm["per1"] / before[1] - 1) >= SHIFT:
                        up = mm["per1"] > before[1]
                        fnd(weeks[wk][-1]["t1"], "med",
                            f"{mm['name']} ({kname[kind]}, {p['name']}): ${mm['per1']:.2f} per 1% in the week of {wk} vs ${before[1]:.2f} in the week of {before[0]} "
                            f"— the same tokens now move the % {'less' if up else 'more'} ({abs(mm['per1'] / before[1] - 1) * 100:.0f}% {'cheaper' if up else 'dearer'}).")
                    last[mm["model"]] = (wk, mm["per1"])

    flag_chunks(pfits)

    # 3. time of day and weekday vs weekend, per plan period and limit
    bands = [("night 0–6", 0, 6), ("morning 6–12", 6, 12), ("afternoon 12–18", 12, 18), ("evening 18–24", 18, 24)]
    times = []
    for kind, pi in [(k, i) for k in (1, 0) for i in range(len(periods))]:
        p = periods[pi]
        g = [c for c in chunks if c["kind"] == kind and c["period"] == pi and c["rate"]]
        if len(g) < 6:
            continue
        overall = sum(c["usd"] for c in g) / sum(c["dp"] for c in g)
        groups = []
        for name, a, b in bands:
            groups.append((name, [c for c in g if a <= local((c["t0"] + c["t1"]) // 2).hour < b]))
        groups.append(("weekdays", [c for c in g if local((c["t0"] + c["t1"]) // 2).weekday() < 5]))
        groups.append(("weekends", [c for c in g if local((c["t0"] + c["t1"]) // 2).weekday() >= 5]))
        for name, cs in groups:
            if not cs:
                continue
            rate = sum(c["usd"] for c in cs) / sum(c["dp"] for c in cs)
            times.append({"kind": kind, "period": p["name"], "band": name, "chunks": len(cs), "per1": round(rate, 3), "vs": round(rate / overall - 1, 3)})
            if len(cs) >= 5 and abs(rate / overall - 1) >= FLAG:
                fnd(g[-1]["t1"], "info",
                    f"On {p['name']}, use in the {name} gets ${rate:.2f} per 1% of the {kname[kind]} limit vs ${overall:.2f} overall "
                    f"({'cheaper' if rate > overall else 'dearer'} by {abs(rate / overall - 1) * 100:.0f}%, {len(cs)} measurements).")

    # 4. drift: the latest chunks against the rest of the same plan period
    for kind in (0, 1):
        for pi, p in enumerate(periods):
            g = [c["rate"] for c in chunks if c["kind"] == kind and c["period"] == pi and c["rate"]]
            if len(g) >= 10:
                recent, rest = median(g[-5:]), median(g[:-5])
                if rest and abs(recent / rest - 1) >= 0.25:
                    fnd(chunks[-1]["t1"], "med",
                        f"Drift on {p['name']} ({kname[kind]}): your last 5 measurements got ${recent:.2f} per 1% vs ${rest:.2f} before "
                        f"({'more' if recent > rest else 'less'} use per % than earlier).")

    findings.sort(key=lambda f: -f["ts"])
    slim = [[c["t0"], c["t1"], c["kind"], c["dp"], round(c["usd"], 3), int(c["tok"]), int(c["out"]), c["period"],
             round(c["base"], 3) if c.get("base") else None, round(c["dev"], 3) if c.get("dev") is not None else None,
             {str(k): round(v, 3) for k, v in c["mu"].items()}, round(c["sub"], 3), c["i0"], c["i1"], c.get("why", "")] for c in chunks]
    return {"chunks": slim, "fits": fits, "times": times, "findings": findings, "flag": FLAG}


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=umeter.ROOT, help="usage-meter data folder")
    ap.add_argument("--out", default=os.path.join(REPO, "reports", "usage-report.html"))
    ap.add_argument("--upgrade", default=UPGRADE_AT, help='local time of the plan change, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--max-price", type=float, default=None, help=f"{PLAN_BEFORE} monthly price in {CURRENCY}")
    ap.add_argument("--ultra-price", type=float, default=None, help=f"{PLAN_AFTER} monthly price in {CURRENCY}")
    ap.add_argument("--fx", type=float, default=USD_TO_LOCAL, help=f"1 USD in {CURRENCY}")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    prices = umeter.Prices()
    rows, t = load_requests(args.root, prices)
    limits = load_limits(args.root)
    changes = load_plan_changes(args.root)
    periods, up, up_given = plan_periods(limits, changes, args)
    cap = capacity(rows, limits, periods)
    for p, c in zip(periods, cap):
        p["cap"] = c
    step_list = steps(rows, limits)
    status = [load(p, {}) for p in glob.glob(os.path.join(args.root, "hosts", "*", "status.json"))]
    data = {
        "built": umeter.now_ms(),
        "currency": CURRENCY,
        "fx": args.fx,
        "upgrade": up,
        "upgradeGiven": up_given,
        "periods": periods,
        "planChanges": changes,
        "hosts": t.out("host"),
        "status": [{"host": s.get("host"), "updatedAt": s.get("updatedAt"), "plan": s.get("plan")} for s in status],
        "sessions": t.out("sess"),
        "prompts": t.out("prompt"),
        "agents": t.out("agent"),
        "models": t.out("model"),
        "efforts": t.out("effort"),
        "skills": t.out("skill"),
        "mcp": t.out("mcp"),
        "reqs": rows,
        "limits": [[s[0], 0 if s[1] == "five_hour" else 1, s[2], s[3]] for s in limits],
        "steps": step_list,
        "irr": irregularities(rows, step_list, periods, t.out("model")),
        "windows": window_table(rows, limits, periods, cap),
        "daily": load_token_ledger(args.root, prices),
    }
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(HERE, "usage-report.html"), encoding="utf-8") as f:
        html = f.read().replace("/*__DATA__*/null", blob)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{len(rows)} requests, {len(limits)} limit readings, {len(data['daily'])} ledger days -> {os.path.abspath(args.out)}")
    if up:
        print(f"plan change: {dt.datetime.fromtimestamp(up / 1000):%Y-%m-%d %H:%M} ({'given' if up_given else 'found in the readings'})")
    for p in periods:
        c7 = (p["cap"].get("seven_day") or {}).get("usdPer1")
        print(f"  {p['name']}: 1% of the 7-day limit ~ ${c7} API-equivalent" if c7 else f"  {p['name']}: not enough readings yet")
    for f in data["irr"]["findings"][:12]:
        print(f"  [{f['sev']}] {f['text']}")
    if not args.no_open:
        webbrowser.open("file:///" + os.path.abspath(args.out).replace("\\", "/"))


if __name__ == "__main__":
    main()
