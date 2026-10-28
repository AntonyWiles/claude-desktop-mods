---
name: fresh-start
description: Hand the work over to a fresh session with a short handoff note, so the next stretch starts small instead of re-reading a long conversation on every call. Use when the user says "fresh start", "hand off", "new session", "start clean" or "wrap up this context", when one task is done and an unrelated one is starting, or when the conversation has grown long (well past 100k tokens of context, or after a compaction) and more work remains.
---

# Fresh start

Every API call re-reads the whole conversation, and that re-reading is most of what a session
costs. A session that has grown to 200k tokens pays for 200k on every call; a fresh one carrying a
one-page note pays for a few thousand. Handing over at a natural break is usually the single biggest
saving in a long piece of work.

## When to offer it

- A task is finished and the user moves on to something unrelated.
- The context is large (the conversation has run long, many files were read, or it has been
  compacted) and there is still real work ahead.
- The user asks for it.

Offer it in one line ("This is a good point for a fresh session: shall I write a handoff?"), unless
they asked for it, in which case just do it. Don't hand over in the middle of a change that leaves
the code broken: finish or back out to a working state first.

## Write the handoff

Write it to `.claude/handoff.md` in the project (create `.claude/` if needed; replace an older
handoff, there is only ever one). Keep it under about 60 lines. It is for a reader who has seen
nothing of this conversation:

```markdown
# Handoff: <the work, in a few words>

Written <date and time> from session <id if known>. Branch `<branch>`, last commit `<short sha> <subject>`.

## Goal
<What the user wants, in their terms, and what "done" looks like.>

## Done
- <What is finished, with commit shas where there are some.>

## State right now
- <Uncommitted changes and what they are, or "working tree clean".>
- <Anything running, half-done or known broken, and how to tell.>

## Next
1. <The next concrete step.>
2. <Then...>

## Where things are
- `path/to/file.ts:120-180`: <what is there and why it matters>
- <Only the files the next steps need, with line ranges where it helps.>

## Decisions and constraints
- <Choices already made with the user, and why, so they aren't reopened.>
- <Things the user said not to do.>

## How to check
<The command that proves the work, e.g. `npm run check:changed`, and what passing looks like.>
```

Facts only: no history of how you got here, no file contents beyond a line or two, no copy of the
plan if it already lives in a file (link it). If the work is a plan step, the plan's own files are
the handoff: point at the step instead.

## Hand over

- If the app offers a tool to start a new session, offer to start one with the brief
  `Read .claude/handoff.md and continue from "Next".` Suggest a cheaper model when the remaining
  work is routine.
- Otherwise tell the user to open a new session in this project and paste that line.

Don't commit `.claude/handoff.md` unless the user wants it in the repository. If it isn't ignored,
mention that it is untracked.
