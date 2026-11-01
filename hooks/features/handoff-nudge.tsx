import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, RenderElement } from 'claude-code'

import type { BandIn } from '../band'
import type { In, NextOf } from '../hook-types'

// Handoff nudge: once a Sonnet or Opus session's context passes a mark (180k
// by default, so it stops near 200k, before its auto-compact window), tell the model to stop
// at the next clean point and write a handoff with the fresh-start skill.
// The note rides on tool results (mid-turn) and on prompts the user sends.
// It is sent once per window; again only after another `repeatEvery` tokens,
// and the count starts over when the context shrinks (compaction, /clear).
// A toggle in the band pauses it for the session (the option removes it).

const isPaused = atom({ plugin: 'desktop-mods', key: 'isHandoffPaused' } as const, false)

let mark = 180_000
let repeatEvery = 50_000

// The context as last measured, and where the last note was sent (0: none).
let tokens = 0
let nudgedAt = 0
let model = ''

export function configure(options: PluginOptions): void {
  const n = options?.handoffAt
  mark = typeof n === 'number' && n > 0 ? n : 180_000
  const r = options?.handoffRepeat
  repeatEvery = typeof r === 'number' && r > 0 ? r : 50_000
}

export function isBigModel(id: string) {
  return /sonnet|opus/i.test(id)
}

export function noteFor(t: number) {
  const k = Math.round(t / 1000)
  return (
    `Context is at ${k}k tokens. Stop at the next CLEAN point: finish the edit or step in progress (never undo ` +
    `or revert work to get there), and do not start a new step. Then follow the fresh-start skill: ` +
    `write .claude/handoff.md for the next agent and tell the user the session is ready to hand over, ` +
    `with the one-line prompt for the new session. Do not keep working past that point in this session.`
  )
}

// The note to send now, if one is due; records it as sent. Nothing while the
// band's toggle has it paused, and nothing is recorded, so turning it back on
// sends the note at once if the context is already past the mark.
async function due($: EngineInterface): Promise<string | null> {
  if (await read($, isPaused)) return null
  if (!isBigModel(model) || tokens < mark) return null
  if (nudgedAt && tokens < nudgedAt + repeatEvery) return null
  nudgedAt = tokens
  return noteFor(tokens)
}

async function readModel($: EngineInterface) {
  model = await Promise.resolve($.session.model()).catch(() => model)
}

export async function sessionStart($: EngineInterface, e: In<'session.start'>, next: NextOf<'session.start'>) {
  const result = await next(e)
  tokens = 0
  nudgedAt = 0
  await readModel($)

  return result
}

export async function sessionMeasure($: EngineInterface, e: In<'session.measure'>, next: NextOf<'session.measure'>) {
  const result = await next(e)
  if (e.changed.includes('context')) {
    const t = e.context.tokens ?? 0
    // A smaller window than the last note: compacted or cleared, start over.
    if (t < nudgedAt) nudgedAt = 0
    tokens = t
  }
  if (e.changed.includes('model' as never)) await readModel($)

  return result
}

export async function toolCall($: EngineInterface, e: In<'tool.call'>, next: NextOf<'tool.call'>) {
  const result = await next(e)
  if (!('result' in result) || result.deny !== undefined) return result
  await readModel($)
  const note = await due($)

  return note ? { ...result, context: [...(result.context ?? []), note] } : result
}

export async function promptSubmit($: EngineInterface, e: In<'prompt.submit'>, next: NextOf<'prompt.submit'>) {
  await readModel($)
  const note = await due($)

  return next(note ? { ...e, context: [...(e.context ?? []), note] } : e)
}

// The toggle for the band, drawn beside the ring: click to stop the model
// being told to wrap up at the mark, click again to bring that back.
export async function bandToggle($: EngineInterface, e: BandIn): Promise<RenderElement | null> {
  const paused = await read($, isPaused)
  const { Button } = $.ui.resolve(e)

  return (
    <Button
      key="handoff-toggle"
      label={paused ? 'handoff nudge off' : 'handoff nudge on'}
      plain
      dimColor
      onPress={() => void update($, isPaused, value => !value)}
    />
  )
}
