"""Cleans the session archive down to what helps spot where tokens go.

The session-archive feature writes every row a conversation stores to
%USERPROFILE%\\claude-session-archive\\<session id>\\ (meta.json and
rows-NNNN.jsonl). Most of those bytes are raw tool output, whole file bodies in
Write calls and system reminders. This keeps the shape of the work and drops
the bulk:

  kept   prompts and slash commands in full, Claude's replies (cut at 2,000
         characters), every tool call as a one-line summary (command, file,
         pattern), each result's size and whether it failed, questions asked
         and their answers, compactions, subagents, and the "tokens left"
         readings, so the tokens a stretch of work cost can be read off
  counted thinking, hook context and system reminders: their size per kind,
         not their text
  dropped tool output, file bodies, empty bookkeeping rows

Tokens and cost come from Claude Code's own transcripts
(~/.claude/projects/<project>/<session id>.jsonl and its subagents' files),
which record every API call's usage. Each call is charged to the prompt it ran
under and priced at today's API list prices (prices.json, shared with the usage
meter) for the latest model of its family: Opus 5.5, Sonnet 5.5, Haiku 5.5 or
Fable 5.1. Each session and request also shows what the same tokens would have
cost on each of those four.

Writes to %USERPROFILE%\\claude-session-archive-clean\\:
  sessions\\<session id>.jsonl   one line per event, in order; each prompt
                                carries the tokens and cost of its request
  index.jsonl                   one line per session: folder, times, tokens
                                and cost, its requests, prompt and tool counts,
                                result sizes by tool, files read again and
                                commands run again
  report.md                     totals across every session, biggest first
  requests.md                   every session and every request in it, with
                                tokens and cost, newest first
  report-<folder>.md and        with --folder: the same for one project only
  requests-<folder>.md

Sessions already cleaned and unchanged since are skipped. The raw archive is
left alone unless --delete-raw is given, which removes the rows files (never
meta.json) of sessions cleaned and idle for --older-than days (default 14).

  python tools\\clean-archive.py [--archive DIR] [--out DIR] [--transcripts DIR]
                                 [--all] [--folder DIR] [--delete-raw]
                                 [--older-than DAYS]
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import os
import re
import sys
import time

SAY_LIMIT = 2_000      # Claude's own words: enough to see what it said
ARG_LIMIT = 400        # a command, a pattern, a prompt to a subagent
ERROR_LIMIT = 400      # the head of a failed tool result
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SHELLS = {"Bash", "PowerShell"}
SCHEMA = 3             # index entries from an older version are cleaned again
PRICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prices.json")
# Every call is priced as the latest model of its family; anything else as Opus.
LATEST = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
          "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}


def family(model: str) -> str:
    m = (model or "").lower()
    for fam, keys in (("fable", ("fable", "mythos")), ("sonnet", ("sonnet",)), ("haiku", ("haiku",))):
        if any(k in m for k in keys):
            return fam
    return "opus"


class Prices:
    """Today's API list prices from prices.json: per-model rates, dated changes
    already in force, Haiku's long-prompt tier, cache writes and fast mode."""

    def __init__(self, path: str = PRICES_PATH):
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
        self.rows = {r[0]: r for r in p["models"]}
        self.cw5, self.cw1 = p.get("cacheWrite5m", 1.25), p.get("cacheWrite1h", 2.0)
        self.fast, self.tiers, self.web = p.get("fast", {}), p.get("tiers", {}), p.get("webSearchUsd", 0.01)
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        self.changes = sorted((c for c in p.get("changes", []) if c["from"] <= today), key=lambda c: c["from"])

    def label(self, model: str) -> str:
        return self.rows[model][1]

    def rates(self, model: str, prompt: int) -> tuple[float, float, float]:
        pin, pout, pcr = self.rows[model][2:5]
        for c in self.changes:
            if c["model"] == model:
                pin, pout, pcr = c.get("in", pin), c.get("out", pout), c.get("cr", pcr)
        tier = self.tiers.get(model)
        if tier and prompt > tier.get("over", 1 << 62):
            pin, pout, pcr = tier.get("in", pin), tier.get("out", pout), tier.get("cr", pcr)
        return pin, pout, pcr

    def usd(self, model: str, call: dict) -> float:
        i, o, cr, cw5, cw1 = (call[k] for k in ("i", "o", "cr", "cw5", "cw1"))
        pin, pout, pcr = self.rates(model, i + cr + cw5 + cw1)
        usd = (i * pin + o * pout + cr * pcr + (cw5 * self.cw5 + cw1 * self.cw1) * pin) / 1e6
        if call["fast"]:
            usd *= self.fast.get(model, 1)  # only models with a fast mode charge for it
        return usd + call["web"] * self.web


def transcript_files(root: str) -> dict[str, list[str]]:
    """Each session's transcript and its subagents' transcripts, by session id."""
    found: dict[str, list[str]] = collections.defaultdict(list)
    if not os.path.isdir(root):
        return found
    for project in os.scandir(root):
        if not project.is_dir():
            continue
        for e in os.scandir(project.path):
            if e.is_file() and e.name.endswith(".jsonl") and SESSION_RE.match(e.name[:-6]):
                found[e.name[:-6]].append(e.path)
            elif e.is_dir() and SESSION_RE.match(e.name):
                sub = os.path.join(e.path, "subagents")
                if os.path.isdir(sub):
                    found[e.name] += [os.path.join(sub, n) for n in os.listdir(sub) if n.endswith(".jsonl")]
    return found


def read_usage(paths: list[str], saved: str | None = None) -> list[dict]:
    """Every API call in these transcripts once (a streamed reply is stored a
    few times under the same message id), oldest first. Claude Code deletes old
    transcripts, so the calls are also kept in `saved` and read back from it:
    a session's tokens, cost and effort outlive its transcript."""
    calls: dict[str, dict] = {}
    if saved and os.path.isfile(saved):
        with open(saved, encoding="utf-8") as f:
            for line in f:
                try:
                    c = json.loads(line)
                    calls[c["id"]] = c
                except (ValueError, KeyError):
                    continue
    known = len(calls)
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message")
                if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                model = str(m.get("model") or "")
                if model.startswith("<"):
                    continue  # <synthetic>: written by Claude Code, not the API
                u, cc = m["usage"], m["usage"].get("cache_creation") or {}
                cw5, cw1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
                if cw5 + cw1 == 0:
                    cw5 = int(u.get("cache_creation_input_tokens") or 0)
                key = m.get("id") or d.get("uuid") or str(len(calls))
                calls[key] = {
                    "id": key, "at": d.get("timestamp") or "", "model": model, "agent": bool(d.get("isSidechain")),
                    # The effort this turn ran at (it can change mid-session), else the session's.
                    "effort": str(d.get("perTurnEffort") or d.get("effort") or ""),
                    "i": int(u.get("input_tokens") or 0), "o": int(u.get("output_tokens") or 0),
                    "cr": int(u.get("cache_read_input_tokens") or 0), "cw5": cw5, "cw1": cw1,
                    "web": int((u.get("server_tool_use") or {}).get("web_search_requests") or 0),
                    "fast": u.get("speed") == "fast",
                }
    ordered = sorted(calls.values(), key=lambda c: c["at"])
    if saved and (len(calls) != known or paths):
        write_atomic(saved, "".join(json.dumps(c) + "\n" for c in ordered))
    return ordered


def effort_mix(calls: list[dict]) -> str | None:
    """The effort levels these calls ran at, most used first: `medium`, `medium/xhigh`."""
    levels = collections.Counter(c.get("effort") or "?" for c in calls)
    return "/".join(k for k, _ in levels.most_common()) or None


def run_minutes(calls: list[dict], idle: float = 5) -> tuple[float | None, float | None]:
    """(first to last call, the same less any gap over `idle` minutes), in minutes."""
    times = []
    for c in calls:
        try:
            times.append(dt.datetime.fromisoformat(c["at"].replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    if len(times) < 2:
        return (0.0, 0.0) if times else (None, None)
    times.sort()
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    return round(sum(gaps), 1), round(sum(g for g in gaps if g <= idle), 1)


def call_tokens(c: dict) -> int:
    return c["i"] + c["o"] + c["cr"] + c["cw5"] + c["cw1"]


def tally(calls: list[dict], prices: Prices) -> dict:
    """Tokens and cost of some calls: as run, and the same tokens on each latest model."""
    out = {"calls": len(calls), "tokens": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "usd": 0.0,
           "usdOn": {prices.label(m): 0.0 for m in LATEST.values()}}
    for c in calls:
        out["tokens"] += call_tokens(c)
        out["input"] += c["i"]
        out["output"] += c["o"]
        out["cacheRead"] += c["cr"]
        out["cacheWrite"] += c["cw5"] + c["cw1"]
        out["usd"] += prices.usd(LATEST[family(c["model"])], c)
        for m in LATEST.values():
            out["usdOn"][prices.label(m)] += prices.usd(m, c)
    out["usd"] = round(out["usd"], 4)
    out["usdOn"] = {k: round(v, 4) for k, v in out["usdOn"].items()}
    return out


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    for unit, scale, places in (("B", 1e9, 2), ("M", 1e6, 2), ("k", 1e3, 1)):
        if n >= scale:
            return f"{n / scale:.{places}f}{unit}"
    return str(n)


def fmt_minutes(m: float | None) -> str:
    if m is None:
        return "?"
    return f"{int(m // 60)}h {int(m % 60):02d}m" if m >= 60 else f"{m:.0f}m" if m >= 1 else "<1m"


def effort_label(u: dict) -> str:
    """`medium`, or `low, agents medium` when subagents ran at another level."""
    own, agents = u.get("effort"), u.get("agentEffort")
    if agents and agents != own:
        return f"{own or '?'}, agents {agents}" if own else f"agents {agents}"
    return own or agents or "?"


def fmt_usd(x: float | None) -> str:
    return f"${x or 0:,.2f}"


def local_time(at: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return at or "?"


def cut(text: object, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}…[+{len(text) - limit}]"


def texts(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text", "")))
        elif isinstance(b, dict) and b.get("type") in ("image", "document"):
            parts.append(f"[{b['type']}]")
    return "\n".join(parts)


def size(value: object) -> int:
    return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))


def summarise_call(name: str, args: dict) -> dict:
    """A tool call reduced to what tells it apart from the next one."""
    a = args if isinstance(args, dict) else {}
    if name in SHELLS:
        out = {"cmd": cut(a.get("command", ""), ARG_LIMIT)}
        if a.get("description"):
            out["why"] = cut(a["description"], 120)
        return out
    if name == "Read":
        return {k: a[k] for k in ("file_path", "offset", "limit", "pages") if k in a}
    if name == "Write":
        return {"file_path": a.get("file_path"), "chars": size(a.get("content", ""))}
    if name == "Edit":
        return {"file_path": a.get("file_path"), "old": size(a.get("old_string", "")),
                "new": size(a.get("new_string", "")), **({"all": True} if a.get("replace_all") else {})}
    if name in ("Grep", "Glob"):
        return {k: cut(a[k], ARG_LIMIT) for k in ("pattern", "path", "glob", "type", "output_mode") if k in a}
    if name == "Agent":
        return {"type": a.get("subagent_type"), "model": a.get("model"),
                "what": a.get("description"), "prompt": cut(a.get("prompt", ""), ARG_LIMIT)}
    if name == "AskUserQuestion":
        return {"questions": [{"q": q.get("question"), "options": [o.get("label") for o in q.get("options", [])]}
                              for q in a.get("questions", []) if isinstance(q, dict)]}
    if name == "Skill":
        return {"skill": a.get("skill"), "args": cut(a.get("args", ""), ARG_LIMIT)}
    # Anything else: keep the arguments, each string cut short.
    return {k: (cut(v, 200) if isinstance(v, str) else v if size(v) <= 200 else f"<{size(v)} chars>")
            for k, v in a.items()}


def target(name: str, args: dict) -> str | None:
    """The thing a call works on, to spot the same file or command again."""
    if name in ("Read", "Write", "Edit"):
        return args.get("file_path")
    if name in SHELLS:
        return args.get("cmd")
    return None


class Session:
    def __init__(self, sid: str, meta: dict):
        self.sid, self.meta = sid, meta
        self.events: list[dict] = []
        self.calls: dict[str, tuple[str, dict]] = {}
        self.tool_calls = collections.Counter()
        self.result_chars = collections.Counter()
        self.input_chars = collections.Counter()
        self.errors = collections.Counter()
        self.overhead = collections.Counter()   # chars of reminders, hook context, thinking
        self.targets = collections.Counter()
        self.prompts = self.replies = self.compactions = 0
        self.agents: set[str] = set()
        self.first = self.last = None
        self.usage: dict | None = None      # the session's tokens and cost, once priced
        self.requests: list[dict] = []      # each prompt's tokens and cost

    def emit(self, row: dict, kind: str, **fields):
        event = {"at": row.get("at"), "k": kind}
        if row.get("agentId"):
            event["agent"] = row["agentId"]
        event.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
        self.events.append(event)

    def add(self, row: dict):
        at, door, name = row.get("at"), row.get("door"), row.get("name")
        content = row.get("content")
        self.first = self.first or at
        self.last = at or self.last
        if row.get("agentId"):
            self.agents.add(row["agentId"])

        if door == "prompt":
            if not row.get("agentId"):
                self.prompts += 1
            self.emit(row, "prompt", text=texts(content))
        elif door == "command":
            self.emit(row, "command", text=cut(texts(content), ARG_LIMIT * 5))
        elif door == "compaction":
            if row.get("type") == "system":
                self.compactions += 1
                self.emit(row, "compaction")
            else:
                self.overhead["compaction summary"] += size(content)
        elif door == "response":
            for b in content if isinstance(content, list) else []:
                t = b.get("type")
                if t == "text":
                    self.replies += 1
                    self.emit(row, "say", text=cut(b.get("text", ""), SAY_LIMIT))
                elif t in ("thinking", "redacted_thinking"):
                    self.overhead["thinking"] += size(b.get("thinking", ""))
                elif t == "tool_use":
                    tool, raw = b.get("name", "?"), b.get("input", {})
                    args = summarise_call(tool, raw)
                    self.calls[b.get("id")] = (tool, args)
                    self.tool_calls[tool] += 1
                    self.input_chars[tool] += size(raw)
                    if (tgt := target(tool, args)):
                        self.targets[(tool, tgt)] += 1
                    self.emit(row, "call", tool=tool, args=args)
        elif door == "tool-result":
            for b in content if isinstance(content, list) else []:
                if b.get("type") != "tool_result":
                    continue
                tool, args = self.calls.get(b.get("tool_use_id"), ("?", {}))
                n = size(texts(b.get("content")) or b.get("content") or "")
                self.result_chars[tool] += n
                failed = bool(b.get("is_error"))
                if failed:
                    self.errors[tool] += 1
                answer = texts(b.get("content")) if tool == "AskUserQuestion" else None
                head = cut(texts(b.get("content")), ERROR_LIMIT) if failed else None
                self.emit(row, "result", tool=tool, chars=n, error=failed or None, answer=answer, head=head)
        elif name == "total_tokens_reminder":
            pass  # the transcripts' usage is exact; this countdown is not needed
        elif door in ("delivery", "note", "tool-message"):
            text = texts(content)
            self.overhead[door] += len(text)
            self.emit(row, door, text=cut(text, ARG_LIMIT))
        else:
            # hook context, reminders, listings, notices: counted, not kept.
            n = len(texts(content))
            if n:
                self.overhead[name or door or "other"] += n

    def price(self, calls: list[dict], prices: Prices):
        """Charges each API call to the prompt it ran under (the last one before
        it, subagents' calls included) and marks each prompt with its total."""
        if not calls:
            return
        self.usage = tally(calls, prices)
        by_model = collections.defaultdict(list)
        for c in calls:
            by_model[prices.label(LATEST[family(c["model"])])].append(c)
        self.usage["byModel"] = {k: {"tokens": t["tokens"], "usd": t["usd"]}
                                 for k, v in by_model.items() for t in [tally(v, prices)]}
        by_model_effort = collections.defaultdict(list)
        for c in calls:
            by_model_effort[f"{prices.label(LATEST[family(c['model'])])} · {c.get('effort') or '?'}"].append(c)
        self.usage["byModelEffort"] = {k: {"calls": t["calls"], "tokens": t["tokens"], "usd": t["usd"]}
                                       for k, v in by_model_effort.items() for t in [tally(v, prices)]}
        main, agents = [c for c in calls if not c["agent"]], [c for c in calls if c["agent"]]
        self.usage["effort"] = effort_mix(main)
        self.usage["agentEffort"] = effort_mix(agents) if agents else None
        self.usage["runMinutes"], self.usage["workingMinutes"] = run_minutes(calls)
        prompts = [e for e in self.events if e["k"] == "prompt" and not e.get("agent")]
        starts = [e.get("at") or "" for e in prompts]
        buckets: list[list[dict]] = [[] for _ in prompts] or [[]]
        for c in calls:
            buckets[max(bisect.bisect_right(starts, c["at"]) - 1, 0)].append(c)
        for n, bucket in enumerate(buckets):
            t = tally(bucket, prices)
            e = prompts[n] if prompts else None
            own, helpers = [c for c in bucket if not c["agent"]], [c for c in bucket if c["agent"]]
            effort = effort_mix(own) if own else None
            self.requests.append({
                "at": e.get("at") if e else (bucket[0]["at"] if bucket else None),
                "prompt": cut(e["text"], 200) if e else "(no prompt)",
                "calls": t["calls"], "tokens": t["tokens"], "output": t["output"],
                "usd": t["usd"], "usdOn": t["usdOn"],
                "effort": effort, "agentEffort": effort_mix(helpers) if helpers else None,
                "minutes": run_minutes(bucket)[0],
            })
            if e is not None:
                e.update(tokens=t["tokens"], usd=t["usd"], apiCalls=t["calls"], effort=effort)

    def summary(self) -> dict:
        def minutes(a, b):
            try:
                return round((dt.datetime.fromisoformat(b.replace("Z", "+00:00")) -
                              dt.datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 60, 1)
            except Exception:
                return None
        repeats = [{"tool": t, "target": cut(x, 200), "times": n}
                   for (t, x), n in self.targets.most_common() if n > 1][:25]
        return {
            "schema": SCHEMA,
            "sessionId": self.sid,
            "cwd": self.meta.get("cwd"),
            "model": self.meta.get("model"),
            "claudeCode": self.meta.get("claudeCode"),
            "startedAt": self.meta.get("startedAt") or self.first,
            "lastAt": self.last,
            "minutes": minutes(self.meta.get("startedAt") or self.first or "", self.last or ""),
            "prompts": self.prompts,
            "firstPrompt": next((cut(e["text"], 300) for e in self.events if e["k"] == "prompt"), None),
            "replies": self.replies,
            "subagents": len(self.agents),
            "compactions": self.compactions,
            "usage": self.usage,
            "requests": self.requests,
            "toolCalls": dict(self.tool_calls.most_common()),
            "toolErrors": dict(self.errors.most_common()),
            "resultChars": dict(self.result_chars.most_common()),
            "inputChars": dict(self.input_chars.most_common()),
            "overheadChars": dict(self.overhead.most_common()),
            "repeated": repeats,
        }


def read_session(folder: str, sid: str) -> Session:
    meta = {}
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        pass
    s = Session(sid, meta)
    for part in sorted(p for p in os.listdir(folder) if p.startswith("rows-") and p.endswith(".jsonl")):
        with open(os.path.join(folder, part), encoding="utf-8") as f:
            for line in f:
                try:
                    s.add(json.loads(line))
                except ValueError:
                    continue  # a line cut short by a write in progress
    return s


def newest_mtime(folder: str) -> float:
    return max((os.path.getmtime(os.path.join(folder, p)) for p in os.listdir(folder)), default=0)


def write_atomic(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def in_folder(cwd: str | None, folder: str) -> bool:
    """Whether a session ran in folder or below it, however its path is spelt."""
    norm = lambda path: os.path.normcase(os.path.normpath(path)).rstrip("\\/")
    here, want = norm(cwd or ""), norm(folder)
    return here == want or here.startswith(want + os.sep)


def cell(text: object, n: int = 90) -> str:
    return str(text or "").replace("\n", " ").replace("|", "/")[:n]


def report(index: list[dict], title: str = "Session archive report") -> str:
    total = collections.Counter
    calls, results, inputs, overhead, errors, folders = total(), total(), total(), total(), total(), total()
    tok_folder, usd_folder, tok_model, usd_model, usd_on, targets = total(), total(), total(), total(), total(), total()
    tok_me, usd_me, calls_me = total(), total(), total()
    requests = []
    for s in index:
        calls.update(s["toolCalls"]); results.update(s["resultChars"]); inputs.update(s["inputChars"])
        overhead.update(s["overheadChars"]); errors.update(s["toolErrors"])
        where = s.get("cwd") or "?"
        folders[where] += 1
        u = s.get("usage") or {}
        tok_folder[where] += u.get("tokens", 0)
        usd_folder[where] += u.get("usd", 0)
        for m, t in (u.get("byModel") or {}).items():
            tok_model[m] += t["tokens"]
            usd_model[m] += t["usd"]
        for m, t in (u.get("byModelEffort") or {}).items():
            tok_me[m] += t["tokens"]
            usd_me[m] += t["usd"]
            calls_me[m] += t["calls"]
        usd_on.update(u.get("usdOn") or {})
        requests += [{**r, "cwd": where} for r in s.get("requests") or []]
        for r in s["repeated"]:
            targets[(r["tool"], r["target"])] += r["times"]
    models = [Prices().label(m) for m in LATEST.values()]

    def table(title, counter, unit="", n=15):
        rows = [f"## {title}", "", "| | |", "|---|---:|"]
        rows += [f"| `{k}` | {v:,}{unit} |" for k, v in counter.most_common(n)]
        return "\n".join(rows) + "\n"

    lines = [
        f"# {title}", "",
        f"{len(index)} sessions, {len(requests):,} requests, {fmt_tokens(sum(tok_folder.values()))} tokens, "
        f"{fmt_usd(sum(usd_folder.values()))} at API prices. "
        f"{sum(calls.values()):,} tool calls, {sum(s['compactions'] for s in index)} compactions.", "",
        "API prices are today's list prices, each call priced as the latest model of its family "
        f"({', '.join(models)}). Tokens count input, output, cache reads and cache writes. "
        "The per-model columns are the same tokens priced on that model. "
        "Sizes in the tool and reminder tables are characters (about 4 to a token).", "",
        "## Cost by model", "",
        "| model | tokens | API cost |", "|---|---:|---:|",
    ]
    lines += [f"| {m} | {fmt_tokens(tok_model[m])} | {fmt_usd(v)} |" for m, v in usd_model.most_common()]
    lines += ["", "## Cost by model and effort", "",
              "| model · effort | API calls | tokens | API cost | cost per call |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {m} | {calls_me[m]:,} | {fmt_tokens(tok_me[m])} | {fmt_usd(v)} | {fmt_usd(v / max(calls_me[m], 1))} |"
              for m, v in usd_me.most_common()]
    lines += ["", "If every call had run on one model:", "",
              "| model | API cost |", "|---|---:|"]
    lines += [f"| {m} | {fmt_usd(usd_on[m])} |" for m in models]
    lines += ["", "## Sessions that cost the most", "",
              "| started | folder | run time | effort | requests | tokens | API cost | "
              + " | ".join(f"on {m}" for m in models) + " | first prompt |",
              "|---|---|---:|---|---:|---:|---:|" + "---:|" * len(models) + "---|"]
    for s in sorted(index, key=lambda s: (s.get("usage") or {}).get("usd", 0), reverse=True)[:20]:
        u = s.get("usage") or {}
        lines.append(f"| {local_time(s.get('startedAt'))} | {cell(os.path.basename(s.get('cwd') or '?'), 30)} "
                     f"| {fmt_minutes(u.get('runMinutes'))} | {effort_label(u)} | {len(s.get('requests') or [])} | {fmt_tokens(u.get('tokens'))} | {fmt_usd(u.get('usd'))} | "
                     + " | ".join(fmt_usd((u.get("usdOn") or {}).get(m)) for m in models)
                     + f" | {cell(s.get('firstPrompt'))} |")
    lines += ["", "## Requests that cost the most", "",
              "| when | folder | run time | effort | API calls | tokens | output | API cost | prompt |",
              "|---|---|---:|---|---:|---:|---:|---:|---|"]
    for r in sorted(requests, key=lambda r: r["usd"], reverse=True)[:25]:
        lines.append(f"| {local_time(r['at'])} | {cell(os.path.basename(r['cwd']), 30)} "
                     f"| {fmt_minutes(r.get('minutes'))} | {effort_label(r)} | {r['calls']} "
                     f"| {fmt_tokens(r['tokens'])} | {fmt_tokens(r['output'])} | {fmt_usd(r['usd'])} "
                     f"| {cell(r['prompt'])} |")
    lines += ["", "## Cost by folder", "", "| folder | sessions | tokens | API cost |", "|---|---:|---:|---:|"]
    lines += [f"| `{k}` | {folders[k]} | {fmt_tokens(tok_folder[k])} | {fmt_usd(usd_folder[k])} |"
              for k, _ in folders.most_common(15)]
    lines += ["", table("Tool output fed back to Claude, by tool", results, " ch"),
              table("Tool input Claude wrote, by tool", inputs, " ch"),
              table("Reminders, hook context and thinking", overhead, " ch"),
              table("Tool calls", calls),
              table("Failed tool calls", errors),
              "## Same file or command again within a session", "",
              "| times | tool | target |", "|---:|---|---|"]
    lines += [f"| {n} | {t} | `{cell(x, 120)}` |" for (t, x), n in targets.most_common(30)]
    return "\n".join(lines) + "\n"


def requests_md(index: list[dict], title: str = "Every session and request") -> str:
    """Every session, newest first, with each of its requests' tokens and cost."""
    lines = [f"# {title}", "",
             "Each request is one prompt and all the API calls made until the next one, "
             "subagents' included, priced at today's API list prices.", ""]
    for s in sorted(index, key=lambda s: s.get("startedAt") or "", reverse=True):
        u = s.get("usage") or {}
        lines += [f"## {local_time(s.get('startedAt'))} · {os.path.basename(s.get('cwd') or '?')} · "
                  f"{fmt_tokens(u.get('tokens'))} tokens · {fmt_usd(u.get('usd'))}", "",
                  f"`{s.get('cwd')}` · {s.get('model')} · effort {effort_label(u)} · "
                  f"ran {fmt_minutes(u.get('runMinutes'))} (working {fmt_minutes(u.get('workingMinutes'))}) · "
                  f"session `{s['sessionId']}`", ""]
        if not s.get("requests"):
            lines += ["No usage found: its transcript is gone.", ""]
            continue
        lines += ["| when | run time | effort | API calls | tokens | output | API cost | prompt |",
                  "|---|---:|---|---:|---:|---:|---:|---|"]
        lines += [f"| {local_time(r['at'])} | {fmt_minutes(r.get('minutes'))} | {effort_label(r)} "
                  f"| {r['calls']} | {fmt_tokens(r['tokens'])} | {fmt_tokens(r['output'])} "
                  f"| {fmt_usd(r['usd'])} | {cell(r['prompt'], 120)} |" for r in s["requests"]]
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    home = os.path.expanduser("~")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", default=os.path.join(home, "claude-session-archive"))
    p.add_argument("--out", default=os.path.join(home, "claude-session-archive-clean"))
    p.add_argument("--transcripts", default=os.path.join(home, ".claude", "projects"),
                   help="Claude Code's transcripts, for each call's tokens")
    p.add_argument("--all", action="store_true", help="clean every session again, changed or not")
    p.add_argument("--delete-raw", action="store_true",
                   help="after cleaning, delete the raw rows of sessions idle for --older-than days")
    p.add_argument("--older-than", type=float, default=14, metavar="DAYS")
    p.add_argument("--folder", metavar="DIR",
                   help="also write report-<folder>.md, counting only sessions run in DIR or below it")
    args = p.parse_args(argv)

    if not os.path.isdir(args.archive):
        print(f"No archive at {args.archive}", file=sys.stderr)
        return 1
    sessions_dir = os.path.join(args.out, "sessions")
    usage_dir = os.path.join(args.out, "usage")
    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(usage_dir, exist_ok=True)
    index_path = os.path.join(args.out, "index.jsonl")

    index: dict[str, dict] = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                    index[s["sessionId"]] = s
                except (ValueError, KeyError):
                    continue

    prices = Prices()
    transcripts = transcript_files(args.transcripts)
    cleaned = skipped = deleted = raw_bytes = clean_bytes = 0
    now = time.time()
    for sid in sorted(os.listdir(args.archive)):
        folder = os.path.join(args.archive, sid)
        if not (SESSION_RE.match(sid) and os.path.isdir(folder)):
            continue
        out = os.path.join(sessions_dir, f"{sid}.jsonl")
        changed = max([newest_mtime(folder)] + [os.path.getmtime(t) for t in transcripts.get(sid, [])])
        has_rows = any(p.startswith("rows-") for p in os.listdir(folder))
        stale = sid not in index or index[sid].get("schema") != SCHEMA
        if has_rows and (args.all or stale or not os.path.exists(out) or os.path.getmtime(out) < changed):
            s = read_session(folder, sid)
            s.price(read_usage(transcripts.get(sid, []), os.path.join(usage_dir, f"{sid}.jsonl")), prices)
            text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in s.events)
            write_atomic(out, text)
            index[sid] = s.summary()
            cleaned += 1
            raw_bytes += sum(os.path.getsize(os.path.join(folder, p)) for p in os.listdir(folder))
            clean_bytes += len(text.encode("utf-8"))
        else:
            skipped += 1

        idle_days = (now - changed) / 86400
        if args.delete_raw and has_rows and os.path.exists(out) and idle_days >= args.older_than \
                and os.path.getmtime(out) >= changed:
            for p in os.listdir(folder):
                if p.startswith("rows-"):
                    os.remove(os.path.join(folder, p))
            deleted += 1

    ordered = sorted(index.values(), key=lambda s: s.get("startedAt") or "")
    write_atomic(index_path, "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in ordered))
    write_atomic(os.path.join(args.out, "report.md"), report(ordered))
    write_atomic(os.path.join(args.out, "requests.md"), requests_md(ordered))
    if args.folder:
        mine = [s for s in ordered if in_folder(s.get("cwd"), args.folder)]
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.normpath(args.folder))) or "folder"
        where = os.path.normpath(args.folder)
        path = os.path.join(args.out, f"report-{name}.md")
        write_atomic(path, report(mine, f"Session archive report: {where}"))
        write_atomic(os.path.join(args.out, f"requests-{name}.md"),
                     requests_md(mine, f"Every session and request: {where}"))
        print(f"{len(mine)} sessions in {args.folder}: {path}")

    print(f"Cleaned {cleaned} sessions ({raw_bytes / 1e6:.1f} MB raw -> {clean_bytes / 1e6:.1f} MB), "
          f"{skipped} unchanged. Output: {args.out}")
    if args.delete_raw:
        print(f"Deleted the raw rows of {deleted} sessions idle for {args.older_than:g}+ days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
