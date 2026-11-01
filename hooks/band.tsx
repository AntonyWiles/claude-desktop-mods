/* @jsxRuntime classic */
/* @jsx h */
/* @jsxFrag Fragment */
// The band above the prompt, laid out once for every feature: each feature
// hands over a section (a short label and its rows), and the band draws the
// sections one under the other with the labels in a dim, right-aligned
// gutter, so the rows line up:
//
//   usage │ last turn · $0.16 · 4 requests · 624k tok          details hide ◔
//     git │ [Commit] [Push]  claude-desktop-mods  main ⌄  +0 −0 · 2 to push
//    next │ 1 Cut a minor release and push
//
// The handoff nudge's toggle and the context ring, where a surface draws it,
// close the first row.

import type { EngineInterface, RenderElement } from 'claude-code'

import type { MatchedIn } from './hook-types'

export type BandIn = MatchedIn<'ui.render', { component: 'AbovePrompt' }>

export type Section = { label: string; rows: RenderElement[] }

// The label column, and the ` │ ` after it.
export const GUTTER = 5
export const GUTTER_COLUMNS = GUTTER + 3

// The columns a section's row has for itself.
export function contentColumns(e: BandIn): number {
  return Math.max(20, e.props.bodyColumns - GUTTER_COLUMNS)
}

// `below` is what the engine and any other plugin drew. Their node may not
// sit under a sized Box, so only the band's own rows are sized.
export function drawBand(
  $: EngineInterface,
  e: BandIn,
  below: RenderElement,
  sections: Section[],
  ring: RenderElement | null,
  toggle: RenderElement | null = null,
): RenderElement {
  const { Box, Text } = $.ui.resolve(e)
  const shown = sections.filter(section => section.rows.length > 0)
  if (shown.length === 0 && ring === null && toggle === null) return below

  const rows = shown.flatMap(section =>
    section.rows.map((row, index) => ({ label: index === 0 ? section.label : '', row, key: `${section.label}:${index}` })),
  )
  // The ring and toggle alone still get a row of their own, at the right.
  if (rows.length === 0) rows.push({ label: '', row: <Box />, key: 'ring' })

  return (
    <Box flexDirection="column">
      {below}
      <Box flexDirection="column" width={e.props.bodyColumns}>
        {rows.map(({ label, row, key }, index) => (
          <Box key={`band:${key}`} flexDirection="row" width={e.props.bodyColumns}>
            <Box width={GUTTER} flexShrink={0} justifyContent="flex-end">
              <Text dimColor>{label}</Text>
            </Box>
            <Text dimColor>{' │ '}</Text>
            <Box flexDirection="row" flexGrow={1} flexShrink={1}>
              {row}
            </Box>
            {index === 0 && (ring !== null || toggle !== null) ? (
              <Box flexShrink={0} marginLeft={1} gap={1}>
                {toggle}
                {ring}
              </Box>
            ) : null}
          </Box>
        ))}
      </Box>
    </Box>
  )
}
