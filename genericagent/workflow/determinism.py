"""Static determinism gate for workflow scripts.

Port of Claude Code's ``LKt()`` (see the research doc
``claudecode283_hack/docs/20261012-复刻dynamic-workflow指南.md`` §3.3): a
workflow script that calls ``Date.now()``, ``Math.random()`` or ``new Date()``
with no arguments is rejected, because resume/cache relies on
"same script + same args -> same agent() call sequence".

Claude Code walks the acorn AST. GenericAgent has no JS-parser dependency, so
this scans a copy of the script with comments and string/template literals
removed. Like the original it does **not** catch ``Date["now"]``, aliases, or
``globalThis.Date.now`` -- it stops the model from casually writing
``Date.now()``; it is not a sandbox.
"""

from __future__ import annotations

import re

DETERMINISM_ERROR_MESSAGE = (
    "Workflow scripts must be deterministic: Date.now()/Math.random()/new Date() are unavailable "
    "(breaks resume). Stamp results after the workflow returns, or pass timestamps via args."
)

# The lookbehind keeps the match to a *bare* identifier, mirroring Claude Code's
# AST rule (object must be an Identifier named Date/Math, not a member access),
# so ``globalThis.Date.now()`` / ``obj.Date.now()`` are not flagged.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Date.now()", re.compile(r"(?<![\w$.])Date\s*\.\s*now\b")),
    ("Math.random()", re.compile(r"(?<![\w$.])Math\s*\.\s*random\b")),
    ("new Date()", re.compile(r"(?<![\w$.])new\s+Date\s*\(\s*\)|(?<![\w$.])new\s+Date\b(?!\s*\()")),
)


def find_nondeterminism(script: str) -> list[str]:
    """Return the non-deterministic constructs found in ``script`` (empty if none)."""

    code = _strip_comments_and_strings(str(script or ""))
    return [label for label, pattern in _RULES if pattern.search(code)]


def has_nondeterminism(script: str) -> bool:
    return bool(find_nondeterminism(script))


def determinism_error(script: str) -> str | None:
    """Return the Claude-Code-style rejection message, or ``None`` if the script is fine."""

    return DETERMINISM_ERROR_MESSAGE if has_nondeterminism(script) else None


def _strip_comments_and_strings(source: str) -> str:
    """Blank out JS comments and string/template literals, keeping code and ``${...}`` bodies."""

    out: list[str] = []
    stack: list[tuple[str, int]] = []
    i = 0
    n = len(source)

    def blank(text: str) -> None:
        out.append("\n" if text == "\n" else " ")

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if stack and stack[-1][0] == "template":
            if ch == "\\":
                blank(ch)
                i += 1
                if i < n:
                    blank(source[i])
                    i += 1
                continue
            if ch == "`":
                blank(ch)
                stack.pop()
                i += 1
                continue
            if ch == "$" and nxt == "{":
                blank(ch)
                i += 1
                blank(source[i])
                i += 1
                stack.append(("interp", 0))
                continue
            blank(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                blank(source[i])
                i += 1
            continue
        if ch == "/" and nxt == "*":
            blank(ch)
            i += 1
            blank(source[i])
            i += 1
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                blank(source[i])
                i += 1
            if i < n:
                blank(source[i])
                i += 1
                blank(source[i])
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            blank(ch)
            i += 1
            while i < n:
                if source[i] == "\\":
                    blank(source[i])
                    i += 1
                    if i < n:
                        blank(source[i])
                        i += 1
                    continue
                if source[i] == quote:
                    blank(source[i])
                    i += 1
                    break
                blank(source[i])
                i += 1
            continue
        if ch == "`":
            blank(ch)
            i += 1
            stack.append(("template", 0))
            continue

        if stack and stack[-1][0] == "interp":
            _, depth = stack[-1]
            if ch == "{":
                stack[-1] = ("interp", depth + 1)
            elif ch == "}":
                if depth == 0:
                    blank(ch)
                    i += 1
                    stack.pop()
                    continue
                stack[-1] = ("interp", depth - 1)

        out.append(ch)
        i += 1

    return "".join(out)
