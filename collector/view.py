"""Builds ~/.claude/usage-meter/local/view.json: everything the mod draws.

All the arithmetic runs here, in the collector's own process, so the mod
inside Claude Code only reads one small file and draws it. Python 3.9+.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import os
import statistics
import sys
import time

HOUR = 3_600_000
DAY = 24 * HOUR
WINDOW_MS = {"five_hour": 5 * HOUR, "seven_day": 7 * DAY}
WINDOW_LABEL = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d Opus", "seven_day_sonnet": "7d Sonnet"}
WATCH_GAP = 12 * 60_000  # sessions save a reading on every change and every 10 min, so a gap this short means someone was watching
# Haiku 5.5 costs 5x more per token on the API once a prompt passes 100k. Whether the
# plan counts it that way is unknown, so long prompts get a weight of their own.
FAMS = ("opus", "sonnet", "haiku", "haiku-long", "fable", "other")
FAM_LABEL = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "haiku-long": "Haiku, prompts over 100k", "fable": "Fable", "other": "Other"}
SHOWN = ("opus", "sonnet", "haiku", "haiku-long")
HISTORY_DAYS = 14
# The token ledger (local/tokens.json): every day's tokens per model, kept long after the
# day files (35 days) and the transcripts (30 days) are gone, and priced at today's list
# prices whenever it is read. Days younger than TOKENS_FIRM_DAYS are rebuilt from the day
# files on every build; older days only ever grow.
TOKENS_KEEP_DAYS = 400
TOKENS_FIRM_DAYS = 28
TOKENS_DAYS_SHOWN = 31
TOP = 8
REBUILD_TOKENS = 50_000  # one request writing this much to the cache re-sent most of a context
# 7 day pace. Nobody uses Claude between midnight and 8am local, so those hours
# count for nothing, and the awake hours count by how busy each hour of the day
# has been over the last 7 days (the profile). The rate is a blend of the last
# 30 min, 1h, 2h and 6h and the window so far, each per profile-weighted hour;
# the forecast runs that rate over the profile-weighted hours left to the reset.
WAKE_HOUR = 8
PACE_LOOKS = (30 * 60_000, HOUR, 2 * HOUR, 6 * HOUR)  # then the window so far
# Lookbacks overlap, so the last 30 min already counts in all four. These are the
# starting weights; replaying past readings replaces them once it has the evidence.
PACE_DEFAULT = (0.10, 0.15, 0.15, 0.25, 0.35)
PACE_GRID = (0, 1, 2, 3)  # replay candidates: every mix of these, scaled to sum to 1
# As the reset nears the window hands weight to the last 30 min and hour, down to a
# floor, because then the question is today's pace, not the week's.
PACE_WINDOW_FLOOR = 0.05
PACE_SLIDE_FROM = 32 * HOUR  # awake time left (two awake days) below which the weight slides
PACE_SLIDE_TO = HOUR  # the lookbacks up to this long (30 min, 1h) take what the window gives up
PACE_MIN_AWAKE = 10 * 60_000  # a lookback with less awake time than this says nothing
PACE_HISTORY = 15 * DAY  # request timeline kept for the replay: last window and this one
PACE_PRIOR_HOURS = 2.0  # each hour of the profile starts at its prior, worth this many hours of data
# Off (the default): midnight to 8am stays zero and overnight requests are left
# out of every rate. On: those hours start at zero and overnight use in the data
# moves them, as for every other hour. Overnight use mostly follows an early
# reset (spare headroom spent on background runs), which isn't a normal week.
PACE_LEARN_NIGHT = False
PACE_REPLAY_EVERY = 30 * 60_000
PACE_REPLAY_GAP = 2 * HOUR  # one replayed forecast per this much time
PACE_HORIZONS = (6 * HOUR, DAY)  # replayed forecasts are checked this far ahead, and at the reset
PACE_MIN_PAIRS = 30
PACE_MIN_DAYS = 3
PACE_MUST_BEAT = 0.9  # replayed weights must cut the default's error by a tenth
FLAT = [0.0] * WAKE_HOUR + [1.0] * (24 - WAKE_HOUR)


def _awake_spans(a: int, b: int):
    """(from, to) ms of each local 8am-to-midnight stretch overlapping (a, b]."""
    day = dt.datetime.fromtimestamp(a / 1000).date()
    while True:
        wake = int(dt.datetime.combine(day, dt.time(WAKE_HOUR)).timestamp() * 1000)
        if wake >= b:
            return
        day += dt.timedelta(days=1)
        midnight = int(dt.datetime.combine(day, dt.time()).timestamp() * 1000)
        lo, hi = max(a, wake), min(b, midnight)
        if hi > lo:
            yield lo, hi


def _hour_pieces(a: int, b: int):
    """(from, to, local hour) for each piece of (a, b] inside one local clock hour."""
    t = a
    while t < b:
        lt = dt.datetime.fromtimestamp(t / 1000)
        nxt = t - (lt.minute * 60 + lt.second) * 1000 - lt.microsecond // 1000 + HOUR
        hi = min(b, nxt)
        yield t, hi, lt.hour
        t = hi


def awake_ms(a: int, b: int) -> int:
    """Awake time in (a, b]: the hours between midnight and 8am don't count."""
    return sum(hi - lo for lo, hi in _awake_spans(a, b)) if b > a else 0


def shaped_ms(a: int, b: int, prof: list) -> float:
    """Time in (a, b], each hour counted by how busy that hour of the day usually is."""
    return sum((hi - lo) * prof[h] for lo, hi, h in _hour_pieces(a, b)) if b > a else 0.0


def after_shaped(t: int, need: float, limit: int, prof: list) -> int | None:
    """When `need` ms of profile-weighted time past t will have gone by, or None if not before limit."""
    if need <= 0:
        return t
    for lo, hi, h in _hour_pieces(t, limit):
        got = (hi - lo) * prof[h]
        if got >= need:
            return int(lo + need / prof[h])
        need -= got
    return None


def learn_profile(pts, a: int, b: int, skip=()) -> list:
    """How busy each local hour of the day is relative to the 8am-midnight
    average (1.0), from usage in (a, b]. Each hour starts at its prior (FLAT:
    average by day, zero overnight) with PACE_PRIOR_HOURS of weight, so a single
    heavy evening doesn't define every evening. Time inside a skip span (from,
    to) is left out."""
    use = [0.0] * 24
    seen = [0.0] * 24
    for lo, hi, h in _hour_pieces(a, b):
        if any(f <= lo < t for f, t in skip):
            continue
        if h >= WAKE_HOUR or PACE_LEARN_NIGHT:
            use[h] += pts(lo, hi)
            seen[h] += (hi - lo) / HOUR
    total, hours = sum(use), sum(seen)
    if total < 1.0 or hours < 24:
        return list(FLAT)
    mean = total / hours
    raw = [0.0] * 24
    for h in range(24):
        if seen[h] or h >= WAKE_HOUR:
            v = (use[h] / mean + PACE_PRIOR_HOURS * FLAT[h]) / (seen[h] + PACE_PRIOR_HOURS)
            raw[h] = min(3.0, max(0.25, v) if h >= WAKE_HOUR else v)
    avg = sum(raw[WAKE_HOUR:]) / (24 - WAKE_HOUR)
    return [v / avg for v in raw]


def _counted(pts, a: int, b: int, prof: list) -> float:
    """Recorded points in (a, b] from hours the profile counts (overnight ones
    are left out when the profile holds them at zero)."""
    return sum(pts(lo, hi) for lo, hi, h in _hour_pieces(a, b) if prof[h] > 0)


def pace_inputs(t: int, pct: float, start, resets, pts, prof: list) -> dict:
    """Rates (% per profile-weighted hour) for each lookback and the window so far,
    as of time t with the meter at pct. pts(a, b) is Claude Code's recorded points,
    or None before any weights are known (then only the window counts)."""
    # the meter also counts claude.ai and the phone: scale recorded points up to it
    rec = pts(start, t) if (pts and start is not None) else 0.0
    scale = min(3.0, max(1.0, (pct + 0.5) / rec)) if rec >= 1.0 else 1.0
    rates = []
    for look in PACE_LOOKS:
        since = max(start, t - look) if start is not None else t - look
        den = shaped_ms(since, t, prof)
        if pts and den >= PACE_MIN_AWAKE:
            rates.append(_counted(pts, since, t, prof) * scale / (den / HOUR))
        else:
            rates.append(None)
    win = None
    if start is not None:
        den = shaped_ms(start, t, prof)
        if den >= PACE_MIN_AWAKE:
            night = rec - _counted(pts, start, t, prof) if pts else 0.0
            win = max(0.0, pct + 0.5 - scale * night) / (den / HOUR)
    rates.append(win)
    return {"rates": rates, "awakeLeft": shaped_ms(t, resets, prof) if resets else None}


def pace_blend(inp: dict, weights) -> float | None:
    """One rate from the lookbacks, the window handing weight to the last 30 min
    and hour as the reset nears."""
    w = list(weights)
    left = inp["awakeLeft"]
    slide = min(1.0, left / PACE_SLIDE_FROM) if left is not None else 1.0
    if w[-1] > PACE_WINDOW_FLOOR:
        keep = PACE_WINDOW_FLOOR + (w[-1] - PACE_WINDOW_FLOOR) * slide
        near = [i for i, look in enumerate(PACE_LOOKS) if look <= PACE_SLIDE_TO]
        for i in near:
            w[i] += (w[-1] - keep) / len(near)
        w[-1] = keep
    num = den = 0.0
    for r, wt in zip(inp["rates"], w):
        if r is not None and wt > 0:
            num += r * wt
            den += wt
    return num / den if den > 0 else None


RESET_HOLD = 2 * HOUR  # a drop is an early reset only if the meter stays down this long


def segments(rs: list, start: int) -> list:
    """Split one window's readings wherever the meter drops and stays down (an
    early reset): [(effective start, readings)], each piece starting from the last
    reading before its drop. Slower sessions report a stale, lower % for a few
    minutes; those readings are dropped, not taken as resets."""
    out = [(start, [])]
    top = None
    for i, r in enumerate(rs):
        if top is not None and r[2] < top - 2:
            later = [x for x in rs[i + 1:] if x[0] - r[0] <= RESET_HOLD]
            held = rs[-1][0] - r[0] >= RESET_HOLD
            if not held or any(x[2] >= top - 2 for x in later):
                continue  # a lagging reading
            if out[-1][1]:
                out.append((out[-1][1][-1][0], []))
            top = None
        out[-1][1].append(r)
        top = r[2] if top is None else max(top, r[2])
    return [seg for seg in out if seg[1]]


def bonus_spans(ks: list) -> list:
    """(from, to) of every stretch after an early reset, to its window's reset.
    That's spare headroom, often spent on runs left going overnight, so it says
    little about a normal week: the profile and the replay leave it out."""
    by_window: dict = {}
    for s in ks:
        if s[3]:
            by_window.setdefault(round(s[3] / 600_000), []).append(s)
    out = []
    for rs in by_window.values():
        resets = rs[-1][3]
        segs = segments(rs, resets - 7 * DAY)
        for i in range(1, len(segs)):
            out.append((segs[i][0], segs[i + 1][0] if i + 1 < len(segs) else resets))
    return out


_GRID = None


def pace_grid() -> list:
    global _GRID
    if _GRID is not None:
        return _GRID
    out = set()
    n = len(PACE_LOOKS) + 1

    def walk(prefix):
        if len(prefix) == n:
            s = sum(prefix)
            if s:
                out.add(tuple(round(v / s, 3) for v in prefix))
            return
        for g in PACE_GRID:
            walk(prefix + [g])
    walk([])
    _GRID = sorted(out)
    return _GRID


def now_ms() -> int:
    return int(time.time() * 1000)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


REPLACE_TRIES = 8  # Windows: about 1.3 s in all, longer than any reader holds a file


def replace_file(tmp: str, path: str) -> None:
    """os.replace, retried on Windows while another process has path open (the
    mod reading view.json, a peer fetching a day file): Windows refuses to replace
    an open file with "Access is denied". Gives up and removes tmp if it stays open."""
    for i in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or i == REPLACE_TRIES - 1:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.01 * 2 ** i)


def write_json(path: str, data) -> str:
    text = json.dumps(data, separators=(",", ":"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    replace_file(tmp, path)
    return text


def local_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def project_of(cwd: str, home: str) -> str:
    if not cwd:
        return "?"
    root = cwd.split("/.claude/worktrees/")[0].rstrip("/")
    if root in ("", home):
        return "~ (home)"
    return os.path.basename(root) or root


class Agg:
    __slots__ = ("usd", "i", "o", "cr", "cw", "reqs", "first", "last", "p5", "p7")

    def __init__(self) -> None:
        self.p5 = 0.0
        self.p7 = 0.0
        self.usd = 0.0
        self.i = self.o = self.cr = self.cw = 0
        self.reqs = 0
        self.first = 0
        self.last = 0

    def add(self, ts: int, usd: float, r: list, pts=(0.0, 0.0)) -> None:
        self.p5 += pts[0]
        self.p7 += pts[1]
        self.usd += usd
        self.i += r[5]
        self.o += r[6]
        self.cr += r[7]
        self.cw += r[8] + r[9]
        self.reqs += 1
        if not self.first or ts < self.first:
            self.first = ts
        if ts > self.last:
            self.last = ts

    def tok(self) -> list:
        return [self.i, self.o, self.cr, self.cw]


def bucket(d: dict, key) -> Agg:
    a = d.get(key)
    if a is None:
        a = d[key] = Agg()
    return a


class Entry:
    """One day file of one host, parsed once per change."""

    def __init__(self, path: str, mtime: float, doc: dict, prices) -> None:
        self.path = path
        self.mtime = mtime
        self.doc = doc
        self.host = doc.get("host", "?")
        self.usd = []
        self.plan = []  # priced without API-only changes: what the plan-limit fit learns against
        self.fam = []
        for r in doc["reqs"]:
            m = doc["m"][r[4]] if r[4] < len(doc["m"]) else ""
            self.usd.append(prices.usd(m, r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[0]))
            self.plan.append(prices.usd(m, r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[0], plan=True))
            fam = prices.family(m)
            if fam == "haiku" and prices.is_long(m, r[5] + r[7] + r[8] + r[9]):
                fam = "haiku-long"
            self.fam.append(fam)
        sessions = doc.get("sessions", {})
        self.remote = [bool((sessions.get(doc["s"][r[1]]) or {}).get("remote")) for r in doc["reqs"]]
        self.keep = [True] * len(doc["reqs"])
        self.daily = None  # {(local date, machine, project): usd}, rebuilt when the file or its dedupe changes
        self.tokens = None  # {local date: {"model|fast|long": [calls, in, out, cacheRead, cw5m, cw1h, web]}}, likewise
        self.first = doc["reqs"][0][0] if doc["reqs"] else 0


class Scope:
    """Every breakdown of one span of requests."""

    def __init__(self) -> None:
        self.total = Agg()
        self.rebuilds = 0
        self.rebuild_usd = 0.0
        self.sessions: dict = {}
        self.tasks: dict = {}
        self.dims = {k: {} for k in ("projects", "models", "effort", "agents", "skills", "mcp", "machines")}

    def add(self, ts, usd, r, ctx, pts=(0.0, 0.0)) -> None:
        sid, pid, aid, model, effort, skill, mcp, project, machine = ctx
        self.total.add(ts, usd, r, pts)
        if r[8] + r[9] >= REBUILD_TOKENS:
            self.rebuilds += 1
            self.rebuild_usd += usd
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = {"all": Agg(), "main": Agg(), "agents": {}, "tasks": {}}
        s["all"].add(ts, usd, r, pts)
        if aid:
            bucket(s["agents"], aid).add(ts, usd, r, pts)
        else:
            s["main"].add(ts, usd, r, pts)
        bucket(s["tasks"], pid).add(ts, usd, r, pts)
        bucket(self.tasks, (sid, pid)).add(ts, usd, r, pts)
        d = self.dims
        bucket(d["projects"], project).add(ts, usd, r, pts)
        bucket(d["models"], model).add(ts, usd, r, pts)
        bucket(d["effort"], effort or "default").add(ts, usd, r, pts)
        if skill:
            bucket(d["skills"], skill).add(ts, usd, r, pts)
        if mcp:
            bucket(d["mcp"], mcp).add(ts, usd, r, pts)
        bucket(d["machines"], machine).add(ts, usd, r, pts)


class ViewBuilder:
    def __init__(self, root: str, host: str, prices, home: str) -> None:
        self.root = root
        self.host = host
        self.prices = prices
        self.home = home
        self.entries: dict = {}
        self.group_sig: dict = {}
        self.group_daily: dict = {}
        self.last_body = ""
        self.last_sig = None
        self.last_build = 0.0
        self.tokens_path = os.path.join(root, "local", "tokens.json")
        self.tokens_doc = None  # loaded on first build
        self.pace_cache = None  # (when, weights, info): the replay runs every PACE_REPLAY_EVERY

    # -------------------------------------------------------------- loading

    def load(self, now: int) -> list:
        cutoff = dt.datetime.fromtimestamp((now - 32 * DAY) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        hosts_dir = os.path.join(self.root, "hosts")
        seen = set()
        for host in sorted(os.listdir(hosts_dir)) if os.path.isdir(hosts_dir) else []:
            ddir = os.path.join(hosts_dir, host, "days")
            if not os.path.isdir(ddir):
                continue
            for name in os.listdir(ddir):
                if not name.endswith(".json") or name[:-5] < cutoff:
                    continue
                path = os.path.join(ddir, name)
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                seen.add(path)
                e = self.entries.get(path)
                if e is None or e.mtime != mtime:
                    doc = load_json(path, None)
                    if not doc or "reqs" not in doc:
                        continue
                    self.entries[path] = Entry(path, mtime, doc, self.prices)
        for path in [p for p in self.entries if p not in seen]:
            del self.entries[path]
        return list(self.entries.values())

    def limits(self) -> list:
        samples = {}
        paths = []
        hosts_dir = os.path.join(self.root, "hosts")
        for host in os.listdir(hosts_dir) if os.path.isdir(hosts_dir) else []:
            paths.append(os.path.join(hosts_dir, host, "limits.json"))
        inbox = os.path.join(self.root, "local", "limits")
        for name in os.listdir(inbox) if os.path.isdir(inbox) else []:
            if name.endswith(".json"):
                paths.append(os.path.join(inbox, name))
        for p in paths:
            for s in load_json(p, {}).get("samples", []):
                if isinstance(s, list) and len(s) >= 4:
                    samples[(s[0], s[1])] = s[:4]
        return sorted(samples.values(), key=lambda s: s[0])

    def signature(self, entries: list, lims: list):
        return (tuple(sorted((e.path, e.mtime) for e in entries)), len(lims), lims[-1][0] if lims else 0)

    # -------------------------------------------------------------- dedupe

    def dedupe(self, entries: list) -> None:
        """A remote copy of a session (the desktop app's mirror of an SSH session)
        counts only for messages no machine recorded as its own. Copies share the
        message's timestamp, so only files of the same UTC day are compared."""
        groups: dict = {}
        for e in entries:
            groups.setdefault(e.doc.get("day", ""), []).append(e)
        for day, group in groups.items():
            sig = tuple(sorted((e.path, e.mtime) for e in group))
            if self.group_sig.get(day) == sig:
                continue
            own = set()
            for e in group:
                for i, r in enumerate(e.doc["reqs"]):
                    if not e.remote[i]:
                        own.add(r[12])
            for e in group:
                for i, r in enumerate(e.doc["reqs"]):
                    if e.remote[i]:
                        e.keep[i] = r[12] not in own
                        own.add(r[12])
                e.daily = None
                e.tokens = None
            self.group_sig[day] = sig

    # -------------------------------------------------------------- build

    def maybe_build(self, force: bool = False) -> bool:
        now = now_ms()
        entries = self.load(now)
        lims = self.limits()
        sig = self.signature(entries, lims)
        stale = time.time() - self.last_build
        # at most every 10 s (Refresh forces it), and once a minute when nothing changed
        if not force and sig == self.last_sig and stale < 60:
            return False
        if not force and stale < 10:
            return False
        self.dedupe(entries)
        view, sessions = self.build(entries, lims, now)
        self.last_sig = sig
        self.last_build = time.time()
        body = json.dumps(view, sort_keys=True, separators=(",", ":"))
        if body != self.last_body:
            view["builtAt"] = now
            write_json(os.path.join(self.root, "local", "view.json"), view)
            self.last_body = body
            sdir = os.path.join(self.root, "local", "sessions")
            os.makedirs(sdir, exist_ok=True)
            keep = set()
            for sid, detail in sessions.items():
                keep.add(f"{sid}.json")
                path = os.path.join(sdir, f"{sid}.json")
                if load_json(path, None) != detail:
                    write_json(path, detail)
            for name in os.listdir(sdir):
                if name.endswith(".json") and name not in keep:
                    try:
                        if now - os.stat(os.path.join(sdir, name)).st_mtime * 1000 > DAY:
                            os.remove(os.path.join(sdir, name))
                    except OSError:
                        pass
            return True
        return False

    def build(self, entries: list, lims: list, now: int):
        # metadata from every file
        sessions_meta: dict = {}
        prompts: dict = {}
        agents: dict = {}
        for e in entries:
            for sid, m in e.doc.get("sessions", {}).items():
                prev = sessions_meta.get(sid)
                if prev and m.get("remote") and not prev["remote"]:
                    continue
                sessions_meta[sid] = {
                    "title": m.get("title") or (prev or {}).get("title", ""),
                    "cwd": m.get("cwd") or (prev or {}).get("cwd", ""),
                    "host": e.host,
                    "remote": bool(m.get("remote")),
                }
            for pid, p in e.doc.get("prompts", {}).items():
                prompts.setdefault(pid, p)
            for aid, a in e.doc.get("agents", {}).items():
                agents.setdefault(aid, a)

        # History and the 30-day total come from per-file caches; only files
        # that changed are re-read. Row-level work covers the last 8 days only.
        windows_raw = self.window_bounds(lims, now)
        start5 = windows_raw.get("five_hour", now - 5 * HOUR)
        start7 = windows_raw.get("seven_day", now - 7 * DAY)
        tl_from = now - 8 * DAY
        row_from_day = dt.datetime.fromtimestamp((min(start5, start7, tl_from) - DAY) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        w5, w7 = Scope(), Scope()
        sess_scopes: dict = {}
        active = {}
        daily: dict = {}
        session_usd: dict = {}
        first_ts = min((e.first for e in entries if e.first), default=None)
        month_from_date = local_date(now - 30 * DAY)
        month = 0.0
        for e in entries:
            if e.daily is None:
                e.daily = self.file_daily(e, sessions_meta)
            for (date, machine, project), usd in e.daily.items():
                if date >= month_from_date:
                    month += usd
                dd = daily.setdefault(date, {"hosts": {}, "projects": {}})
                dd["hosts"][machine] = dd["hosts"].get(machine, 0.0) + usd
                dd["projects"][project] = dd["projects"].get(project, 0.0) + usd
        # 1) every recent request's plan-reference $, per model family, as a time line
        feats = []
        for e in entries:
            if e.doc.get("day", "") < row_from_day:
                continue
            for i, r in enumerate(e.doc["reqs"]):
                if e.keep[i] and r[0] >= tl_from:
                    feats.append((r[0], FAMS.index(e.fam[i]), e.plan[i], e.usd[i]))
        feats.sort(key=lambda f: f[0])
        fts = [f[0] for f in feats]
        cum = [[0.0] * len(FAMS)]
        cum_api = [0.0]
        for f in feats:
            row = list(cum[-1])
            row[f[1]] += f[2]
            cum.append(row)
            cum_api.append(cum_api[-1] + f[3])

        def between(a: int, b: int):
            """per-family plan-reference $, and API $, spent in (a, b]"""
            ia = bisect.bisect_right(fts, a)
            ib = bisect.bisect_right(fts, b)
            return [cum[ib][j] - cum[ia][j] for j in range(len(FAMS))], cum_api[ib] - cum_api[ia]

        # the 7 day pace replays the last window too, so it keeps a longer time line
        pace_from = now - PACE_HISTORY
        pace_day = dt.datetime.fromtimestamp((pace_from - DAY) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        pfeats = []
        for e in entries:
            if e.doc.get("day", "") < pace_day:
                continue
            for i, r in enumerate(e.doc["reqs"]):
                if e.keep[i] and r[0] >= pace_from:
                    pfeats.append((r[0], FAMS.index(e.fam[i]), e.plan[i]))
        pfeats.sort(key=lambda f: f[0])
        pts_ts = [f[0] for f in pfeats]
        pcum = [[0.0] * len(FAMS)]
        for f in pfeats:
            row = list(pcum[-1])
            row[f[1]] += f[2]
            pcum.append(row)

        def pace_x(a: int, b: int):
            """per-family plan-reference $ in (a, b], over the longer time line"""
            ia = bisect.bisect_right(pts_ts, a)
            ib = bisect.bisect_right(pts_ts, b)
            return [pcum[ib][j] - pcum[ia][j] for j in range(len(FAMS))]

        # 2) each limit window learns how much of it a dollar of each model uses
        windows = [self.window_view(kind, lims, between, now, first_ts, (pace_x, pace_from)) for kind in self.kinds(lims)]
        windows = [w for w in windows if w]
        k5 = next((w["rates"] for w in windows if w["kind"] == "five_hour" and w["rates"]), None)
        k7 = next((w["rates"] for w in windows if w["kind"] == "seven_day" and w["rates"]), None)

        def pts(e, i):
            f = e.fam[i]
            return (e.plan[i] * k5[f] if k5 else 0.0, e.plan[i] * k7[f] if k7 else 0.0)

        # 3) every breakdown, with each request's share of the limits
        for e in entries:
            if e.doc.get("day", "") < row_from_day:
                continue
            doc = e.doc
            S, P, A, M = doc["s"], doc["p"], doc["a"], doc["m"]
            E, K, X = doc.get("e", []), doc.get("k", []), doc.get("x", [])
            for i, r in enumerate(doc["reqs"]):
                if not e.keep[i]:
                    continue
                ts = r[0]
                if ts < tl_from or (ts < start7 and ts < start5):
                    continue
                usd = e.usd[i]
                sid = S[r[1]]
                meta = sessions_meta.get(sid) or {}
                machine = f"{meta.get('host', e.host)} (remote)" if meta.get("remote") else meta.get("host", e.host)
                project = project_of(meta.get("cwd", ""), self.home)
                model_id = M[r[4]] if r[4] < len(M) else ""
                pid = P[r[2]] if r[2] >= 0 else ""
                aid = A[r[3]] if r[3] >= 0 else ""
                label = self.prices.label(model_id)
                effort = E[r[13]] if len(r) > 13 and r[13] >= 0 else ""
                skill = K[r[14]] if len(r) > 14 and r[14] >= 0 else ""
                mcp = X[r[15]] if len(r) > 15 and r[15] >= 0 else ""
                ctx = (sid, pid, aid, label, effort, skill, mcp, project, machine)
                pt = pts(e, i)
                if ts >= start7:
                    w7.add(ts, usd, r, ctx, pt)
                if ts >= start5:
                    w5.add(ts, usd, r, ctx, pt)
                session_usd[sid] = session_usd.get(sid, 0.0) + usd
                a = active.get(sid)
                if a is None or ts > a:
                    active[sid] = ts
        for scope in (w5, w7):
            self.fix_agent_types(scope, agents)

        # sessions active in the last 6 hours get a detail file of their own
        recent = [sid for sid, last in active.items() if last >= now - 6 * HOUR]
        if recent:
            want = set(recent)
            for e in entries:
                if e.doc.get("day", "") < row_from_day:
                    continue
                doc = e.doc
                S, P, A, M = doc["s"], doc["p"], doc["a"], doc["m"]
                E, K, X = doc.get("e", []), doc.get("k", []), doc.get("x", [])
                for i, r in enumerate(doc["reqs"]):
                    sid = S[r[1]]
                    if sid not in want or not e.keep[i]:
                        continue
                    meta = sessions_meta.get(sid) or {}
                    machine = f"{meta.get('host', e.host)} (remote)" if meta.get("remote") else meta.get("host", e.host)
                    model_id = M[r[4]] if r[4] < len(M) else ""
                    ctx = (
                        sid,
                        P[r[2]] if r[2] >= 0 else "",
                        A[r[3]] if r[3] >= 0 else "",
                        self.prices.label(model_id),
                        E[r[13]] if len(r) > 13 and r[13] >= 0 else "",
                        K[r[14]] if len(r) > 14 and r[14] >= 0 else "",
                        X[r[15]] if len(r) > 15 and r[15] >= 0 else "",
                        project_of(meta.get("cwd", ""), self.home),
                        machine,
                    )
                    scope = sess_scopes.get(sid)
                    if scope is None:
                        scope = sess_scopes[sid] = Scope()
                    scope.add(r[0], e.usd[i], r, ctx, pts(e, i))
            for scope in sess_scopes.values():
                self.fix_agent_types(scope, agents)
        r5, r7 = k5 is not None, k7 is not None

        # typical session size over the last 7 days, for the remaining-budget line
        sizes = sorted(a["all"].usd for a in w7.sessions.values() if a["all"].usd >= 1)
        typical = statistics.median(sizes) if sizes else 0.0
        biggest = sizes[-1] if sizes else 0.0
        for w in windows:
            if w["usdLeft"] is not None and typical > 0:
                w["typicalSessions"] = round(w["usdLeft"] / typical, 1)
        ctx = Ctx(sessions_meta, prompts, agents, r5, r7, now)

        view = {
            "v": 1,
            "host": self.host,
            "windows": windows,
            "window": self.export_scope(w5, ctx),
            "week": self.export_scope(w7, ctx),
            "history": self.history(daily, now),
            "tokens": self.token_ledger(entries, now),
            "monthUsd": round(month, 4),
            "typicalSessionUsd": round(typical, 2),
            "biggestSessionUsd": round(biggest, 2),
            "sessionUsd": {sid: round(u, 4) for sid, u in session_usd.items() if active.get(sid, 0) >= now - 2 * DAY},
            "accuracy": self.accuracy(windows, entries, now),
            "hosts": self.hosts(entries),
            "config": load_json(os.path.join(self.root, "config.json"), {}),
        }
        details = {sid: self.export_scope(sc, ctx) for sid, sc in sess_scopes.items()}
        return view, details

    def file_daily(self, e: Entry, sessions_meta: dict) -> dict:
        out: dict = {}
        S = e.doc["s"]
        for i, r in enumerate(e.doc["reqs"]):
            if not e.keep[i]:
                continue
            meta = sessions_meta.get(S[r[1]]) or {}
            machine = f"{meta.get('host', e.host)} (remote)" if meta.get("remote") else meta.get("host", e.host)
            key = (local_date(r[0]), machine, project_of(meta.get("cwd", ""), self.home))
            out[key] = out.get(key, 0.0) + e.usd[i]
        return out

    def fix_agent_types(self, scope: Scope, agents: dict) -> None:
        types: dict = {}
        for s in scope.sessions.values():
            if s["main"].reqs:
                merge(bucket(types, "main thread"), s["main"])
            for aid, a in s["agents"].items():
                merge(bucket(types, (agents.get(aid) or {}).get("type", "subagent")), a)
        scope.dims["agents"] = types

    # -------------------------------------------------------------- limits

    def kinds(self, lims: list) -> list:
        ks = sorted({s[1] for s in lims}, key=lambda k: WINDOW_MS.get(k, 10**15))
        return ks

    def window_bounds(self, lims: list, now: int) -> dict:
        out = {}
        for kind, dur in WINDOW_MS.items():
            last = next((s for s in reversed(lims) if s[1] == kind), None)
            if last and last[3] and last[3] > now:
                out[kind] = last[3] - dur
        return out

    def window_view(self, kind, lims, between, now, first_ts, pace_line=None):
        ks = [s for s in lims if s[1] == kind]
        if not ks:
            return None
        last = ks[-1]
        dur = WINDOW_MS.get(kind)
        resets = last[3] or None
        is_reset = resets is not None and resets <= now
        pct = 0 if is_reset else last[2]
        start = resets - dur if (resets and dur) else None

        def win(s):
            return round((s[3] or 0) / 600_000)

        # Each whole point is timed by the first reading that showed it, and only
        # counts when some session had read the limit shortly before (it was
        # watching). Lagging readings from slower sessions just prove watching.
        crossings = []
        top = last_seen = cur = None
        for s in ks:
            if win(s) != cur:
                cur, top, last_seen = win(s), s[2], s[0]
                crossings.append(None)
                continue
            if s[2] > top:
                crossings.append((s[0], s[2]) if s[0] - last_seen <= WATCH_GAP else None)
                top = s[2]
            last_seen = s[0]
        stretches = []
        for a, b in zip(crossings, crossings[1:]):
            if a is None or b is None:
                continue
            x, api = between(a[0], b[0])
            if sum(x) > 0.05:
                stretches.append({"from": a[0], "to": b[0], "pctFrom": a[1], "pctTo": b[1], "x": x, "usd": api})

        # Points = sum over model families of (weight x plan-reference $). Each
        # family starts at the overall rate and moves with the evidence.
        recent = stretches[-40:]
        plan_in, usd_in = between(start, now) if (start is not None and not is_reset) else (None, None)
        k = None
        rate_from = "none"
        if recent:
            r0 = sum(s["pctTo"] - s["pctFrom"] for s in recent) / sum(sum(s["x"]) for s in recent)
            k = self.fit(recent, r0)
            rate_from = "learned"
        elif plan_in and sum(plan_in) > 0.5 and pct > 0 and first_ts is not None and start is not None and first_ts <= start:
            # whole-point readings: the true value sits half a point above, on average
            k = [(pct + 0.5) / sum(plan_in)] * len(FAMS)
            rate_from = "estimate"
        est_in = sum(k[j] * plan_in[j] for j in range(len(FAMS))) if (k and plan_in) else None
        # "7" means 7% to just under 8%: recorded usage can't be credited with more
        if k and est_in and est_in > pct + 1:
            f = (pct + 1) / est_in
            k = [v * f for v in k]
            est_in = pct + 1
        rates = dict(zip(FAMS, k)) if k else None
        kref = rates["opus"] if rates else None

        evidence = {f: sum(st["x"][j] for st in recent) * k[j] for j, f in enumerate(FAMS)} if k else {}
        models = []
        if rates:
            for f in FAMS:
                if f in SHOWN or evidence.get(f, 0) >= 0.05:
                    models.append({
                        "family": f, "label": FAM_LABEL[f], "weight": round(rates[f] / kref, 3),
                        "points": round(evidence.get(f, 0.0), 1),
                    })
        out_stretches = []
        per_pt = []
        for st in stretches[-8:]:
            pts_ = st["pctTo"] - st["pctFrom"]
            oe = sum(st["x"][j] * (k[j] / kref) for j in range(len(FAMS))) if k else sum(st["x"])
            per_pt.append(oe / pts_)
            mix = max(range(len(FAMS)), key=lambda j: st["x"][j])
            out_stretches.append({
                "from": st["from"], "to": st["to"], "pctFrom": st["pctFrom"], "pctTo": st["pctTo"],
                "usd": round(st["usd"], 3), "usdPerPoint": round(oe / pts_, 3),
                "opusShare": round(st["x"][0] / (sum(st["x"]) or 1), 2), "mostly": FAM_LABEL[FAMS[mix]],
            })
        spread = round((max(per_pt) - min(per_pt)) / statistics.median(per_pt), 2) if len(per_pt) >= 2 else None
        n = len(stretches)
        if rate_from == "estimate":
            confidence = "rough"
        elif n == 0:
            confidence = "none"
        elif n >= 6 and spread is not None and spread <= 0.25:
            confidence = "high"
        elif n >= 3 and spread is not None and spread <= 0.4:
            confidence = "medium"
        else:
            confidence = "low"

        # Usage outside Claude Code shows up as points recorded usage doesn't
        # explain; under one point is rounding.
        untracked = None
        outside = "unknown"
        if rate_from == "learned" and n >= 3 and (spread is None or spread <= 0.5) and est_in is not None:
            gap = (pct + 0.5) - est_in
            untracked, outside = (round(gap, 1), "some") if gap >= 1.0 else (0.0, "none")

        # Burn from what each model actually used recently: whole-point readings
        # would show no movement for hours. Look back a fifth of the window.
        burn = None
        hours_left = (resets - now) / HOUR if (resets and not is_reset) else None
        pace = None
        if kind == "seven_day":
            # 7 day: see PACE_LOOKS. Whole-point readings sit half a point under
            # the true value on average, so the forecast starts half a point up.
            at_reset = full_at = None
            if not is_reset:
                pts = prof = None
                weights = PACE_DEFAULT
                if k and pace_line:
                    pace_x, pace_from = pace_line

                    def pts(a, b, k=k):
                        x = pace_x(a, b)
                        return sum(k[j] * x[j] for j in range(len(FAMS)))
                    prof = learn_profile(pts, now - 7 * DAY, now, bonus_spans(ks))
                    weights, pace = self.pace_weights(ks, pts, prof, now, pace_from)
                prof = prof or FLAT
                cur = [s for s in ks if s[3] and abs(s[3] - resets) < 600_000]
                since = segments(cur, start)[-1][0] if (cur and start is not None) else start
                burn = pace_blend(pace_inputs(now, pct, since, resets, pts, prof), weights)
                if burn is not None and resets:
                    at_reset = pct + 0.5 + burn * shaped_ms(now, resets, prof) / HOUR
                if burn and burn > 0:
                    full_at = after_shaped(now, (100 - pct - 0.5) / burn * HOUR, resets or now + 7 * DAY, prof)
                if pace is not None:
                    pace = dict(pace, profile=[round(v, 2) for v in prof])
        else:
            if not is_reset and k:
                look = (dur or 5 * HOUR) / 5
                since = max(start, now - look) if start is not None else now - look
                if now - since >= 10 * 60_000:
                    x, _ = between(since, now)
                    points = sum(k[j] * x[j] for j in range(len(FAMS)))
                    burn = points / ((now - since) / HOUR)
            at_reset = pct + burn * hours_left if (burn is not None and hours_left is not None) else None
            full_at = now + (100 - pct) / burn * HOUR if (burn and burn > 0) else None
        usd_left = (100 - pct - 0.5) / kref if kref else None
        return {
            "kind": kind,
            "label": WINDOW_LABEL.get(kind, kind),
            "pct": pct,
            "resetsAt": resets,
            "isReset": is_reset,
            "rate": kref,
            "rates": rates,
            "models": models,
            "rateFrom": rate_from,
            "confidence": confidence,
            "stretches": out_stretches,
            "spread": spread,
            "readings": len(ks),
            "usdInWindow": round(usd_in, 3) if usd_in is not None else None,
            "untrackedPct": untracked,
            "outside": outside,
            "burnPerHour": round(burn, 3) if burn is not None else None,
            "atReset": round(at_reset, 1) if at_reset is not None else None,
            "fullAt": int(full_at) if full_at else None,
            "usdLeft": round(usd_left, 2) if usd_left is not None else None,
            "typicalSessions": None,
            "pace": pace,
        }

    def pace_weights(self, ks: list, pts, prof: list, now: int, pace_from: int):
        """Weights for the 7 day pace, picked by replaying past readings: from a
        reading every PACE_REPLAY_GAP, forecast the rise PACE_HORIZONS ahead and to
        the window's last reading, and keep the mix of PACE_GRID that missed least.
        The defaults stay until there's enough replay and the winner clearly beats
        them. The profile is today's, so the replay sees a little of its own future."""
        c = self.pace_cache
        if c and now - c[0] < PACE_REPLAY_EVERY:
            return c[1], c[2]
        by_window: dict = {}
        for s in ks:
            if s[3]:
                by_window.setdefault(round(s[3] / 600_000), []).append(s)
        pairs = []
        days = set()
        pieces = []
        for rs in by_window.values():
            resets = rs[-1][3]
            if resets - 7 * DAY < pace_from:
                continue
            segs = segments(rs, resets - 7 * DAY)
            for i, (start, seg) in enumerate(segs):
                if i:
                    continue  # after an early reset: see bonus_spans
                # a piece that ended (an early reset, or the reset itself) gives its last reading as the end
                pieces.append((start, resets, seg, seg[-1] if (i < len(segs) - 1 or resets <= now) else None))
        for start, resets, rs, final in pieces:
            rts = [r[0] for r in rs]
            last_t = None
            for r in rs:
                t, pct = r[0], r[2]
                if last_t is not None and t - last_t < PACE_REPLAY_GAP:
                    continue
                if dt.datetime.fromtimestamp(t / 1000).hour < WAKE_HOUR:
                    continue
                targets = []
                for hz in PACE_HORIZONS:
                    i = bisect.bisect_left(rts, t + hz)
                    if i < len(rs) and rts[i] - (t + hz) <= HOUR:
                        targets.append(rs[i])
                if final and final[0] - t > PACE_HORIZONS[0] and final not in targets:
                    targets.append(final)
                if not targets:
                    continue
                last_t = t
                inp = pace_inputs(t, pct, start, resets, pts, prof)
                pairs.append((inp, [(shaped_ms(t, x[0], prof) / HOUR, x[2] - pct, 100 - pct) for x in targets]))
                days.add(local_date(t))
        n = sum(len(tg) for _, tg in pairs)

        def err(w):
            tot = 0.0
            for inp, tg in pairs:
                r = pace_blend(inp, w) or 0.0
                for hours, rise, room in tg:
                    tot += abs(min(r * hours, room) - rise)  # the meter stops at 100
            return tot / n if n else None

        weights = PACE_DEFAULT
        base = err(PACE_DEFAULT)
        best_err = base
        learned = False
        if n >= PACE_MIN_PAIRS and len(days) >= PACE_MIN_DAYS:
            e, w = min((err(w), w) for w in pace_grid())
            if e <= PACE_MUST_BEAT * base:
                weights, best_err, learned = w, e, True
        info = {
            "from": "learned" if learned else "default",
            "weights": dict(zip(("30m", "1h", "2h", "6h", "window"), weights)),
            "error": round(best_err, 2) if best_err is not None else None,
            "defaultError": round(base, 2) if base is not None else None,
            "checks": n,
            "days": len(days),
        }
        self.pace_cache = (now, weights, info)
        return weights, info

    def fit(self, stretches: list, r0: float) -> list:
        """Weights per model family (points per plan-reference $) by least squares,
        each pulled toward r0 with the strength of about one point of evidence."""
        n = len(FAMS)
        A = [[0.0] * n for _ in range(n)]
        b = [0.0] * n
        for st in stretches:
            x, y = st["x"], st["pctTo"] - st["pctFrom"]
            for i in range(n):
                b[i] += x[i] * y
                for j in range(n):
                    A[i][j] += x[i] * x[j]
        lam = 1.0 / (r0 * r0)
        for i in range(n):
            A[i][i] += lam
            b[i] += lam * r0
        k = solve(A, b)
        return [min(max(v, 0.25 * r0), 4 * r0) for v in k]

    # -------------------------------------------------------------- export

    def export_scope(self, sc: Scope, ctx) -> dict:
        def row(label, a: Agg, sub="", extra=None):
            d = {
                "label": label, "sub": sub, "usd": round(a.usd, 4), "tok": a.tok(), "reqs": a.reqs,
                "ms": a.last - a.first if a.reqs > 1 else 0,
                "p5": round(a.p5, 3) if ctx.r5 else None,
                "p7": round(a.p7, 3) if ctx.r7 else None,
            }
            if extra:
                d.update(extra)
            return d

        threads = []
        for sid, s in sorted(sc.sessions.items(), key=lambda kv: -kv[1]["all"].usd)[:30]:
            meta = ctx.sessions.get(sid) or {}
            project = project_of(meta.get("cwd", ""), self.home)
            host = f"{meta.get('host', '?')} (remote)" if meta.get("remote") else meta.get("host", "?")
            kids = []
            if s["agents"]:
                kids.append(row("main thread", s["main"], extra={"depth": 1}))
                order = self.agent_tree(s["agents"], ctx.agents)
                for aid, depth in order[:20]:
                    a = ctx.agents.get(aid) or {}
                    name = f"{a.get('type', 'subagent')}: {a['desc']}" if a.get("desc") else a.get("type", "subagent")
                    kids.append(row(name, s["agents"][aid], extra={"depth": depth}))
            tasks = []
            for pid, a in sorted(s["tasks"].items(), key=lambda kv: -kv[1].usd)[:8]:
                p = ctx.prompts.get(pid) or {}
                tasks.append(row(p.get("text") or ("(no prompt)" if not pid else "(prompt)"), a))
            title = meta.get("title") or (os.path.basename(meta.get("cwd", "")) if meta.get("cwd") else sid[:8])
            threads.append(row(title, s["all"], f"{host} · {project}", {
                "id": sid, "agents": len(s["agents"]), "kids": kids, "tasks": tasks,
            }))

        tasks = []
        for (sid, pid), a in sorted(sc.tasks.items(), key=lambda kv: -kv[1].usd)[:30]:
            p = ctx.prompts.get(pid) or {}
            meta = ctx.sessions.get(sid) or {}
            n_agents = sum(1 for x in ctx.agents.values() if pid and x.get("prompt") == pid)
            title = meta.get("title") or sid[:8]
            sub = title + (f" · {n_agents} subagent{'s' if n_agents != 1 else ''}" if n_agents else "")
            ms = (a.last - p["at"]) if p.get("at") else None
            r = row(p.get("text") or ("(no prompt)" if not pid else "(prompt)"), a, sub)
            if ms is not None and ms > 0:
                r["ms"] = ms
            tasks.append(r)

        dims = {}
        for name, d in sc.dims.items():
            dims[name] = [row(k, a) for k, a in sorted(d.items(), key=lambda kv: -kv[1].usd)[:TOP] if a.reqs]

        cache = self.cache_story(sc)
        return {
            "usd": round(sc.total.usd, 4), "tok": sc.total.tok(), "reqs": sc.total.reqs,
            "sessions": len(sc.sessions), "threads": threads, "tasks": tasks, "dims": dims, "cache": cache,
        }

    def cache_story(self, sc: Scope) -> dict:
        """What the prompt cache did: reads are cheap, writes re-send context."""
        t = sc.total
        return {
            "read": t.cr, "write": t.cw, "fresh": t.i, "out": t.o,
            "rebuilds": sc.rebuilds, "rebuildUsd": round(sc.rebuild_usd, 2),
        }

    def agent_tree(self, present: dict, agents: dict) -> list:
        out = []
        shown = set()

        def walk(parent: str, depth: int) -> None:
            kids = sorted(
                (aid for aid in present if (agents.get(aid) or {}).get("parent", "") == parent and aid not in shown),
                key=lambda aid: -present[aid].usd,
            )
            for aid in kids:
                shown.add(aid)
                out.append((aid, min(depth, 4)))
                walk(aid, depth + 1)

        walk("", 1)
        for aid in present:
            if aid not in shown:
                out.append((aid, 1))
        return out

    # -------------------------------------------------------------- token ledger

    def file_tokens(self, e: Entry) -> dict:
        out: dict = {}
        M = e.doc["m"]
        for i, r in enumerate(e.doc["reqs"]):
            if not e.keep[i]:
                continue
            m = M[r[4]] if r[4] < len(M) else ""
            long = self.prices.is_long(m, r[5] + r[7] + r[8] + r[9])
            key = f"{m}|{1 if r[11] else 0}|{1 if long else 0}"
            t = out.setdefault(local_date(r[0]), {}).setdefault(key, [0] * 7)
            t[0] += 1
            for j, k in enumerate((5, 6, 7, 8, 9, 10)):
                t[j + 1] += r[k]
        return out

    def token_ledger(self, entries: list, now: int) -> dict:
        """Merge the loaded day files into local/tokens.json and export it, priced now."""
        doc = self.tokens_doc
        if doc is None:
            doc = load_json(self.tokens_path, None)
            if not isinstance(doc, dict) or doc.get("v") != 1:
                doc = {"v": 1, "days": {}}
            self.tokens_doc = doc
        fresh: dict = {}
        for e in entries:
            if e.tokens is None:
                e.tokens = self.file_tokens(e)
            for date, models in e.tokens.items():
                into = fresh.setdefault(date, {})
                for key, t in models.items():
                    acc = into.setdefault(key, [0] * 7)
                    for j in range(7):
                        acc[j] += t[j]
        firm = local_date(now - TOKENS_FIRM_DAYS * DAY)
        days = doc["days"]
        changed = False
        for date, models in fresh.items():
            old = days.get(date)
            calls = sum(t[0] for t in models.values())
            # a day half out of the loaded files (or rebuilt from fewer transcripts) never shrinks a kept one
            if old != models and (date >= firm or old is None or calls >= sum(t[0] for t in old.values())):
                days[date] = models
                changed = True
        drop = local_date(now - TOKENS_KEEP_DAYS * DAY)
        for date in [d for d in days if d < drop]:
            del days[date]
            changed = True
        if changed:
            write_json(self.tokens_path, doc)
        return self.export_tokens(days, now)

    def export_tokens(self, days: dict, now: int) -> dict:
        def tally():
            return {"calls": 0, "in": 0, "out": 0, "cacheRead": 0, "cacheWrite5m": 0, "cacheWrite1h": 0, "web": 0, "usd": 0.0}

        def add(into: dict, t: list, usd: float) -> None:
            for j, k in enumerate(("calls", "in", "out", "cacheRead", "cacheWrite5m", "cacheWrite1h", "web")):
                into[k] += t[j]
            into["usd"] += usd

        def done(t: dict) -> dict:
            return {**t, "usd": round(t["usd"], 2)}

        months: dict = {}
        shown_from = local_date(now - (TOKENS_DAYS_SHOWN - 1) * DAY)
        recent = []
        total = tally()
        for date in sorted(days):
            day = tally()
            month = months.setdefault(date[:7], {"t": tally(), "models": {}, "days": 0})
            month["days"] += 1
            for key, t in days[date].items():
                model, fast, long = (key.rsplit("|", 2) + ["0", "0"])[:3]
                usd = self.prices.usd(model, t[1], t[2], t[3], t[4], t[5], t[6], int(fast), now, prompt=(1 << 61) if long == "1" else 0)
                add(day, t, usd)
                add(month["t"], t, usd)
                add(total, t, usd)
                add(month["models"].setdefault(self.prices.label(model), tally()), t, usd)
            if date >= shown_from:
                recent.append({"date": date, **done(day)})
        out_months = []
        for key in sorted(months, reverse=True):
            m = months[key]
            label = dt.datetime.strptime(key + "-01", "%Y-%m-%d").strftime("%b %Y")
            models = sorted(m["models"].items(), key=lambda kv: -kv[1]["usd"])
            out_months.append({
                "month": key, "label": label, "activeDays": m["days"], **done(m["t"]),
                "models": [{"label": name, **done(t)} for name, t in models],
            })
        return {
            "since": min(days) if days else None,
            "keepDays": TOKENS_KEEP_DAYS,
            "total": done(total),
            "months": out_months,
            "days": list(reversed(recent)),
        }

    def history(self, daily: dict, now: int) -> list:
        out = []
        for back in range(HISTORY_DAYS - 1, -1, -1):
            d = dt.datetime.fromtimestamp(now / 1000) - dt.timedelta(days=back)
            key = d.strftime("%Y-%m-%d")
            day = daily.get(key, {"hosts": {}, "projects": {}})
            top = sorted(day["projects"].items(), key=lambda kv: -kv[1])[:3]
            out.append({
                "date": key,
                "label": f"{d:%a} {d.day}",
                "hosts": {h: round(u, 2) for h, u in sorted(day["hosts"].items())},
                "total": round(sum(day["hosts"].values()), 2),
                "top": [[p, round(u, 2)] for p, u in top],
            })
        return out

    def accuracy(self, windows: list, entries: list, now: int) -> dict:
        per_host: dict = {}
        for e in entries:
            per_host[e.host] = per_host.get(e.host, 0) + sum(1 for k in e.keep if k)
        return {"requestsByHost": per_host}

    def hosts(self, entries: list) -> list:
        out = []
        hosts_dir = os.path.join(self.root, "hosts")
        for host in sorted(os.listdir(hosts_dir)) if os.path.isdir(hosts_dir) else []:
            st = load_json(os.path.join(hosts_dir, host, "status.json"), {})
            mine = load_json(os.path.join(hosts_dir, self.host, "status.json"), {}).get("sync", {}).get(host)
            out.append({
                "host": host,
                "isSelf": host == self.host,
                "updatedAt": st.get("updatedAt", 0),
                "syncError": (mine or {}).get("error") if mine and not mine.get("ok") else None,
                "subscription": st.get("subscription", ""),
            })
        out.sort(key=lambda h: (not h["isSelf"], h["host"]))
        return out


class Ctx:
    def __init__(self, sessions, prompts, agents, r5, r7, now) -> None:
        self.sessions = sessions
        self.prompts = prompts
        self.agents = agents
        self.r5 = r5
        self.r7 = r7
        self.now = now


def solve(A: list, b: list) -> list:
    """Gaussian elimination with partial pivoting (no numpy on stock macOS Python)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for j in range(c, n + 1):
                M[r][j] -= f * M[c][j]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][j] * x[j] for j in range(r + 1, n))) / M[r][r]
    return x


def merge(into: Agg, a: Agg) -> None:
    into.p5 += a.p5
    into.p7 += a.p7
    into.usd += a.usd
    into.i += a.i
    into.o += a.o
    into.cr += a.cr
    into.cw += a.cw
    into.reqs += a.reqs
    if a.first and (not into.first or a.first < into.first):
        into.first = a.first
    if a.last > into.last:
        into.last = a.last
