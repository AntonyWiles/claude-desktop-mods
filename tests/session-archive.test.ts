import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

// The session archive alone: the mod's other features are switched off.
const ONLY = { options: { usageMeter: false, nextSteps: false, contextRing: false, gitBar: false } }
const OFF = { options: { ...ONLY.options, sessionArchive: false } }

const T0 = Date.UTC(2026, 9, 23, 2, 0, 0)
const FOLDER = '/home/t/claude-session-archive/sess-1'

// Stands for the engine's session and the file system beneath the plugin.
function world(on: Parameters<TestBody>[1], files = new Map<string, string>()) {
  mock.clock(on, { now: T0 })
  mock.env(on, { USERPROFILE: '/home/t' })
  // On Windows the engine hands paths over as C:\home\t\...: key the fake files by the POSIX form.
  const posix = (path: string) => path.replace(/^[A-Za-z]:/, '').replace(/\\/g, '/')
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: 'C:/repo' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.version', () => ({ value: { version: '2.1.308', base: '2.1.308', builtAt: '' } }) as never)
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('fs.exists', ($, e) => ({ value: files.has(posix(e.path)) }))
  on('fs.read', ($, e) => ({ value: files.get(posix(e.path)) ?? '' }) as never)
  on('fs.write', ($, e) => {
    files.set(posix(e.path), e.text)
    return { value: undefined }
  })
  on('fs.list', ($, e) => ({
    value: [...files.keys()]
      .filter(path => path.startsWith(`${posix(e.path)}/`))
      .map(path => ({ name: path.slice(posix(e.path).length + 1), kind: 'file', size: 0, mtimeMs: 0, isLink: false })),
  }) as never)
  return files
}

const prompt = (uuid: string, text: string) => ({
  door: 'prompt',
  origin: { kind: 'unclassified' },
  uuid,
  message: { type: 'user', role: 'user', content: [{ type: 'text', text }] },
}) as never

const rows = (files: Map<string, string>, part = 1) =>
  (files.get(`${FOLDER}/rows-000${part}.jsonl`) ?? '').split('\n').filter(Boolean).map(line => JSON.parse(line))

test('each stored row is archived, with the session described once', ONLY, async ($, on) => {
  const files = world(on)
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.append(prompt('u1', 'hello'))
  await $.session.append({
    door: 'response',
    origin: { kind: 'model', model: 'claude-opus-5-5' },
    uuid: 'u2',
    message: {
      type: 'assistant',
      role: 'assistant',
      content: [
        { type: 'text', text: 'hi' },
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAAA' } },
      ],
    },
  } as never)

  const meta = JSON.parse(files.get(`${FOLDER}/meta.json`) ?? '{}')
  expect(meta.sessionId).toBe('sess-1')
  expect(meta.cwd).toBe('C:/repo')
  expect(meta.startedAt).toBe(new Date(T0).toISOString())

  const kept = rows(files)
  expect(kept.map(r => r.uuid)).toEqual(['u1', 'u2'])
  expect(kept[0].content).toEqual([{ type: 'text', text: 'hello' }])
  expect(kept[1].origin).toEqual({ kind: 'model', model: 'claude-opus-5-5' })
  expect(kept[1].content[1]).toEqual({ type: 'image', omitted: true })
})

test('long text is cut and the queue’s copy of a prompt is skipped', ONLY, async ($, on) => {
  const files = world(on)
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.append(prompt('u1', 'x'.repeat(30_000)))
  await $.session.append({ ...(prompt('u2', 'again') as object), door: 'queue' } as never)

  const kept = rows(files)
  expect(kept.length).toBe(1)
  expect(kept[0].content[0].text.length).toBeLessThan(21_000)
  expect(kept[0].content[0].text).toContain('10000 more characters cut')
})

test('empty bookkeeping rows are not archived', ONLY, async ($, on) => {
  const files = world(on)
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.append({
    door: 'hook-context', origin: { kind: 'hook', event: 'PostToolUse' }, uuid: 'h1',
    message: { type: 'attachment', name: 'hook_success', content: [] },
  } as never)
  await $.session.append({
    door: 'notice', origin: { kind: 'engine' }, uuid: 'n1',
    message: { type: 'system', name: 'stop_hook_summary', content: [] },
  } as never)
  await $.session.append(prompt('u1', 'hello'))

  expect(rows(files).map(r => r.uuid)).toEqual(['u1'])
})

test('after a reload it carries on in the newest part', ONLY, async ($, on) => {
  const files = new Map([
    [`${FOLDER}/meta.json`, '{}'],
    [`${FOLDER}/rows-0001.jsonl`, '{"uuid":"old1"}\n'],
    [`${FOLDER}/rows-0002.jsonl`, '{"uuid":"old2"}\n'],
  ])
  world(on, files)
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.append(prompt('u3', 'next'))

  expect(rows(files, 1).map(r => r.uuid)).toEqual(['old1'])
  expect(rows(files, 2).map(r => r.uuid)).toEqual(['old2', 'u3'])
})

test('switched off, nothing is written', OFF, async ($, on) => {
  const files = world(on)
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.session.append(prompt('u1', 'hello'))
  expect(files.size).toBe(0)
})
