---
name: file-outline
description: Get the outline of a large file (its functions, classes, components, types and headings, each with its line range) before reading it, then read only the range you need. Use before opening any source or Markdown file over about 300 lines, when you need one function or section of a big file, or when you find yourself reading the same large file again.
---

# Outline before reading

A whole large file read into the conversation stays there and is re-read on every later call. Most
of the time only one function or section of it matters. Outline it, then read just that range.

## Run it

```
python "${CLAUDE_PLUGIN_ROOT}/skills/file-outline/outline.py" path/to/File.tsx
```

It prints each landmark with the lines it spans:

```
src/renderer/components/whiteboard/WhiteboardEditor.tsx (3,404 lines)
       1-202  imports and setup
     208-248  export interface WhiteboardEditorProps {
   1587-1617    const pasteCopies = (held: CopiedWhiteboardObjects) => {
   1701-1725    const paste = (event: ClipboardEvent) => {
```

Then read that range: the Read tool with `offset` and `limit` (here `offset: 1701, limit: 25`), or
`sed -n 1701,1725p`.

Options:

- several files at once: `outline.py a.ts b.ts`;
- `--grep paste`: only the landmarks whose line mentions `paste`;
- `--depth 1`: top level only (the default, 2, also shows one level in, such as a component's
  handlers or a class's methods).

It knows TypeScript and JavaScript, Python, Markdown, CSS, JSON, Go, Rust, Java, C#, C and C++,
PowerShell and shell. For anything else it says so; use `grep -n`.

## Rules

- For a file over about 300 lines, outline it first unless you truly need all of it.
- Read the range the outline gives, plus a few lines either side if you need context. Don't read
  the whole file "to be safe".
- Already read part of a file in this conversation? Don't read it again unless it changed since.
- To find where something is used rather than defined, `grep -n` is still the tool.
- Ranges are found from indentation and brackets, so they can be a line or two off in unusual
  code. If a range looks cut short, read a little further.
