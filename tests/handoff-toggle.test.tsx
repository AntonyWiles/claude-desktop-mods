import { expect, test } from 'claude-code/testing'

const OPTIONS = { options: { usageMeter: false, sessionArchive: false, gitBar: false, nextSteps: false } }

const BAND = {
  plugin: 'desktop-mods',
  component: 'AbovePrompt',
  surface: 'desktop',
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as never

test('the band toggle pauses and resumes the handoff nudge', OPTIONS, async ($, on) => {
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.measure', (_, e) => ({ changed: [...e.changed] }))
  on('session.usage', () => ({ value: { startedAt: 0, rateLimits: [], context: { window: 1_000_000 } } }) as never)
  on('tool.call', () => ({ result: { stdout: '', stderr: '', interrupted: false } }) as never)
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.measure({ context: { tokens: 190_000, window: 1_000_000, percent: 19 }, rateLimits: [], changed: ['context'] } as never)

  const band = await $.ui.mount({ ...(BAND as object), requestId: 'band' } as never)
  const toggle = await band.find({ key: 'handoff-toggle' })
  expect(toggle?.props.label).toBe('handoff nudge on')

  // Paused: the tool result carries no note, however full the context.
  await band.press({ key: 'handoff-toggle' })
  expect((await band.find({ key: 'handoff-toggle' }))?.props.label).toBe('handoff nudge off')
  const quiet = await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(JSON.stringify(quiet)).not.toContain('Context is at')

  // On again: the note was not spent while paused, so the next result carries it.
  await band.press({ key: 'handoff-toggle' })
  expect((await band.find({ key: 'handoff-toggle' }))?.props.label).toBe('handoff nudge on')
  const loud = await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(JSON.stringify(loud)).toContain('Context is at 190k tokens')
  await band.unmount()
})

test('no toggle when the handoff nudge option is off', { options: { ...OPTIONS.options, handoffNudge: false } }, async ($, on) => {
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  const band = await $.ui.mount({ ...(BAND as object), requestId: 'band-off' } as never)
  expect(await band.find({ key: 'handoff-toggle' })).toBeUndefined()
  await band.unmount()
})
