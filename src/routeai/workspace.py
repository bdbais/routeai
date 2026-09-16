"""Safe file access: the server reads project files itself so Claude never has to paste them."""

from __future__ import annotations

import os
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode", ".gradle",
}
# Worker output lands on disk before Claude reviews it: never inside places where a file can run code
# (git hooks, editor tasks) or change Claude Code itself (.claude/settings.json hooks).
PROTECTED_WRITE_DIRS = SKIP_DIRS | {".claude"}
# Globs never pick up files that look like secrets; they can still be listed explicitly by path.
SECRET_NAME = re.compile(r"^\.env|\.(pem|key|p12|pfx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)|credential|secret"
                         r"|^\.(npmrc|pypirc|netrc|git-credentials)$", re.I)
GLOB_CHARS = set("*?[")


class WorkspaceError(ValueError):
    pass


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class Workspace:
    def __init__(self, roots: list[str | Path]):
        if not roots:
            raise WorkspaceError("at least one workspace root is required")
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        given = [_norm(str(Path(r).expanduser())) for r in roots]
        self._lexical_roots = sorted(set(given + [_norm(str(r)) for r in self.roots]))

    @property
    def root(self) -> Path:
        return self.roots[0]

    def _outside(self, path) -> WorkspaceError:
        return WorkspaceError(f"{path} is outside the allowed roots ({', '.join(map(str, self.roots))})")

    def resolve(self, path: str | Path) -> Path:
        raw = str(path)
        if raw.startswith(("\\\\", "//")):
            raise WorkspaceError(f"network paths are not allowed: {path}")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.root / p
        # Check the text first: on Windows, resolving a foreign path (a UNC share, another drive) already
        # touches it, which can send the user's credentials to another machine.
        lexical = _norm(str(p))
        if not any(lexical == r or lexical.startswith(r.rstrip("\\/") + os.sep) for r in self._lexical_roots):
            raise self._outside(path)
        p = p.resolve()
        if not any(p == r or r in p.parents for r in self.roots):  # a symlink pointing outside
            raise self._outside(path)
        return p

    def rel(self, path: Path) -> str:
        for r in self.roots:
            if path == r or r in path.parents:
                return path.relative_to(r).as_posix()
        return path.as_posix()

    def expand(self, patterns: list[str], limit: int = 500) -> list[Path]:
        """Resolve file paths and relative glob patterns (e.g. ``src/**/*.py``), in order, deduplicated."""
        out: dict[Path, None] = {}
        for pat in patterns:
            if GLOB_CHARS & set(pat):
                # Path.anchor also catches "/src/*" on Windows, which is not absolute but is rooted.
                if Path(pat).anchor or ".." in Path(pat).parts:
                    raise WorkspaceError(f"glob patterns must be relative to the project root, without '..': {pat}")
                for match in self._glob(pat):
                    out.setdefault(match, None)
                    if len(out) > limit:
                        break
            else:
                p = self.resolve(pat)
                if not p.is_file():
                    raise WorkspaceError(f"file not found: {pat}")
                out.setdefault(p, None)
            if len(out) > limit:
                raise WorkspaceError(f"more than {limit} files matched; narrow the patterns")
        return list(out)

    def _glob(self, pattern: str):
        named = set(Path(pattern).parts)  # hidden directories the pattern names explicitly are fine
        for match in sorted(self.root.glob(pattern)):
            parts = match.relative_to(self.root).parts
            if SKIP_DIRS & set(parts):
                continue
            if any(part.startswith(".") and part not in named for part in parts[:-1]):
                continue
            if match.name.startswith(".") or SECRET_NAME.search(match.name):
                continue
            if match.is_file():
                yield self.resolve(match)

    def read(self, path: Path, max_bytes: int) -> str:
        try:
            size = path.stat().st_size
            if size > max_bytes:
                raise WorkspaceError(f"{self.rel(path)} is {size} bytes (limit {max_bytes}); split the task")
            data = path.read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"cannot read {self.rel(path)}: {exc}") from None
        if b"\x00" in data[:8192]:
            raise WorkspaceError(f"{self.rel(path)} looks binary")
        return data.decode("utf-8", errors="replace")

    def check_writable(self, path: str | Path) -> Path:
        p = self.resolve(path)
        parts = Path(self.rel(p)).parts
        blocked = PROTECTED_WRITE_DIRS & set(parts[:-1])
        if blocked:
            raise WorkspaceError(f"refusing to write {self.rel(p)}: files inside {sorted(blocked)[0]}/ can run code "
                                 "or change tool settings")
        return p

    def write(self, path: str | Path, text: str, overwrite: bool = True) -> Path:
        p = self.check_writable(path)
        if p.exists() and not overwrite:
            raise WorkspaceError(f"{self.rel(p)} exists and overwrite is false")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="")
        except OSError as exc:
            raise WorkspaceError(f"cannot write {self.rel(p)}: {exc}") from None
        return p
