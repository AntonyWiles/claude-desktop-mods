---
name: check-changed
description: Check your work with one command instead of running the formatter, linter, typechecker and tests one at a time. Formats, lints, typechecks and runs the related tests for the files changed since the last commit, printing only what failed. Use after editing code and before reporting or committing, whenever you would otherwise run prettier, eslint, tsc, vitest, jest, ruff, mypy or pytest to see whether a change works.
---

# Check changed files

Every command costs a turn, and every turn re-reads the whole conversation. Running the formatter,
linter, typechecker and tests separately is four or five turns and four or five long outputs. This
is one turn and a few lines.

## Run it

From the project folder:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/check-changed/check_changed.py"
```

If the project's `package.json` has its own `check:changed` script, this runs that instead.
Otherwise it finds the project's installed tools and, for the files changed since the last commit
plus new files:

1. formats them (prettier or biome; ruff format or black), naming any file it rewrote;
2. lints them (eslint; ruff check);
3. typechecks (the `typecheck` npm script or `tsc --noEmit`; mypy when the project configures it);
4. runs only the related tests (`vitest related`, `jest --findRelatedTests`; pytest on the changed
   tests and the `test_<module>.py` of each changed module).

It prints one line per step and only the failures, then `all passed` or `FAILED (steps)`, and
exits non-zero on failure. It never installs or downloads anything: a tool the project doesn't
have is skipped.

Options:

- `--base main`: the files this branch changed since `main`.
- `--no-tests`: format, lint and typecheck only.
- `path/a.ts path/b.py`: check these files instead of the changed ones.

## Rules

- Run it once after a batch of edits, not after every edit. Fix everything it reports, then run it
  again.
- Don't re-run a tool on its own to see more unless the output isn't enough to fix the failure.
  Then run just that tool on just that file, with short output (`| tail -30`).
- Don't run it again when nothing has changed since it last passed.
- If it finds no tools, run the project's own checks (its README or AGENTS.md says which), keeping
  output short.
- It is a quick check, not a release gate. When the project has a full gate (`npm run verify`,
  `make check`) and the owner asks for it, run that.
