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

Writes to %USERPROFILE%\\claude-session-archive-clean\\:
  sessions\\<session id>.jsonl   one line per event, in order
  index.jsonl                   one line per session: folder, times, prompt
                                and tool counts, result sizes by tool, tokens
                                used, files read again and commands run again
  report.md                     totals across every session, biggest first
  report-<folder>.md            with --folder: the same for one project only

Sessions already cleaned and unchanged since are skipped. The raw archive is
left alone unless --delete-raw is given, which removes the rows files (never
meta.json) of sessions cleaned and idle for --older-than days (default 14).

  python tools\\clean-archive.py [--archive DIR] [--out DIR] [--all]
                                 [--folder DIR] [--delete-raw] [--older-than DAYS]
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
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
TOKENS_RE = re.compile(r"<total_tokens>(\d+) tokens left</total_tokens>")
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SHELLS = {"Bash", "PowerShell"}


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
        self.tokens: list[int] = []
        self.first = self.last = None

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
            if (m := TOKENS_RE.search(texts(content))):
                left = int(m.group(1))
                if not row.get("agentId"):
                    self.tokens.append(left)
                self.emit(row, "tokens", left=left)
        elif door in ("delivery", "note", "tool-message"):
            text = texts(content)
            self.overhead[door] += len(text)
            self.emit(row, door, text=cut(text, ARG_LIMIT))
        else:
            # hook context, reminders, listings, notices: counted, not kept.
            n = len(texts(content))
            if n:
                self.overhead[name or door or "other"] += n

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
            "tokensUsed": (max(self.tokens) - min(self.tokens)) if len(self.tokens) > 1 else None,
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


def report(index: list[dict], title: str = "Session archive report") -> str:
    total = collections.Counter
    calls, results, inputs, overhead, errors, folders = total(), total(), total(), total(), total(), total()
    tokens_by_folder, targets = total(), total()
    for s in index:
        calls.update(s["toolCalls"]); results.update(s["resultChars"]); inputs.update(s["inputChars"])
        overhead.update(s["overheadChars"]); errors.update(s["toolErrors"])
        folders[s.get("cwd") or "?"] += 1
        tokens_by_folder[s.get("cwd") or "?"] += s.get("tokensUsed") or 0
        for r in s["repeated"]:
            targets[(r["tool"], r["target"])] += r["times"]

    def table(title, counter, unit="", n=15):
        rows = [f"## {title}", "", "| | |", "|---|---:|"]
        rows += [f"| `{k}` | {v:,}{unit} |" for k, v in counter.most_common(n)]
        return "\n".join(rows) + "\n"

    biggest = sorted(index, key=lambda s: s.get("tokensUsed") or 0, reverse=True)[:15]
    lines = [
        f"# {title}", "",
        f"{len(index)} sessions, {sum(s['prompts'] for s in index):,} prompts, "
        f"{sum(calls.values()):,} tool calls, {sum(s['compactions'] for s in index)} compactions. "
        "Sizes are characters (about 4 to a token).", "",
        "## Sessions that used the most tokens", "",
        "| tokens | prompts | calls | folder | first prompt |", "|---:|---:|---:|---|---|",
    ]
    for s in biggest:
        first = (s.get("firstPrompt") or "").replace("\n", " ").replace("|", "/")[:90]
        lines.append(f"| {s.get('tokensUsed') or 0:,} | {s['prompts']} | {sum(s['toolCalls'].values())} "
                     f"| `{s.get('cwd')}` | {first} |")
    lines += ["", table("Tokens used by folder", tokens_by_folder),
              table("Tool output fed back to Claude, by tool", results, " ch"),
              table("Tool input Claude wrote, by tool", inputs, " ch"),
              table("Reminders, hook context and thinking", overhead, " ch"),
              table("Tool calls", calls),
              table("Failed tool calls", errors),
              table("Sessions by folder", folders),
              "## Same file or command again within a session", "",
              "| times | tool | target |", "|---:|---|---|"]
    lines += [f"| {n} | {t} | `{x.replace('|', '/').replace(chr(10), ' ')[:120]}` |"
              for (t, x), n in targets.most_common(30)]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    home = os.path.expanduser("~")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", default=os.path.join(home, "claude-session-archive"))
    p.add_argument("--out", default=os.path.join(home, "claude-session-archive-clean"))
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
    os.makedirs(sessions_dir, exist_ok=True)
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

    cleaned = skipped = deleted = raw_bytes = clean_bytes = 0
    now = time.time()
    for sid in sorted(os.listdir(args.archive)):
        folder = os.path.join(args.archive, sid)
        if not (SESSION_RE.match(sid) and os.path.isdir(folder)):
            continue
        out = os.path.join(sessions_dir, f"{sid}.jsonl")
        changed = newest_mtime(folder)
        has_rows = any(p.startswith("rows-") for p in os.listdir(folder))
        if has_rows and (args.all or sid not in index or not os.path.exists(out) or os.path.getmtime(out) < changed):
            s = read_session(folder, sid)
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
    if args.folder:
        mine = [s for s in ordered if in_folder(s.get("cwd"), args.folder)]
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.normpath(args.folder))) or "folder"
        path = os.path.join(args.out, f"report-{name}.md")
        write_atomic(path, report(mine, f"Session archive report: {os.path.normpath(args.folder)}"))
        print(f"{len(mine)} sessions in {args.folder}: {path}")

    print(f"Cleaned {cleaned} sessions ({raw_bytes / 1e6:.1f} MB raw -> {clean_bytes / 1e6:.1f} MB), "
          f"{skipped} unchanged. Output: {args.out}")
    if args.delete_raw:
        print(f"Deleted the raw rows of {deleted} sessions idle for {args.older_than:g}+ days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
