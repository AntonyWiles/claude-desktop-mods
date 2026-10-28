"""A short usage review from the session archive: tokens, API cost and where they went.

Brings the cleaned archive up to date (tools/clean-archive.py), then prints at
most a screenful:

  python review.py                  this session (CLAUDE_CODE_SESSION_ID), request by request
  python review.py --session ID     another session
  python review.py --days 7         this folder's sessions over the last 7 days
  python review.py --days 7 --everywhere   every folder
  python review.py --folder DIR     another folder (with --days, default 7)

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLEANER = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "clean-archive.py"))
OUT = os.path.join(os.path.expanduser("~"), "claude-session-archive-clean")


def tokens(n) -> str:
    n = n or 0
    for unit, scale, places in (("B", 1e9, 2), ("M", 1e6, 2), ("k", 1e3, 1)):
        if n >= scale:
            return f"{n / scale:.{places}f}{unit}"
    return str(n)


def usd(x) -> str:
    return f"${x or 0:,.2f}"


def when(at) -> str:
    try:
        return dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except Exception:
        return "?"


def one_line(text, n=70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def norm(path) -> str:
    return os.path.normcase(os.path.normpath(path or "")).rstrip("\\/")


def top(counter: collections.Counter, n: int, fmt=str) -> str:
    return ", ".join(f"{k} {fmt(v)}" for k, v in counter.most_common(n)) or "none"


def chars(n) -> str:
    return f"{tokens(n)} ch"


def session_view(s: dict) -> list[str]:
    u = s.get("usage") or {}
    rows = [f"Session {s['sessionId'][:8]} in {s.get('cwd')}, started {when(s.get('startedAt'))}, "
            f"{s.get('minutes')} min, {s.get('model')}",
            f"{tokens(u.get('tokens'))} tokens ({tokens(u.get('cacheRead'))} cache reads, "
            f"{tokens(u.get('output'))} output) in {u.get('calls', 0)} API calls: {usd(u.get('usd'))} at API prices"]
    if u.get("usdOn"):
        rows.append("Same tokens on " + ", ".join(f"{k} {usd(v)}" for k, v in u["usdOn"].items()))
    reqs = s.get("requests") or []
    if reqs:
        rows += ["", "Requests (newest last):"]
        shown = reqs if len(reqs) <= 15 else reqs[:3] + [None] + reqs[-11:]
        for r in shown:
            if r is None:
                rows.append(f"  ... {len(reqs) - 14} more")
                continue
            rows.append(f"  {when(r['at'])}  {tokens(r['tokens']):>8}  {usd(r['usd']):>8}  {r['calls']:>4} calls  "
                        f"{one_line(r['prompt'], 60)}")
    rows += ["", "Tool output read back: " + top(collections.Counter(s.get("resultChars")), 4, chars),
             "Tool calls: " + top(collections.Counter(s.get("toolCalls")), 5)]
    if s.get("repeated"):
        rows.append("Same target again: " + "; ".join(
            f"{r['tool']} {one_line(os.path.basename(r['target']) if r['tool'] in ('Read', 'Edit', 'Write') else r['target'], 40)} x{r['times']}"
            for r in s["repeated"][:4]))
    return rows


def period_view(sessions: list[dict], label: str) -> list[str]:
    used = [s for s in sessions if s.get("usage")]
    total = sum(s["usage"]["usd"] for s in used)
    toks = sum(s["usage"]["tokens"] for s in used)
    by_model, on, results, overhead, repeats = (collections.Counter() for _ in range(5))
    reqs = []
    for s in used:
        for m, t in (s["usage"].get("byModel") or {}).items():
            by_model[m] += t["usd"]
        on.update(s["usage"].get("usdOn") or {})
        results.update(s.get("resultChars") or {})
        overhead.update(s.get("overheadChars") or {})
        for r in s.get("repeated") or []:
            repeats[f"{r['tool']} {one_line(os.path.basename(r['target']) if r['tool'] in ('Read', 'Edit', 'Write') else r['target'], 40)}"] += r["times"]
        reqs += [{**r, "cwd": s.get("cwd")} for r in s.get("requests") or []]
    rows = [f"{label}: {len(sessions)} sessions, {len(reqs)} requests, {tokens(toks)} tokens, {usd(total)} at API prices",
            "By model: " + top(by_model, 4, usd),
            "Same tokens on " + ", ".join(f"{k} {usd(v)}" for k, v in on.items()) if on else "",
            "", "Costliest sessions:"]
    for s in sorted(used, key=lambda s: s["usage"]["usd"], reverse=True)[:5]:
        rows.append(f"  {when(s.get('startedAt'))}  {usd(s['usage']['usd']):>8}  {tokens(s['usage']['tokens']):>8}  "
                    f"{len(s.get('requests') or []):>3} req  {one_line(s.get('firstPrompt'), 55)}")
    rows.append("Costliest requests:")
    for r in sorted(reqs, key=lambda r: r["usd"], reverse=True)[:5]:
        rows.append(f"  {when(r['at'])}  {usd(r['usd']):>8}  {r['calls']:>4} calls  {one_line(r['prompt'], 60)}")
    rows += ["", "Tool output read back: " + top(results, 4, chars),
             "Reminders and listings: " + top(overhead, 3, chars),
             "Same target again: " + top(repeats, 4, lambda v: f"x{v}")]
    return [r for r in rows if r is not None]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--session", help="a session id (default: this session)")
    p.add_argument("--days", type=float, help="sessions started in the last N days")
    p.add_argument("--folder", help="sessions run in this folder or below it (default: here)")
    p.add_argument("--everywhere", action="store_true", help="every folder")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    done = subprocess.run([sys.executable, "-I", CLEANER], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"usage-review: the archive cleaner failed:\n{done.stdout}{done.stderr}".strip())
        return 1
    with open(os.path.join(OUT, "index.jsonl"), encoding="utf-8") as f:
        index = [json.loads(line) for line in f if line.strip()]

    if args.days is None and args.folder is None and not args.everywhere:
        sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
        s = next((s for s in index if s["sessionId"] == sid), None) if sid else None
        if s is None:
            print(f"usage-review: no archived session {sid or '(no CLAUDE_CODE_SESSION_ID)'}; "
                  "try --days 7. The session archive only covers sessions since the mod's archive was on.")
            return 1
        print("\n".join(session_view(s)))
        return 0

    days = args.days or 7
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    chosen = [s for s in index if (s.get("startedAt") or "") >= since[:19]]
    where = "everywhere"
    if not args.everywhere:
        folder = norm(args.folder or os.getcwd())
        chosen = [s for s in chosen if norm(s.get("cwd")) == folder or norm(s.get("cwd")).startswith(folder + os.sep)]
        where = os.path.basename(folder) or folder
    print("\n".join(period_view(chosen, f"Last {days:g} days, {where}")))
    print(f"\nFull detail: {os.path.join(OUT, 'report.md')} and requests.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
