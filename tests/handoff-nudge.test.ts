import { expect, test } from 'claude-code/testing'

import { isBigModel, noteFor } from '../hooks/features/handoff-nudge'

test('nudges only Sonnet and Opus', async () => {
  expect(isBigModel('claude-opus-5-5')).toBe(true)
  expect(isBigModel('claude-sonnet-5-5[1m]')).toBe(true)
  expect(isBigModel('claude-haiku-5-5')).toBe(false)
})

test('the note names the size and the handoff', async () => {
  const note = noteFor(183_400)
  expect(note).toContain('183k')
  expect(note).toContain('.claude/handoff.md')
  expect(note).toContain('CLEAN')
})
