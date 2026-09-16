"""Learned performance store: how fast each node/model is and how good it is per category.

Written by the benchmark and by every real task, read by the router. Quality
scores come from graded bench tasks and from Claude's feedback on real work.

Several processes share the file (one MCP server per Claude Code session, plus
the CLI), so every update re-reads it under a lock file, a failed read never
overwrites what was learned, and errors are reported instead of raised.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ALPHA = 0.3  # EWMA weight of the newest sample
HISTORY = 10
LOCK_TIMEOUT_S = 5.0
LOCK_STALE_S = 30.0


def _ewma(old: float | None, new: float) -> float:
    return new if old is None else old + ALPHA * (new - old)


def _empty() -> dict:
    return {
        "speed": {},
        "quality": {},
        "nodes": {},
        "totals": {"tasks": 0, "tokens_in": 0, "tokens_out": 0, "tokens_returned": 0,
                   "saved_input": 0, "saved_output": 0},
        "last_bench": None,
    }


def _warn(message: str) -> None:
    print(f"routeai: {message}", file=sys.stderr, flush=True)


class Stats:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._mtime = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return self._read()[0]

    def _read(self, force: bool = False) -> tuple[dict, bool]:
        """Current data, and whether it reflects the file (False after a read error: never save that)."""
        fallback = self._cache if self._cache is not None else _empty()
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return fallback, True
        except OSError:
            return fallback, False
        if not force and self._cache is not None and mtime == self._mtime:
            return self._cache, True
        for attempt in range(5):  # antivirus or another writer can hold the file for a moment
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("stats.json is not an object")
                self._cache, self._mtime = {**_empty(), **data}, mtime
                return self._cache, True
            except (OSError, ValueError):
                time.sleep(0.05 * (attempt + 1))
        return fallback, False

    def _acquire(self) -> Path | None:
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.time() + LOCK_TIMEOUT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return lock
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
            except OSError:
                return None
            if time.time() > deadline:
                return None
            time.sleep(0.02)

    def _save(self, data: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:  # Windows: the target is open in another process
                if attempt == 9:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05)
        self._cache = data
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def _update(self, fn) -> None:
        with self._lock:
            lock = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock = self._acquire()
                data, ok = self._read(force=True)
                if not ok:
                    _warn(f"{self.path} could not be read; update skipped so learned data is not overwritten")
                    return
                fn(data)
                self._save(data)
            except OSError as exc:
                _warn(f"could not save {self.path}: {exc}")
            finally:
                if lock is not None:
                    lock.unlink(missing_ok=True)

    # -- writers ---------------------------------------------------------

    def record_speed(self, node: str, model: str, *, gen_tps: float, prompt_tps: float,
                     load_s: float, gpu_ratio: float | None = None) -> None:
        def fn(data):
            e = data["speed"].setdefault(f"{node}|{model}", {"n": 0, "fails": 0})
            if gen_tps > 0:
                e["n"] += 1
                e["gen_tps"] = _ewma(e.get("gen_tps"), gen_tps)
            if prompt_tps > 0:
                e["prompt_tps"] = _ewma(e.get("prompt_tps"), prompt_tps)
            if load_s > 1.0:  # sub-second means the model was already loaded
                e["load_s"] = _ewma(e.get("load_s"), load_s)
            if gpu_ratio is not None:
                e["gpu_ratio"] = gpu_ratio
            e["last"] = time.time()
        self._update(fn)

    def record_failure(self, node: str, model: str) -> None:
        def fn(data):
            e = data["speed"].setdefault(f"{node}|{model}", {"n": 0, "fails": 0})
            e["fails"] = e.get("fails", 0) + 1
            e["last_fail"] = time.time()
        self._update(fn)

    def record_quality(self, node: str, model: str, category: str, score: float, source: str) -> None:
        score = max(0.0, min(1.0, score))

        def fn(data):
            e = data["quality"].setdefault(f"{node}|{model}|{category}", {"n": 0, "history": []})
            e["n"] += 1
            e["q"] = _ewma(e.get("q"), score)
            e["history"] = (e.get("history", []) + [round(score, 3)])[-HISTORY:]
            e[f"n_{source}"] = e.get(f"n_{source}", 0) + 1
            e["last"] = time.time()
        self._update(fn)

    def record_node(self, node: str, **profile) -> None:
        self._update(lambda data: data["nodes"].setdefault(node, {}).update(profile))

    def add_totals(self, tokens_in: int, tokens_out: int, tokens_returned: int,
                   saved_input: int = 0, saved_output: int = 0) -> None:
        def fn(data):
            t = data["totals"]
            for key, value in (("tasks", 1), ("tokens_in", tokens_in), ("tokens_out", tokens_out),
                               ("tokens_returned", tokens_returned), ("saved_input", saved_input),
                               ("saved_output", saved_output)):
                t[key] = t.get(key, 0) + value
        self._update(fn)

    def add_savings(self, saved_input: int, saved_output: int) -> None:
        """Adjust savings without counting a task (a batch brief written once, polls read by Claude)."""
        def fn(data):
            t = data["totals"]
            t["saved_input"] = t.get("saved_input", 0) + saved_input
            t["saved_output"] = t.get("saved_output", 0) + saved_output
        self._update(fn)

    def mark_bench(self) -> None:
        self._update(lambda data: data.__setitem__("last_bench", time.time()))

    # -- readers ---------------------------------------------------------

    @staticmethod
    def speed_of(snapshot: dict, node: str, model: str) -> dict:
        return snapshot["speed"].get(f"{node}|{model}", {})

    @staticmethod
    def quality_of(snapshot: dict, node: str, model: str, category: str) -> tuple[float | None, int]:
        e = snapshot["quality"].get(f"{node}|{model}|{category}")
        return (e.get("q"), e.get("n", 0)) if e else (None, 0)

    @staticmethod
    def is_stable(entry: dict) -> bool:
        """Enough samples and the last five agree: the fleet has learned this pairing."""
        hist = entry.get("history", [])
        return entry.get("n", 0) >= 5 and max(hist[-5:]) - min(hist[-5:]) <= 0.2
