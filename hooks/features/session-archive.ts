// session-archive: every row a conversation keeps (prompts, responses, tool
// calls and results, subagents' rows too) is written as it is stored to
// %USERPROFILE%\claude-session-archive\<session id>\, as meta.json and
// rows-NNNN.jsonl, one JSON line per row. The archive is for reading back
// later, to see how the work went and which steps repeat, so long text is
// cut and images and documents are left out. /archive says where it goes.

import type { EngineInterface } from 'claude-code'

import type { In, MatchedIn, NextOf } from '../hook-types'

// $.fs.write replaces a whole file, so a part is rewritten on every row.
// Parts roll over well below $.fs.read's 4 MiB, so a reloaded module can
// read the newest part back and carry on.
const PART_LIMIT = 1_500_000
// A tool result or file dump past this is cut: the shape of the work
// matters here, not every byte a tool printed.
const TEXT_LIMIT = 20_000

type Log = { folder: string; part: number; text: string; rows: number }

const logs = new Map<string, Promise<Log>>()
let queue: Promise<unknown> = Promise.resolve()

const pad = (n: number) => String(n).padStart(4, '0')
const partPath = (log: Log) => `${log.folder}/rows-${pad(log.part)}.jsonl`

async function archiveRoot($: EngineInterface): Promise<string> {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '.'
  return `${home.replace(/\\/g, '/')}/claude-session-archive`
}

async function openLog($: EngineInterface, sessionId: string): Promise<Log> {
  const folder = `${await archiveRoot($)}/${sessionId}`
  if (!(await $.fs.exists(`${folder}/meta.json`))) {
    const [cwd, model, version, now] = await Promise.all([
      $.session.cwd(), $.session.model(), $.session.version(), $.clock.now(),
    ])
    const meta = {
      sessionId, cwd, model, claudeCode: version.version,
      startedAt: new Date(now).toISOString(),
    }
    await $.fs.write(`${folder}/meta.json`, JSON.stringify(meta, null, 2) + '\n')
    return { folder, part: 1, text: '', rows: 0 }
  }
  // Picked up again after a reload: continue the newest part on disk.
  const parts = (await $.fs.list(folder))
    .map(entry => /^rows-(\d+)\.jsonl$/.exec(entry.name)?.[1])
    .filter((n): n is string => n !== undefined)
    .map(Number)
  const part = parts.length > 0 ? Math.max(...parts) : 1
  const log: Log = { folder, part, text: '', rows: 0 }
  if (await $.fs.exists(partPath(log))) {
    log.text = await $.fs.read(partPath(log))
    log.rows = log.text.split('\n').filter(Boolean).length
  }
  return log
}

function logFor($: EngineInterface, sessionId: string): Promise<Log> {
  let log = logs.get(sessionId)
  if (log === undefined) {
    log = openLog($, sessionId)
    logs.set(sessionId, log)
  }
  return log
}

const cut = (text: string) =>
  text.length > TEXT_LIMIT
    ? `${text.slice(0, TEXT_LIMIT)}\n…[${text.length - TEXT_LIMIT} more characters cut by session-archive]`
    : text

// Keeps text, tool calls and tool results; swaps images and documents for
// a placeholder so base64 never lands in the archive.
function slim(block: unknown): unknown {
  if (typeof block !== 'object' || block === null) return block
  const b = block as Record<string, unknown>
  switch (b.type) {
    case 'text': return { type: 'text', text: cut(String(b.text ?? '')) }
    case 'thinking': return { type: 'thinking', thinking: cut(String(b.thinking ?? '')) }
    case 'redacted_thinking': return { type: 'redacted_thinking' }
    case 'image': case 'document': return { type: b.type, omitted: true }
    case 'tool_use': {
      const input = JSON.stringify(b.input ?? {})
      return { type: 'tool_use', id: b.id, name: b.name,
        input: input.length > TEXT_LIMIT ? cut(input) : b.input }
    }
    case 'tool_result': {
      const content = typeof b.content === 'string'
        ? cut(b.content)
        : Array.isArray(b.content) ? b.content.map(slim) : b.content
      return { type: 'tool_result', tool_use_id: b.tool_use_id, is_error: b.is_error, content }
    }
    default: return { type: b.type }
  }
}

function slimContent(content: unknown): unknown {
  if (typeof content === 'string') return cut(content)
  return Array.isArray(content) ? content.map(slim) : content
}

async function record($: EngineInterface, e: In<'session.append'>, sessionId: string) {
  const log = await logFor($, sessionId)
  const { message } = e
  const row = {
    at: new Date(await $.clock.now()).toISOString(),
    uuid: e.uuid,
    agentId: e.agentId,
    door: e.door,
    origin: e.origin,
    type: message.type,
    name: message.name,
    role: message.role,
    isMeta: message.isMeta,
    content: slimContent(message.content),
  }
  const line = JSON.stringify(row) + '\n'
  if (log.text.length > 0 && log.text.length + line.length > PART_LIMIT) {
    log.part += 1
    log.text = ''
  }
  log.text += line
  log.rows += 1
  await $.fs.write(partPath(log), log.text)
}

export async function sessionStart($: EngineInterface, e: In<'session.start'>, next: NextOf<'session.start'>) {
  await $.command.register({
    name: 'archive',
    description: 'Shows where this session is being archived.',
  })
  return next(e)
}

// Hook successes, tool-list records and stop-hook summaries with nothing in
// them: most of the archive's rows, and none of its information.
function isEmptyBookkeeping(e: In<'session.append'>): boolean {
  const { type, content } = e.message
  return (type === 'attachment' || type === 'system') && Array.isArray(content) && content.length === 0
}

// Stored first, then archived. Writes run one at a time so a part never
// interleaves, and a failed write never holds up the conversation.
export async function sessionAppend($: EngineInterface, e: In<'session.append'>, next: NextOf<'session.append'>) {
  const stored = await next(e)
  if (e.door === 'queue') return stored // the queue's copy of a prompt row
  if (isEmptyBookkeeping(e)) return stored

  const sessionId = await $.session.id()
  const write = queue.then(() => record($, e, sessionId))
  queue = write.catch(() => undefined)
  await write.catch(() => undefined)
  return stored
}

export async function runArchive($: EngineInterface, _e: MatchedIn<'command.run', { command: 'archive' }>) {
  const log = await logFor($, await $.session.id())
  return { text: `Archiving to ${log.folder} (${log.rows} rows in part ${log.part}).` }
}
