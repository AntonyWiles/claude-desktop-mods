# Changelog

## Unreleased

- New: a token ledger. The usage collector keeps every day's tokens per model (calls, input,
  output, cache reads, 5-minute and 1-hour cache writes, web searches) in
  `~/.claude/usage-meter/local/tokens.json` for 400 days, long after Claude Code deletes the
  transcripts. The `/burn` pane's History tab shows them by month and by day, priced at today's
  API list prices, with each month's value against your plan's price and the top models. Re-run
  `deploy\install.ps1` to update the collector; the ledger starts from the 35 days the collector
  already holds.

## 1.0.2 - 2026-11-14

- Fix: Fable 5.2 and Mythos 5.2 were priced as Fable 5 by the collector and the band, and as
  Fable 5.1 by the session archive, so the three disagreed with Claude Code's own cost. They are now
  priced at $8 / $40 per MTok with $0.20 cache reads, and the archive prices Fable as 5.2.
- New: a **handoff nudge on/off** toggle in the band, beside the context ring. Click it to stop
  Sonnet and Opus being told to wrap up and hand off at about 180k tokens, for the rest of that
  session; click again to bring it back. The `handoffNudge` option still removes the nudge and the toggle.
- New: five skills for spending fewer tokens and less time, in every project. `check-changed`
  checks the changed files in one command, failures only. `plan-step` runs one plan step and stops.
  `fresh-start` writes a handoff and moves to a fresh session. `file-outline` gives a large file's
  outline with line ranges. `usage-review` shows a session's or project's tokens and API cost.
- Fix: the band stayed hidden after most turns on the desktop, whose end-of-turn box (recap,
  question, buttons) counts as a survey. The band now draws under it, without the next-step rows.
- New: the session archive. Every session's conversation is saved as it happens to
  `%USERPROFILE%\claude-session-archive\<session id>\` (`meta.json` and `rows-NNNN.jsonl`),
  for turning repeated work into skills later. `/archive` shows where; `sessionArchive` turns it off.
- New: `tools\clean-archive.py` boils the archive down (about a tenth of the size) to prompts,
  replies, one-line tool calls, result sizes, token readings, and a per-session index and report
  for spotting where tokens go; `--folder` adds a report for one project. Every session and
  request shows its tokens and API cost at today's prices for Opus 5.5, Sonnet 5.5, Haiku 5.5 and
  Fable 5.1, read from Claude Code's transcripts; `requests.md` lists them all.
  Each session and request also shows its effort level and run time, `report.md` breaks cost down
  by model and effort, and every call's usage is kept in the cleaned archive so it outlives the
  transcripts Claude Code deletes. Empty bookkeeping rows
  are no longer archived.
- New band layout: the full-width `── usage` / `── git` rules are gone. Each feature's rows sit
  beside a short dim label in a gutter (`usage │`, `git │`, `next │`), and the context ring closes
  the first row instead of taking one of its own.
- Next steps: hover a suggestion on desktop or VS Code to read its full prompt in a preview area
  under the list, which keeps its height so nothing moves. On the desktop the preview waits until
  the pointer has rested on a row for 200ms, follows it row to row, and stays while you read it.
- On the desktop, Commit and Push are text links instead of buttons.

## 1.0.1 - 2026-10-23

- The context ring now draws in the band above the prompt on desktop: the desktop app draws mod
  content only there and in panes, never in the prompt footer. The terminal keeps its `ctx N%` footer label.

## 1.0.0 - 2026-10-23

- One mod, `desktop-mods`, replaces three: commit-push (now the git bar), usage-meter and next-steps.
  Each feature switches on or off in the mod's options.
- New: the context ring, full at the auto-compact window (or the model's
  window, or `contextLimit`), with the numbers on hover; `ctx N%` on the terminal.
- Windows installer (`deploy\install.ps1`) for a checkout or straight from GitHub, which retires
  the old mods and moves the usage collector to a stable folder; uninstaller and options script.
- `tools\check.ps1`, `tools\package.ps1` (offline zip) and `tools\release.ps1` (version bump, tag, release).
