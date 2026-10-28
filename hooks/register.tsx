import type { Register } from 'claude-code'

import { drawBand } from './band'

import {
  configure as configureRing,
  bandRing,
  renderSessionMode as ringRenderSessionMode,
  sessionEnd as ringSessionEnd,
  sessionMeasure as ringSessionMeasure,
  sessionStart as ringSessionStart,
} from './features/context-ring'
import {
  bandSection as gitBandSection,
  clientMessage as gitMessage,
  sessionAppend as gitSessionAppend,
  sessionStart as gitSessionStart,
  toolCall as gitToolCall,
  turnComplete as gitTurnComplete,
  turnStart as gitTurnStart,
} from './features/git-bar'
import {
  configure as configureNext,
  bandSection as nextBandSection,
  clientMessage as nextMessage,
  turnComplete as nextTurnComplete,
  turnStart as nextTurnStart,
} from './features/next-steps'
import {
  configure as configureHandoff,
  promptSubmit as handoffPromptSubmit,
  sessionMeasure as handoffSessionMeasure,
  sessionStart as handoffSessionStart,
  toolCall as handoffToolCall,
} from './features/handoff-nudge'
import {
  runArchive,
  sessionAppend as archiveSessionAppend,
  sessionStart as archiveSessionStart,
} from './features/session-archive'
import {
  agentSpawn as usageAgentSpawn,
  bandSection as usageBandSection,
  renderPane as usageRenderPane,
  runBurn as usageRunBurn,
  sessionEnd as usageSessionEnd,
  sessionMeasure as usageSessionMeasure,
  sessionStart as usageSessionStart,
  toolCallBash as usageToolCallBash,
  turnComplete as usageTurnComplete,
  turnStart as usageTurnStart,
  turnStep as usageTurnStep,
} from './features/usage-meter'

// One mod, six features, each switched on or off in the mod's options (the
// config menu, or `pluginConfigs` in settings); a change reloads the module.
//
// A module hooks each event once, so where several features hook the same
// event they are chained here by hand: each feature's hook gets the next
// feature's as its `next`, the last one the engine's. A feature that is off
// passes straight through. The band above the prompt is drawn once, by
// hooks/band.tsx: each feature hands it a section (usage, git, next, top to
// bottom) and the context ring closes the first row. The session archive
// draws nothing; it records each row once it is stored.
export const register: Register = (on, options) => {
  const isOn = (name: string) => options?.[name] !== false
  const ring = isOn('contextRing')
  const git = isOn('gitBar')
  const usage = isOn('usageMeter')
  const steps = isOn('nextSteps')
  const archive = isOn('sessionArchive')
  const handoff = isOn('handoffNudge')

  configureRing(options)
  configureNext(options)
  configureHandoff(options)

  on('session.start', ($, e, next) => {
    const toHandoff = (ev: typeof e) => (handoff ? handoffSessionStart($, ev, next) : next(ev))
    const toRing = (ev: typeof e) => (ring ? ringSessionStart($, ev, toHandoff as never) : toHandoff(ev))
    const toUsage = (ev: typeof e) => (usage ? usageSessionStart($, ev, toRing as never) : toRing(ev))
    const toGit = (ev: typeof e) => (git ? gitSessionStart($, ev, toUsage as never) : toUsage(ev))

    return archive ? archiveSessionStart($, e, toGit as never) : toGit(e)
  })

  on('session.end', ($, e, next) => {
    const toRing = (ev: typeof e) => (ring ? ringSessionEnd($, ev, next) : next(ev))

    return usage ? usageSessionEnd($, e, toRing as never) : toRing(e)
  })

  on('session.measure', ($, e, next) => {
    const toHandoff = (ev: typeof e) => (handoff ? handoffSessionMeasure($, ev, next) : next(ev))
    const toRing = (ev: typeof e) => (ring ? ringSessionMeasure($, ev, toHandoff as never) : toHandoff(ev))

    return usage ? usageSessionMeasure($, e, toRing as never) : toRing(e)
  })

  on('session.append', ($, e, next) => {
    const toGit = (ev: typeof e) => (git ? gitSessionAppend($, ev, next) : next(ev))

    return archive ? archiveSessionAppend($, e, toGit as never) : toGit(e)
  })

  on('turn.start', ($, e, next) => {
    const toUsage = (ev: typeof e) => (usage ? usageTurnStart($, ev, next) : next(ev))
    const toGit = (ev: typeof e) => (git ? gitTurnStart($, ev, toUsage as never) : toUsage(ev))

    return steps ? nextTurnStart($, e, toGit as never) : toGit(e)
  })

  on('turn.complete', ($, e, next) => {
    const toUsage = (ev: typeof e) => (usage ? usageTurnComplete($, ev, next) : next(ev))
    const toGit = (ev: typeof e) => (git ? gitTurnComplete($, ev, toUsage as never) : toUsage(ev))

    return steps ? nextTurnComplete($, e, toGit as never) : toGit(e)
  })

  on('agent.spawn', ($, e, next) => (usage ? usageAgentSpawn($, e, next) : next(e)))

  on('tool.call', ($, e, next) => {
    const toHandoff = (ev: typeof e) => (handoff ? handoffToolCall($, ev, next) : next(ev))
    const toUsage = (ev: typeof e) => (usage && ev.tool === 'Bash' ? usageToolCallBash($, ev as never, toHandoff as never) : toHandoff(ev))

    return git ? gitToolCall($, e, toUsage as never) : toUsage(e)
  })

  on('prompt.submit', ($, e, next) => (handoff ? handoffPromptSubmit($, e, next) : next(e)))

  on('turn.step', async function* ($, e, next) {
    return usage ? yield* usageTurnStep($, e, next) : yield* next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)
    // The desktop's end-of-turn box (recap, question, buttons) holds the band
    // as a survey after most turns. The band still draws under it, without
    // the next-step rows: the box suggests the next step itself, and their
    // 1/2/3 hotkeys would clash with its buttons.
    const survey = e.props.hasSurvey

    const [usageRows, gitRows, nextRows, ringMark] = await Promise.all([
      usage ? usageBandSection($, e) : null,
      git ? gitBandSection($, e) : null,
      steps && !survey ? nextBandSection($, e) : null,
      ring ? bandRing($, e) : null,
    ])
    const sections = [usageRows, gitRows, nextRows].filter(section => section !== null)

    return drawBand($, e, below, sections, ringMark)
  })

  // Presses in the desktop's Clients: the git links and the next-step list.
  on('ui.message', ($, e, next) => {
    const toSteps = (ev: typeof e) => (steps ? nextMessage($, ev, next) : next(ev))

    return git ? gitMessage($, e, toSteps as never) : toSteps(e)
  })

  on('ui.render', { component: 'SessionMode' }, ($, e, next) => (ring ? ringRenderSessionMode($, e, next) : next(e)))

  if (usage) {
    on('ui.render', { component: 'Pane', requestId: 'usage-meter' }, ($, e) => usageRenderPane($, e))
    on('command.run', { command: 'burn' }, ($, e) => usageRunBurn($, e))
  }

  if (archive) {
    on('command.run', { command: 'archive' }, ($, e) => runArchive($, e))
  }
}
