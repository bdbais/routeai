"""A durable work queue: the fleet keeps going while Claude is paused (usage limit, closed session).

Claude queues self-contained tasks; a worker - inside the running MCP server, or the standalone
`run.py queue work` - claims them one by one and writes the results to disk. Claiming is an atomic
rename, so several workers (and several sessions) never run the same task twice.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .config import fleet_home

STALE_RUNNING_S = 1800  # a claim from a worker that died is offered again after this


def queue_dir() -> Path:
    return fleet_home() / "queue"


def done_dir() -> Path:
    return queue_dir() / "done"


def enqueue(spec: dict, root: str, note: str | None = None) -> dict:
    item = {"id": uuid.uuid4().hex[:8], "queued_at": time.time(), "root": root, "spec": spec, "note": note}
    queue_dir().mkdir(parents=True, exist_ok=True)
    path = queue_dir() / f"{int(item['queued_at'])}-{item['id']}.json"
    path.write_text(json.dumps(item, indent=1, default=str), encoding="utf-8")
    return item


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def requeue_stale(max_age_s: float = STALE_RUNNING_S) -> int:
    """Give back tasks claimed by a worker that never finished (killed session, crash)."""
    count = 0
    for path in queue_dir().glob("*.running"):
        try:
            if time.time() - path.stat().st_mtime < max_age_s:
                continue
            path.rename(path.with_suffix(".json"))
            count += 1
        except OSError:
            continue
    return count


def claim() -> tuple[Path, dict] | None:
    """Take the oldest queued task, or None. The rename is the lock."""
    for path in sorted(queue_dir().glob("*.json")):
        running = path.with_suffix(".running")
        try:
            path.rename(running)
        except OSError:  # another worker was faster
            continue
        item = _read(running)
        if item is None:
            done_dir().mkdir(parents=True, exist_ok=True)
            running.replace(done_dir() / f"{running.stem}.broken.json")
            continue
        return running, item
    return None


def complete(running: Path, result: dict) -> Path:
    item = _read(running) or {"id": running.stem}
    item.update(finished_at=time.time(), result=result)
    done_dir().mkdir(parents=True, exist_ok=True)
    out = done_dir() / f"{running.stem}.json"
    out.write_text(json.dumps(item, indent=1, default=str), encoding="utf-8")
    running.unlink(missing_ok=True)
    return out


def cancel(item_id: str) -> bool:
    for path in queue_dir().glob(f"*{item_id}*.json"):
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def clear(what: str = "done") -> int:
    patterns = {"done": [done_dir().glob("*.json")], "pending": [queue_dir().glob("*.json")],
                "all": [queue_dir().glob("*.json"), queue_dir().glob("*.running"), done_dir().glob("*.json")]}
    count = 0
    for group in patterns.get(what, []):
        for path in group:
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
    return count


def _row(item: dict, path: Path) -> dict:
    spec = item.get("spec", {})
    row = {"id": item.get("id", path.stem), "category": spec.get("category"),
           "instruction": (spec.get("instruction") or "")[:120], "output_path": spec.get("output_path"),
           "queued_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("queued_at", 0)))}
    if item.get("note"):
        row["note"] = item["note"]
    result = item.get("result")
    if result:
        row["result"] = {k: result.get(k) for k in ("ok", "node", "model", "seconds", "written", "error", "task_id")
                         if result.get(k) is not None}
    return row


def snapshot(limit: int = 20) -> dict:
    requeue_stale()
    pending = [(_read(p) or {}, p) for p in sorted(queue_dir().glob("*.json"))]
    running = [(_read(p) or {}, p) for p in sorted(queue_dir().glob("*.running"))]
    done = sorted(done_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if done_dir().exists() else []
    finished = [(_read(p) or {}, p) for p in done[:limit]]
    return {
        "pending": len(pending), "running": len(running), "done": len(done),
        "queued": [_row(i, p) for i, p in pending[:limit]],
        "in_progress": [_row(i, p) for i, p in running],
        "finished": [_row(i, p) for i, p in finished],
        "folder": str(queue_dir()),
    }
