---
name: usage-review
description: Show what this session, this project or a stretch of days cost in tokens and API dollars, request by request, and where the tokens went, from the desktop-mods session archive. Use when the user asks "how much did this cost", "where did my tokens go", "usage review", "what's been expensive", or wants to cut token use and needs the numbers first.
---

# Usage review

The desktop-mods session archive records every session. Its cleaner reads Claude Code's own
transcripts for each API call's exact usage and prices it at today's API list prices, as the latest
model of its family (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1). This skill brings that up to
date and prints a screenful.

## Run it

```
python "${CLAUDE_PLUGIN_ROOT}/skills/usage-review/review.py"
```

- no options: **this session**, request by request: tokens, cost and API calls per prompt, then
  where the output came from and what was touched again and again;
- `--days 7`: this folder's sessions over the last 7 days: totals, cost by model, what the same
  tokens would have cost on each model, the costliest sessions and requests, tool output, and the
  biggest reminders and listings;
- `--days 30 --everywhere`: every folder;
- `--folder C:\path\to\project`: another project (last 7 days unless `--days` says otherwise);
- `--session <id>`: another session.

Full detail is in `%USERPROFILE%\claude-session-archive-clean\`: `report.md` for totals, and
`requests.md` for every session and request.

## Tell the user

Give them the numbers in a few lines, then at most three concrete things that would have cost less,
each tied to a number in the output. What usually drives the cost:

- **Context re-read on every call.** Cache reads are almost all of the tokens: each API call
  re-reads the whole conversation. A request with hundreds of calls in a big context is where the
  money goes. Fewer, bigger steps, and a fresh session (the `fresh-start` skill) when the context is
  large and a task is done, cut it most.
- **Long tool output.** Big Bash or Read results stay in the context for every later call. Point
  at `check-changed` for checks and `file-outline` for large files.
- **The same file or command again and again.** Usually a large file edited in many small steps, or
  the same check re-run.
- **Model choice.** The "same tokens on" line shows the price of routine work on a cheaper model.

These are API-equivalent figures. On a Claude plan they show what the work would cost on the API,
not what the user pays. Sessions from before the archive was on, or whose transcripts were deleted,
aren't counted.
