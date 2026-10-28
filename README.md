# claude-desktop-mods

One mod for Claude Code on Windows (the desktop app's Code tab and the terminal),
holding five features that each switch on or off:

| Feature | What it does | Option |
| --- | --- | --- |
| **Context ring** | A small ring at the right end of the band's first row that fills as the context window does. Full is your auto-compact window (say 400k on a 1M model), or the model's window when auto-compact is off. Hover it for the numbers. The terminal shows `ctx 52%` in its prompt footer instead. | `contextRing`, `contextLimit` |
| **Git bar** | Commit (Haiku writes the message) and Push buttons, branch picker and diff count above the prompt. | `gitBar` |
| **Usage meter** | Plan-limit %, tokens and API-equivalent $ in the band above the prompt, and the `/burn` pane. Needs its collector (installed by `deploy\install.ps1`). From [rohit3a/usage-meter](https://github.com/rohit3a/usage-meter). | `usageMeter` |
| **Next steps** | After each turn, up to three suggested next prompts above the input: press 1, 2 or 3 to put one in the prompt box, 0 to dismiss. From Thariq Shihipar's [next-steps](https://github.com/anthropics/claude-plugins-community/tree/main/next-steps) plugin. | `nextSteps`, `minAnswerChars`, `suggestSkills` |
| **Session archive** | Saves every session's conversation as it happens to `%USERPROFILE%\claude-session-archive\<session id>\`: `meta.json` (folder, model, start time) and `rows-NNNN.jsonl`, one line per prompt, response, tool call and result, subagents' included. Long text is cut at 20,000 characters and images are left out. `/archive` shows where the current session goes. For reading back later, to turn repeated work into skills. `tools\clean-archive.py` boils it down for spotting where tokens go; see [Cleaning the archive](#cleaning-the-archive). | `sessionArchive` |

Two features are other people's work, brought in here under the MIT licence: the usage meter is
Rohit's [usage-meter](https://github.com/rohit3a/usage-meter), and next steps is Thariq Shihipar's
[next-steps](https://github.com/anthropics/claude-plugins-community/tree/main/next-steps). Thanks to both; see [Credits and licence](#credits-and-licence).

## Install

On the machine you develop on, from this checkout (edits go live with `/reload-plugins`):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install.ps1
```

On any other Windows machine, straight from GitHub:

```powershell
irm https://raw.githubusercontent.com/AntonyWiles/claude-desktop-mods/main/deploy/install.ps1 | iex
```

Or, without the installer (no usage collector), in a terminal session:

`/plugin install desktop-mods --marketplace AntonyWiles/claude-desktop-mods`

In the desktop app, where `/plugin` doesn't run: click + next to the prompt box, then Plugins,
Add marketplace with `AntonyWiles/claude-desktop-mods`, then Add plugin.

Offline: copy `dist\desktop-mods-<version>.zip` over (see Releasing), unzip it, and run
`deploy\install.ps1` from inside the unzipped folder.

The installer:

1. disables the old separate mods (`commit-push@local-mods`, `next-steps@claude-community`) and
   removes the `~/.claude/skills/usage-meter` link, so nothing draws twice (`-KeepOld` skips this);
2. adds this repo as a marketplace and installs `desktop-mods` (or updates it);
3. copies the usage collector to `~/.claude/desktop-mods/app` and runs it as the hidden
   Scheduled Task `usage-meter` at logon (`-NoCollector` skips this; needs Python 3.9+).
   Usage history in `~/.claude/usage-meter` is kept.

Collector options are passed through: `-HostName Desktop`, and for HTTP sync between machines
`-Peer laptop=http://192.168.1.20:8765 -Listen 8765 -Token <shared> -Allow 192.168.1.0/24`.

Then open a new session, or run `/reload-plugins` in an open one.

Remove it with `deploy\uninstall.ps1` (`-RestoreOld` re-enables the old mods, `-RemoveData` deletes usage history).

## Options

Set them in the config menu (`/config`, in a terminal session), or from PowerShell:

```powershell
claude plugin configure desktop-mods@claude-desktop-mods          # show them
'{"nextSteps":"false","contextLimit":"400000"}' | claude plugin configure desktop-mods@claude-desktop-mods --values-stdin
```

`deploy\set-options.ps1 -Set nextSteps=false,contextLimit=400000` does the same.

| Option | Default | |
| --- | --- | --- |
| `contextRing` | `true` | the context ring |
| `contextLimit` | `0` | tokens at which the ring is full; `0` reads the auto-compact window |
| `gitBar` | `true` | the git bar |
| `usageMeter` | `true` | the usage meter's band, pane and `/burn` |
| `nextSteps` | `true` | next-step suggestions |
| `minAnswerChars` | `80` | no suggestions after answers shorter than this |
| `suggestSkills` | `true` | let suggestions name your skills and slash commands |
| `sessionArchive` | `true` | the session archive and `/archive` |

## Updating

- **From a checkout:** `git pull`, then `/reload-plugins`. Re-run `deploy\install.ps1` when `collector/` changed.
- **From GitHub:** re-run the install line; it runs `claude plugin update` and refreshes the collector.
- `claude plugin list` shows the installed version.

## Cleaning the archive

The raw archive grows fast, mostly with tool output, file bodies and system reminders.
`tools\clean-archive.py` (Python 3.9+, no packages) keeps the shape of the work and drops the bulk:

```
python tools\clean-archive.py
```

It writes `%USERPROFILE%\claude-session-archive-clean\`:

- `sessions\<session id>.jsonl`: one line per event, with its time. Prompts and slash commands in
  full, Claude's replies (cut at 2,000 characters), each tool call as a short summary (the command,
  the file, the pattern), each result's size and whether it failed, questions asked and their
  answers, compactions, subagents, and the `tokens left` readings, so a stretch of work's cost can
  be read off.
- `index.jsonl`: one line per session: folder, model, times, prompt and tool counts, tokens used,
  tool output and input sizes by tool, reminder and hook-context sizes by kind, and the files and
  commands it went back to again and again.
- `report.md`: the same totals across every session, biggest first.
- `report-<folder>.md`: with `--folder C:\path\to\project`, the same report counting only the
  sessions run in that folder or below it.

Run it again whenever you like: sessions unchanged since the last run are skipped. It only reads the
raw archive. To reclaim the space, `--delete-raw` deletes the `rows-*.jsonl` of sessions that are
cleaned and have been idle `--older-than` days (14 by default); `meta.json` stays. Deleted rows
can't be cleaned again later, so keep them until you're happy with what the cleaned copy keeps.

The archive itself no longer records empty bookkeeping rows (hook successes, stop-hook summaries),
which were most of its rows.

## Layout

```
.claude-plugin/        plugin.json (name, version, options) and marketplace.json
hooks/register.tsx     the one hooks module: chains every feature's hooks per event
hooks/band.tsx         the band above the prompt: every feature's rows under a labelled gutter
hooks/features/        context-ring, git-bar, next-steps, usage-meter, session-archive
hooks/hook-types.ts    parameter types for hooks written as function declarations
types/index.d.ts       the $.state contract for every feature
collector/             the usage meter's collector (Python), prices.json beside it
deploy/                install.ps1, uninstall.ps1, run_hidden.pyw
scripts/push.ps1       the git bar's push
tools/                 check.ps1, package.ps1, release.ps1, clean-archive.py
tests/                 claude plugin test suites, one per feature plus a combined one
```

### Adding a feature

1. Add `hooks/features/<name>.tsx`. Write each hook as an exported `async function`
   taking `($: EngineInterface, e: In<'event'>, next: NextOf<'event'>)` (types from
   `hooks/hook-types.ts`). The engine follows `$` across files only into function
   declarations, and a module may hook each event only once, so features never call `on`.
2. Chain it into the event's hook in `hooks/register.tsx`, behind an option. A feature that
   draws in the band exports `bandSection($, e)` returning `{ label, rows }` (or `null`), and
   `register.tsx` adds it to the sections it hands `drawBand` in `hooks/band.tsx`.
3. Add the option to `userConfig` in `plugin.json`, any `$.state` values to `types/index.d.ts`
   (all under `'desktop-mods'`), and a `tests/<name>.test.tsx` that turns the other features off.

## Developing

```powershell
powershell -ExecutionPolicy Bypass -File tools\check.ps1   # validate + tests + collector compile
```

## Releasing

Add what changed under `## Unreleased` in `CHANGELOG.md`, commit, then:

```powershell
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch   # or minor / major
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump minor -Push   # also push + GitHub release
```

It runs the checks, bumps `version` in `plugin.json`, dates the changelog, commits, tags
`v<version>` and builds `dist\desktop-mods-<version>.zip`. Machines installed from GitHub pick
it up on their next `claude plugin update` (or install-script run).

## Credits and licence

MIT (see `LICENSE`). The usage meter is Rohit's [usage-meter](https://github.com/rohit3a/usage-meter)
(MIT, `licenses/usage-meter.LICENSE`) with a Windows port; next steps is Thariq Shihipar's
[next-steps](https://github.com/anthropics/claude-plugins-community/tree/main/next-steps) plugin (MIT). See `NOTICE`.
