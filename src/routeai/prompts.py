"""Prompts sent to worker models, and parsing of their replies."""

from __future__ import annotations

import re
from pathlib import Path

BASE = """You are a focused worker model. A senior engineer delegates small, well-scoped tasks to you and reviews everything you return.
Rules:
- Do exactly what the task asks. Do not add unrequested features, files or commentary.
- Correctness first, then clarity. No chit-chat, no apologies.
- When you produce code or a file, return the COMPLETE content in ONE fenced code block with the right language tag.
- If something is ambiguous, pick the most reasonable option and note it in one short code comment."""

CATEGORY_HINTS = {
    "complex": "Reason about edge cases before writing. Produce well-structured, idiomatic code with type hints where the language supports them.",
    "code": "Write clean, idiomatic code with type hints where the language supports them.",
    "tests": "Write thorough, deterministic unit tests: normal cases, edge cases and error cases. Use the test framework the project already uses if visible; for Python default to pytest.",
    "scripts": "Write a robust self-contained script: argument parsing, clear error messages, non-zero exit code on failure, standard library only unless told otherwise.",
    "build": "You handle build, CI and packaging files and build logs. Be exact with syntax. When diagnosing a log, state the root cause first, then the minimal fix.",
    "docs": "Write clear, accurate documentation. Never describe behaviour that is not in the code.",
    "general": "",
}

FILE_OUTPUT = "Your reply is written verbatim to `{path}`. Return ONLY that file's complete content in a single fenced code block."
JSON_OUTPUT = "Reply with JSON only, matching the requested schema."

LANGS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust", ".cs": "csharp", ".cpp": "cpp",
    ".c": "c", ".h": "c", ".rb": "ruby", ".php": "php", ".sh": "bash", ".ps1": "powershell",
    ".sql": "sql", ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".xml": "xml", ".html": "html", ".css": "css", ".md": "markdown", ".dart": "dart",
    ".groovy": "groovy", ".xsl": "xml", ".xslt": "xml",
}

RAW_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n(.*?)^\1[ \t]*$", re.S | re.M)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def system_prompt(category: str, output_path: str | None = None, json_mode: bool = False) -> str:
    parts = [BASE]
    if hint := CATEGORY_HINTS.get(category):
        parts.append(hint)
    if output_path:
        parts.append(FILE_OUTPUT.format(path=output_path))
    if json_mode:
        parts.append(JSON_OUTPUT)
    return "\n\n".join(parts)


def _fence_for(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", text)), default=2)
    return "`" * max(3, longest + 1)


def build_user_message(instruction: str, files: list[tuple[str, str]], context: str | None = None) -> str:
    parts = [f"## Task\n{instruction.strip()}"]
    if context:
        parts.append(f"## Context\n{context.strip()}")
    for rel, text in files:
        fence = _fence_for(text)
        lang = LANGS.get(Path(rel).suffix.lower(), "")
        parts.append(f"## File: {rel}\n{fence}{lang}\n{text.rstrip()}\n{fence}")
    return "\n\n".join(parts)


def strip_thinking(reply: str) -> str:
    return _THINK_RE.sub("", reply).strip()


def extract_file_content(reply: str, path: str) -> str:
    """Turn a model reply into file content: the largest fenced block, or raw text for prose files."""
    text = strip_thinking(reply)
    blocks = [m.group(2) for m in _FENCE_RE.finditer(text)]
    if not blocks and re.search(r"^\s*(```|~~~)", text, re.M):
        raise ValueError("the answer opens a code block that never closes (probably cut off); nothing was written")
    if Path(path).suffix.lower() in RAW_TEXT_SUFFIXES:
        if len(blocks) == 1 and text.startswith(("```", "~~~")) and text.endswith(("```", "~~~")):
            return blocks[0]
        return text + "\n"
    if blocks:
        return max(blocks, key=len)
    return text + "\n"


def looks_like_echo(content: str, instruction: str) -> bool:
    """A weak model sometimes returns our own user message instead of an answer; never write that to a file."""
    head = content.lstrip()[:800]
    if head.startswith("## Task"):
        return True
    start = instruction.strip()[:80]
    return bool(start) and start in head and "## File:" in content[:4000]


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5))


_CLASSIFY = [
    ("tests", ("unit test", "pytest", "test case", "tests for", "write tests", "junit", "coverage")),
    ("build", ("build", "ci ", "pipeline", "dockerfile", "makefile", "pyproject", "package.json", "gradle", "workflow", "compile error", "log")),
    ("scripts", ("script", "cli", "bash", "powershell", "command line", "automation")),
    ("docs", ("docstring", "document", "readme", "comment", "summar", "explain", "changelog")),
    ("complex", ("algorithm", "refactor", "architecture", "concurren", "optimi", "parser", "complex")),
]


def classify(instruction: str) -> str:
    """Cheap keyword fallback when the caller did not choose a category."""
    text = f" {instruction.lower()} "
    for category, keywords in _CLASSIFY:
        if any(k in text for k in keywords):
            return category
    return "code"
