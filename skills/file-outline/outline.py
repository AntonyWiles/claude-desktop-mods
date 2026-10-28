"""The outline of a file, with line ranges, so only the part needed gets read.

For each declaration (function, class, component, type, constant) or heading,
prints the lines it spans and its first line:

  src/renderer/components/whiteboard/WhiteboardEditor.tsx (2,140 lines)
       1-38    imports
      40-61    export interface WhiteboardEditorProps {
      63-2140  export function WhiteboardEditor({ board, onChange }: WhiteboardEditorProps) {
     118-140     const handleDrop = useCallback((event: DragEvent) => {

TypeScript/JavaScript, Python, Markdown, CSS, JSON, Go, Rust, Java, C#, C/C++,
PowerShell and shell. --depth sets how many levels of nesting to show (default
2: top level and one level in).

  python outline.py FILE [FILE ...] [--depth N] [--grep TEXT]
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

WIDTH = 110

JS_DECL = re.compile(
    r"^(\s*)(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:function\*?\s+[\w$]+|class\s+[\w$]+|abstract\s+class\s+[\w$]+|interface\s+[\w$]+|type\s+[\w$]+\s*[=<]|"
    r"enum\s+[\w$]+|namespace\s+[\w$]+|(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:\(|function|[\w$]+\s*=>|use\w+\(|React\.|memo\(|forwardRef|styled|class\b|\{|\[|new\s|create\w*\())")
JS_METHOD = re.compile(r"^(\s+)(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+|get\s+|set\s+)*"
                       r"(?!if\b|for\b|while\b|switch\b|catch\b|return\b)[\w$]+\s*\([^;]*\)\s*(?::\s*[^={;]+)?\{\s*$")
# Inside a block only functions and hooks are landmarks, not every local value.
JS_FUNCY = re.compile(r"=\s*(?:async\s+)?(?:\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>|function|use\w+\(|memo\(|forwardRef)|"
                      r"^\s*(?:export\s+)?(?:async\s+)?function|^\s*(?:abstract\s+)?class")
JS_EXPORT_ANY = re.compile(r"^(\s*)export\s+(?:default\s+)?\{|^(\s*)export\s+\*")
PY_DECL = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+\w+")
GO_DECL = re.compile(r"^(\s*)(?:func|type)\s+")
RUST_DECL = re.compile(r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod|type|const|static)\b")
CLIKE_DECL = re.compile(r"^(\s*)(?:(?:public|private|protected|internal|static|final|abstract|sealed|override|virtual|"
                        r"async|partial|readonly|export)\s+)*(?:class|interface|struct|enum|record|namespace)\s+\w+|"
                        r"^(\s*)(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async)\s+)+"
                        r"[\w<>\[\],\s]+\s+\w+\s*\([^;]*\)\s*(?:\{|$)")
C_DECL = re.compile(r"^()(?:[A-Za-z_][\w\s\*&:<>,]*\s+\**)?[A-Za-z_][\w:~]*\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$")
PS_DECL = re.compile(r"^(\s*)(?:function|filter|class)\s+[\w-]+", re.I)
SH_DECL = re.compile(r"^(\s*)(?:function\s+)?[\w-]+\s*\(\)\s*\{")
CSS_RULE = re.compile(r"^(\s*)(@media|@supports|@layer|@keyframes|@font-face|[^\s@/*}][^{;]*)\{\s*$")
MD_HEAD = re.compile(r"^(#{1,6})\s+\S")
JSON_KEY = re.compile(r'^(\s*)"([^"]+)"\s*:')

LANG = {
    **dict.fromkeys(("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "vue", "svelte"), "js"),
    "py": "py", "pyw": "py", "go": "go", "rs": "rust",
    **dict.fromkeys(("java", "cs", "kt", "kts", "scala", "swift", "dart"), "clike"),
    **dict.fromkeys(("c", "h", "cc", "cpp", "cxx", "hpp", "hh"), "c"),
    **dict.fromkeys(("ps1", "psm1"), "ps"), **dict.fromkeys(("sh", "bash", "zsh"), "sh"),
    **dict.fromkeys(("css", "scss", "less"), "css"),
    **dict.fromkeys(("md", "markdown", "mdx"), "md"),
    **dict.fromkeys(("json", "jsonc"), "json"),
}


def indent_of(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def entries(lines: list[str], lang: str) -> list[tuple[int, int, str]]:
    """(line number, nesting level, text) for each landmark."""
    out: list[tuple[int, int, str]] = []
    if lang == "md":
        fence = False
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
            elif not fence and (m := MD_HEAD.match(line)):
                out.append((n, len(m.group(1)) - 1, line.strip()))
        return out
    if lang == "json":
        for n, line in enumerate(lines, 1):
            if (m := JSON_KEY.match(line)) and indent_of(line) <= 4:
                out.append((n, indent_of(line) // 2 - 1, line.strip().rstrip(",")))
        return out
    patterns = {"js": [JS_DECL, JS_METHOD, JS_EXPORT_ANY], "py": [PY_DECL], "go": [GO_DECL], "rust": [RUST_DECL],
                "clike": [CLIKE_DECL], "c": [C_DECL], "ps": [PS_DECL], "sh": [SH_DECL], "css": [CSS_RULE]}[lang]
    unit = None
    comment = False
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if lang in ("js", "clike", "c", "css", "rust", "go"):
            if comment:
                comment = "*/" not in s
                continue
            if s.startswith("/*"):
                comment = "*/" not in s
                continue
            if s.startswith("//"):
                continue
        if not s or (lang in ("py", "ps", "sh") and s.startswith("#")):
            continue
        for p in patterns:
            if p.match(line):
                ind = indent_of(line)
                if lang == "js" and ind and p is JS_DECL and not JS_FUNCY.search(line):
                    break
                if ind and unit is None:
                    unit = ind
                out.append((n, ind // (unit or 4) if ind else 0, s))
                break
    return out


def block_end(lines: list[str], start: int, level_indent: int, lang: str) -> int:
    """The last line of the block starting at line `start` (1-based)."""
    if lang == "py":
        end = start
        for n in range(start, len(lines)):
            line = lines[n]
            if line.strip() and indent_of(line) <= level_indent and not line.strip().startswith(("#", ")", "]")):
                break
            if line.strip():
                end = n + 1
        return end
    depth, opened = 0, False
    for n in range(start - 1, len(lines)):
        if not opened and n > start - 1 and lines[n].strip() and indent_of(lines[n]) <= level_indent:
            while n > start and not lines[n - 1].strip():
                n -= 1
            return n  # back at the declaration's level: it had no block
        code = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", lines[n])  # drop string literals
        code = code.split("//")[0] if lang != "css" else code
        for ch in code:
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
        # Brackets that open and close on one line, like a parameter list, aren't the block.
        if depth > 0:
            opened = True
        elif opened:
            return n + 1
    return start


def outline(path: str, depth: int, grep: str | None) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    lang = LANG.get(ext)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        return f"{path}: {e.strerror}"
    if lines and lines[-1] == "":
        lines.pop()
    head = f"{path} ({len(lines):,} lines)"
    if lang is None:
        return f"{head}: no outline for .{ext or '?'} files; use grep -n to find the part you need"
    landmarks = entries(lines, lang)
    found = [e for e in landmarks if e[1] < depth]
    if grep:
        found = [e for e in found if grep.lower() in e[2].lower()]
    rows = [head]
    first = found[0][0] if found else len(lines) + 1
    if lang in ("js", "py") and first > 3 and not grep:
        rows.append(f"  {f'1-{first - 1}':>12}  imports and setup")
    for n, level, text in found:
        if lang == "md":
            nxt = next((m for m, l, _ in landmarks if m > n and l <= level), len(lines) + 1)
            end = nxt - 1
        elif lang == "json":
            end = n
        else:
            end = block_end(lines, n, indent_of(lines[n - 1]), lang)
        span = f"{n}-{end}" if end > n else f"{n}"
        text = text if len(text) <= WIDTH else text[:WIDTH - 1] + "…"
        rows.append(f"  {span:>12}  {'  ' * level}{text}")
    if len(rows) == 1:
        rows.append("  no declarations found; use grep -n to find the part you need")
    return "\n".join(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="+")
    p.add_argument("--depth", type=int, default=2, help="levels of nesting to show (default 2)")
    p.add_argument("--grep", help="only landmarks whose line contains this text")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n\n".join(outline(f, args.depth, args.grep) for f in args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
