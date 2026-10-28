---
name: plan-step
description: Run exactly one step of a written plan (a docs/plans/<plan>/prompts/NN-*.md brief, as plan-proposal writes them), then report, commit and stop. Use when asked to "work on this doc", "do the next step", "continue the plan", "run prompt 04", or given a path to a plan prompt file. Keeps each step to one bounded run instead of letting one session roll on through the whole plan.
---

# Run one plan step

A plan step is a self-contained brief. Running it costs least in a fresh session that reads only
what the brief needs, does it, proves it, reports, and stops. The expensive pattern this skill
exists to prevent is "continue with the next step" turning into hundreds of calls across several
steps in one ever-growing session.

## 1. Find the step

- **Given a file** (`docs/plans/<plan>/prompts/04-x.md`): that is the step.
- **Asked for "the next step"**: open the plan's `README.md` run-order table. The next step is
  the first row whose report (`reports/<same name>.md`) is missing or whose `Status:` line is not
  `done`, and whose "Depends on" steps all have `done` reports. The reports win over the table when
  they disagree. If no step is ready, say which step is blocked by what, and stop.
- **Several plans and no path given**: list the plans with a ready step (one line each) and ask
  which. Don't guess.

If the step's report says `in progress`, another session may be on it: ask before starting.

## 2. Read only what it needs

Read the step file in full. Then read only the files it names, and only the parts it points at
(use an outline or `grep -n` to find them; read ranges, not whole large files). Don't read the
plan's other prompts, its other reports, or `overview.md` unless the step tells you to. Read the
reports of the steps it depends on only if it says to.

Check its "Do not start if" section before changing anything. If a condition holds, write the
report with status `not started`, say why, and stop.

## 3. Do the work

Do what the step says, and nothing past its "Done when" list. Work the step mentions as out of
scope, and anything you notice on the way, goes in the report as a follow-up, not into the code.

Mark the step `in progress` in its report file as soon as you start, so no one else picks it up.

## 4. Prove it

Run the checks the step names. For the routine checks (format, lint, typecheck, related tests) use
the `check-changed` skill (`npm run check:changed` where the project has it): one command, failures
only. Run the full suite only if the step asks for it. Fix failures, then check again. Go through
"Done when" item by item and confirm each one with evidence, not memory.

## 5. Report, commit, stop

1. Fill in the report at the path the step gives (its template is at the end of the step file),
   with `Status: done`, or `blocked` / `failed` with the reason.
2. Set the step's status in the plan's `README.md` table to match.
3. Commit the step's changes and its report together, with a message naming the step (for example
   `Plan people-and-devices 04: shared roles`). Commit only this step's files; leave anything else
   in the working tree alone.
4. Reply in a few lines: what was done, the checks and their results, any follow-ups, and which
   step is next and whether it is ready.

Then **stop**. Don't start the next step in this session, even if it is ready and small. The next
step belongs in a fresh session, which starts small instead of carrying this one's context.

## Starting the next step

When the app offers a tool to start a new session, offer to start the next ready step there with
the brief `Read and follow docs/plans/<plan>/prompts/<file>.md`. Suggest a cheaper model when the
step is routine (Sonnet costs half as much as Opus per token, and Haiku less again). When
there is no such tool, give the user that one-line brief to paste into a new session.

Steps in the same phase that the README says can run alongside each other can each get their own
session, each in its own worktree.
