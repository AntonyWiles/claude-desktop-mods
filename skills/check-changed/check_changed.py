"""One quick check of the files you changed, in any project.

Formats, lints, typechecks and runs the related tests for the files changed
since the last commit (and new files), all at once, and prints one line per
step and only what failed. A project whose package.json has its own
"check:changed" script runs that instead.

Finds the project's own tools and runs them without a shell or downloads:
  Node    prettier or biome, eslint, the "typecheck" script or tsc,
          vitest related or jest --findRelatedTests
  Python  ruff (format and check) or black, mypy when configured, pytest on
          the changed tests and the tests named after changed modules

  python check_changed.py [--base REF] [--no-tests] [--root DIR] [FILE ...]
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

MAX_LINES = 40
JS = re.compile(r"\.(ts|tsx|js|jsx|cjs|mjs|vue|svelte)$")
PY = re.compile(r"\.py$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ENV = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"}


def run(argv: list[str], cwd: str, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, env=ENV, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, ANSI.sub("", (p.stdout or "") + (p.stderr or "")).replace("\r", "")
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def head(lines: list[str], n: int = MAX_LINES) -> list[str]:
    lines = [l for l in lines if l.strip()]
    return lines if len(lines) <= n else lines[:n] + [f"... {len(lines) - n} more lines"]


def ok(detail: str) -> dict:
    return {"ok": True, "detail": detail, "lines": []}


def fail(detail: str, lines: list[str]) -> dict:
    return {"ok": False, "detail": detail, "lines": head(lines)}


def rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def tail_of(out: str, n: int = 30) -> list[str]:
    return [l for l in out.split("\n") if l.strip()][-n:]


# ------------------------------------------------------------------ files


def git(root: str, *argv: str) -> list[str]:
    code, out = run(["git", *argv], root)
    return [l.strip() for l in out.split("\n") if l.strip()] if code == 0 else []


def changed_files(root: str, base: str) -> list[str]:
    files = set(git(root, "diff", "--name-only", "--diff-filter=d", base))
    files |= set(git(root, "ls-files", "--others", "--exclude-standard"))
    return sorted(f for f in files if os.path.isfile(os.path.join(root, f)))


# ------------------------------------------------------------------ Node


class Node:
    def __init__(self, root: str):
        self.root = root
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            self.pkg = json.load(f)
        self.scripts = self.pkg.get("scripts") or {}
        self.node = shutil.which("node") or "node"

    def bin(self, package: str, name: str | None = None) -> list[str] | None:
        """The local install's own bin script, run with node."""
        meta = os.path.join(self.root, "node_modules", package, "package.json")
        if not os.path.isfile(meta):
            return None
        with open(meta, encoding="utf-8") as f:
            b = json.load(f).get("bin")
        rel = b if isinstance(b, str) else (b or {}).get(name or package.split("/")[-1])
        return [self.node, os.path.join(self.root, "node_modules", package, rel)] if rel else None

    def npm_script(self, script: str, extra: list[str] = ()) -> list[str]:
        npm = shutil.which("npm") or "npm"
        cli = os.path.join(os.path.dirname(shutil.which("node") or ""), "node_modules", "npm", "bin", "npm-cli.js")
        base = [self.node, cli] if os.path.isfile(cli) else [npm]
        return [*base, "run", script, *(["--", *extra] if extra else [])]

    def format(self, files: list[str]) -> dict:
        if (cmd := self.bin("prettier")):
            code, out = run([*cmd, "--write", "--list-different", "--ignore-unknown", *files], self.root)
            changed = [l.strip() for l in out.split("\n") if l.strip() in files]
            if code != 0 and not changed:
                return fail("could not format", tail_of(out))
            return ok(f"prettier reformatted {', '.join(changed)}" if changed else f"prettier: {len(files)} files clean")
        if (cmd := self.bin("@biomejs/biome", "biome")):
            code, out = run([*cmd, "format", "--write", "--no-errors-on-unmatched", *files], self.root)
            return ok("biome formatted") if code == 0 else fail("biome could not format", tail_of(out))
        return None

    def lint(self, files: list[str]) -> dict | None:
        code_files = [f for f in files if JS.search(f)]
        cmd = self.bin("eslint")
        if not cmd or not code_files:
            return None
        code, out = run([*cmd, "--format", "json", "--no-warn-ignored", *code_files], self.root)
        try:
            report = json.loads(out[out.index("["):])
        except ValueError:
            return fail("eslint did not run", tail_of(out))
        lines = [f"{rel(f['filePath'], self.root)}:{m.get('line')}:{m.get('column')} "
                 f"{'error' if m.get('severity') == 2 else 'warning'} {m.get('message')} ({m.get('ruleId')})"
                 for f in report for m in f.get("messages", [])]
        errors = sum(f.get("errorCount", 0) for f in report)
        return ok(f"eslint: {len(code_files)} files") if errors == 0 else fail(f"eslint: {errors} errors", lines)

    def typecheck(self, files: list[str]) -> dict | None:
        if not any(re.search(r"\.(ts|tsx|vue)$", f) for f in files):
            return None
        if "typecheck" in self.scripts:
            cmd = self.npm_script("typecheck")
        elif os.path.isfile(os.path.join(self.root, "tsconfig.json")) and (tsc := self.bin("typescript", "tsc")):
            cmd = [*tsc, "--noEmit"]
        else:
            return None
        code, out = run(cmd, self.root)
        errors = sorted(set(l.strip() for l in out.split("\n") if re.search(r"error TS\d+", l)))
        return ok("typecheck passed") if code == 0 else fail(f"typecheck: {len(errors)} errors", errors or tail_of(out))

    def tests(self, files: list[str]) -> dict | None:
        code_files = [f for f in files if JS.search(f)]
        if not code_files:
            return None
        report = os.path.join(tempfile.gettempdir(), f"check-changed-{os.getpid()}.json")
        if (cmd := self.bin("vitest")):
            argv = [*cmd, "related", *code_files, "--run", "--silent", "--passWithNoTests",
                    "--reporter=json", f"--outputFile={report}"]
        elif (cmd := self.bin("jest")):
            argv = [*cmd, "--findRelatedTests", *code_files, "--passWithNoTests", "--silent",
                    "--json", f"--outputFile={report}"]
        else:
            return None
        code, out = run(argv, self.root)
        try:
            with open(report, encoding="utf-8") as f:
                data = json.load(f)
            os.remove(report)
        except (OSError, ValueError):
            return ok("no related tests") if code == 0 else fail("tests did not run", tail_of(out))
        if not data.get("numTotalTests"):
            return ok("no related tests") if code == 0 else fail("tests did not run", tail_of(out))
        lines = []
        for t in data.get("testResults", []):
            if t.get("status") != "failed":
                continue
            name = rel(t.get("name", "?"), self.root)
            failed = [a for a in t.get("assertionResults", []) if a.get("status") == "failed"]
            if not failed:
                lines.append(f"{name}: {(t.get('message') or 'failed to run').splitlines()[0]}")
            for a in failed:
                lines.append(f"{name} > {a.get('fullName') or a.get('title')}")
                msg = [l for l in ((a.get("failureMessages") or [""])[0]).split("\n")
                       if l.strip() and not l.strip().startswith("at ")]
                lines += [f"    {ANSI.sub('', l)}" for l in msg[:6]]
        summary = f"{data.get('numPassedTests', 0)}/{data['numTotalTests']} tests"
        return ok(summary) if code == 0 and data.get("success", True) else fail(summary, lines)


# ------------------------------------------------------------------ Python


class Python:
    def __init__(self, root: str):
        self.root = root
        venv = [os.path.join(root, d, s) for d in (".venv", "venv") for s in ("Scripts/python.exe", "bin/python")]
        self.py = next((p for p in venv if os.path.isfile(p)), sys.executable)
        self._has: dict[str, bool] = {}
        config = ""
        for name in ("pyproject.toml", "setup.cfg", "mypy.ini", ".mypy.ini"):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    config += f.read()
        self.config = config

    def has(self, module: str) -> bool:
        if module not in self._has:
            self._has[module] = run([self.py, "-c", f"import {module}"], self.root)[0] == 0
        return self._has[module]

    def format(self, files: list[str]) -> dict | None:
        py = [f for f in files if PY.search(f)]
        if not py:
            return None
        if self.has("ruff"):
            code, out = run([self.py, "-m", "ruff", "format", *py], self.root)
            return ok(f"ruff format: {out.strip().splitlines()[-1] if out.strip() else 'done'}") if code == 0 \
                else fail("ruff could not format", tail_of(out))
        if self.has("black"):
            code, out = run([self.py, "-m", "black", "-q", *py], self.root)
            return ok("black formatted") if code == 0 else fail("black could not format", tail_of(out))
        return None

    def lint(self, files: list[str]) -> dict | None:
        py = [f for f in files if PY.search(f)]
        if not py or not self.has("ruff"):
            return None
        code, out = run([self.py, "-m", "ruff", "check", "--output-format", "concise", *py], self.root)
        lines = [l for l in out.split("\n") if re.search(r":\d+:\d+:", l)]
        return ok(f"ruff: {len(py)} files") if code == 0 else fail(f"ruff: {len(lines)} problems", lines or tail_of(out))

    def typecheck(self, files: list[str]) -> dict | None:
        py = [f for f in files if PY.search(f)]
        if not py or "mypy" not in self.config or not self.has("mypy"):
            return None
        code, out = run([self.py, "-m", "mypy", *py], self.root)
        lines = [l for l in out.split("\n") if ": error:" in l]
        return ok("mypy passed") if code == 0 else fail(f"mypy: {len(lines)} errors", lines or tail_of(out))

    def tests(self, files: list[str]) -> dict | None:
        py = [f for f in files if PY.search(f)]
        if not py or not self.has("pytest"):
            return None
        targets = {f for f in py if re.search(r"(^|/)test_[^/]*\.py$|_test\.py$", f)}
        stems = {os.path.splitext(os.path.basename(f))[0] for f in py}
        for dirpath, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__")]
            for n in names:
                if n.startswith("test_") and n.endswith(".py") and n[5:-3] in stems:
                    targets.add(os.path.relpath(os.path.join(dirpath, n), self.root))
        if not targets:
            return ok("no related tests")
        code, out = run([self.py, "-m", "pytest", "-q", "--no-header", "-rf", *sorted(targets)], self.root)
        if code == 5:
            return ok("no related tests")
        last = tail_of(out, 1)
        lines = [l for l in out.split("\n") if l.startswith(("FAILED", "ERROR", "E   "))]
        return ok(last[0] if last else "passed") if code == 0 else fail(last[0] if last else "failed", lines or tail_of(out))


# ------------------------------------------------------------------ main


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="*", help="check these files instead of the changed ones")
    p.add_argument("--base", default="HEAD", help="compare with this commit (default: HEAD)")
    p.add_argument("--no-tests", action="store_true")
    p.add_argument("--root", default=None, help="the project folder (default: the git root here)")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    here = os.path.abspath(args.root or os.getcwd())
    top = git(here, "rev-parse", "--show-toplevel")
    root = os.path.normpath(top[0]) if top and not args.root else here

    node = Node(root) if os.path.isfile(os.path.join(root, "package.json")) else None
    if node and "check:changed" in node.scripts:
        extra = [*(["--base", args.base] if args.base != "HEAD" else []),
                 *(["--no-tests"] if args.no_tests else []), *args.files]
        print("check-changed: running this project's own `npm run check:changed`", flush=True)
        return subprocess.call(node.npm_script("check:changed", extra), cwd=root, env=ENV)

    started = time.time()
    files = ([os.path.relpath(os.path.abspath(f), root).replace("\\", "/") for f in args.files]
             if args.files else changed_files(root, args.base))
    files = [f for f in files if os.path.isfile(os.path.join(root, f))]
    if not files:
        print(f"check-changed: nothing changed since {args.base}")
        return 0
    print(f"check-changed: {len(files)} files {'named' if args.files else f'changed since {args.base}'}")

    kits = [k for k in (node, Python(root)) if k]
    steps: list[tuple[str, dict]] = []
    # Formatting first: it rewrites files the other steps read.
    for k in kits:
        if (r := k.format(files)):
            steps.append(("format", r))
    jobs = [("lint", k.lint) for k in kits] + [("typecheck", k.typecheck) for k in kits]
    if not args.no_tests:
        jobs += [("tests", k.tests) for k in kits]
    with cf.ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
        futures = [(name, pool.submit(fn, files)) for name, fn in jobs]
        steps += [(name, r) for name, f in futures if (r := f.result())]

    if not steps:
        print("check-changed: no format, lint, typecheck or test tool found for these files. "
              "Run the project's own checks, keeping output short.")
        return 0
    for name, r in steps:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'} {name:<10} {r['detail']}")
        for line in r["lines"]:
            print(f"       {line}")
    failed = [name for name, r in steps if not r["ok"]]
    took = round(time.time() - started)
    print(f"check-changed: FAILED ({', '.join(failed)}) in {took}s" if failed
          else f"check-changed: all passed in {took}s{' (tests skipped)' if args.no_tests else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
