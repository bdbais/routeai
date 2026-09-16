"""Task execution: single delegations, parallel batches and background jobs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath

from .config import FleetConfig, fleet_home
from .fleet import Fleet
from .ollama_client import OllamaError
from .prompts import (
    build_user_message, classify, estimate_tokens, extract_file_content, looks_like_echo, strip_thinking,
    system_prompt,
)
from .stats import Stats
from .workspace import Workspace, WorkspaceError

MAX_INLINE_OUTPUT = 30_000
FEEDBACK_SCORES = {"good": 1.0, "fixed": 0.5, "rejected": 0.0}
RESULT_OVERHEAD_TOKENS = 150  # JSON envelope of a tool result that Claude reads anyway
SAVINGS_BLOCK_TOKENS = 60     # the tokens_saved block itself
TOOL_CALL_TOKENS = 40         # envelope of the tool call Claude writes


@dataclass
class TaskSpec:
    instruction: str
    category: str = "auto"
    files: list[str] = field(default_factory=list)
    context: str | None = None
    output_path: str | None = None
    json_schema: dict | None = None
    node: str | None = None
    model: str | None = None
    max_output_tokens: int | None = None
    overwrite: bool = True
    in_batch: bool = False  # the brief and the summary are paid once per batch, not per file


def brief_tokens(spec: TaskSpec, files: list[str] | None = None) -> int:
    """Tokens Claude spends writing the delegation call: output tokens it would not spend otherwise."""
    parts = [spec.instruction, spec.context or "", spec.output_path or "",
             " ".join(spec.files if files is None else files)]
    if spec.json_schema:
        parts.append(json.dumps(spec.json_schema))
    return estimate_tokens(" ".join(parts)) + TOOL_CALL_TOKENS


@dataclass
class TaskResult:
    task_id: str
    ok: bool
    category: str
    node: str | None = None
    model: str | None = None
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    returned_tokens: int = 0
    saved: dict | None = None
    output: str | None = None
    json: object = None
    written: str | None = None
    error: str | None = None
    attempts: list[dict] = field(default_factory=list)
    routing: list[dict] = field(default_factory=list)

    def brief(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in (None, [], "")}
        d.pop("output", None)
        d.pop("routing", None)
        return d


@dataclass
class Job:
    id: str
    kind: str
    total: int
    started: float = field(default_factory=time.time)
    finished: float | None = None
    state: str = "running"
    done: int = 0
    failed: int = 0
    results: list[TaskResult] = field(default_factory=list)
    report: dict | None = None
    error: str | None = None
    progress: str = ""
    overhead_tokens: int = 0  # batch brief, and every poll of the batch, paid by Claude
    task: asyncio.Task | None = None

    def summary(self, include_outputs: bool = False, preview_chars: int = 400) -> dict:
        out = {
            "job_id": self.id, "kind": self.kind, "state": self.state, "done": self.done,
            "failed": self.failed, "total": self.total, "progress": self.progress,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
        }
        if self.error:
            out["error"] = self.error
        if self.report is not None:
            out["report"] = self.report
        if self.kind == "task":
            # A long single task comes back here instead of from fleet_delegate: give the whole answer.
            rows = [self._row(r, preview_chars if r.written else MAX_INLINE_OUTPUT) for r in self.results]
        elif include_outputs:
            rows = [self._row(r, preview_chars) for r in self.results]
        else:
            rows = [self._row(r, 0) for r in self.results if not r.ok]  # every poll is paid: failures only
        if rows:
            out["results"] = rows
        return out

    @staticmethod
    def _row(r: TaskResult, output_chars: int) -> dict:
        row = r.brief()
        if output_chars and r.output:
            row["output"] = r.output[:output_chars]
        return row


def render_output_path(pattern: str, rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    parent = p.parent.as_posix()
    try:
        return pattern.format(path=p.as_posix(), dir="" if parent == "." else parent,
                              stem=p.stem, name=p.name, ext=p.suffix).lstrip("/")
    except (KeyError, IndexError, ValueError):
        raise WorkspaceError(f"bad output_pattern {pattern!r}: use {{path}} {{dir}} {{stem}} {{name}} {{ext}}") from None


class Engine:
    def __init__(self, cfg: FleetConfig, fleet: Fleet, stats: Stats, workspace: Workspace):
        self.cfg = cfg
        self.fleet = fleet
        self.stats = stats
        self.ws = workspace
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, tuple[str, str, str]] = {}  # task_id -> (node, model, category)
        self.log_path = fleet_home() / "tasks.jsonl"
        self.session_saved = 0  # this server process = one Claude Code session
        self.session_tasks = 0

    # -- single task -----------------------------------------------------------

    async def run_task(self, spec: TaskSpec) -> TaskResult:
        category = spec.category if spec.category in self.cfg.categories else classify(spec.instruction)
        result = TaskResult(task_id=uuid.uuid4().hex[:10], ok=False, category=category)
        started = time.time()
        try:
            await self._run(spec, result)
        except Exception as exc:  # one task must never take down a batch or the server
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            result.ok, result.error = False, f"{type(exc).__name__}: {exc}"
        result.seconds = round(time.time() - started, 1)
        self._remember(result, spec)
        return result

    def _load_files(self, spec: TaskSpec) -> list[tuple[str, str]]:
        files = [(self.ws.rel(p), self.ws.read(p, self.cfg.max_file_bytes)) for p in self.ws.expand(spec.files)]
        if spec.output_path:
            self.ws.check_writable(spec.output_path)  # fail fast, before any model runs
        return files

    async def _run(self, spec: TaskSpec, result: TaskResult) -> None:
        category = result.category
        await self.fleet.refresh()
        try:
            files = await asyncio.to_thread(self._load_files, spec)  # globs can walk big trees
        except WorkspaceError as exc:
            result.error = str(exc)
            return

        cat = self.cfg.categories[category]
        files_tokens = sum(estimate_tokens(text) for _, text in files)
        system = system_prompt(category, spec.output_path, spec.json_schema is not None)
        user = build_user_message(spec.instruction, files, spec.context)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        est_in = estimate_tokens(system + user)
        max_out = spec.max_output_tokens or cat.max_output_tokens

        tried: set[tuple[str, str]] = set()
        for _ in range(3):
            ranked = self.fleet.rank(category, est_in, max_out, exclude=tried, node=spec.node,
                                     model=spec.model, has_files=bool(files))
            if not ranked:
                break
            if not result.routing:
                result.routing = [c.brief() for c in ranked[:4]]
            cand = ranked[0]
            tried.add((cand.node, cand.model))
            try:
                async with self.fleet.slot(cand.node):
                    reply = await self.fleet.chat(cand, messages, cat, max_tokens=max_out, fmt=spec.json_schema)
            except OllamaError as exc:
                result.attempts.append({"node": cand.node, "model": cand.model, "error": str(exc)[:300]})
                continue

            result.node, result.model = cand.node, cand.model
            result.tokens_in, result.tokens_out = reply.prompt_tokens, reply.output_tokens
            if reply.done_reason == "length":
                result.error = (f"the answer hit max_output_tokens ({max_out}) and was cut off, so nothing was "
                                "written; raise max_output_tokens or split the task")
                break
            text = strip_thinking(reply.content)
            if spec.json_schema is not None:
                try:
                    result.json = json.loads(text)
                except json.JSONDecodeError:
                    result.attempts.append({"node": cand.node, "model": cand.model, "error": "invalid JSON"})
                    continue
            if spec.output_path:
                try:
                    content = extract_file_content(text, spec.output_path)
                    if looks_like_echo(content, spec.instruction):
                        raise ValueError("the model echoed the prompt instead of answering (model too weak for "
                                         "this task, or the brief was too long); nothing was written")
                    path = await asyncio.to_thread(self.ws.write, spec.output_path, content, spec.overwrite)
                except (WorkspaceError, ValueError) as exc:
                    result.error = str(exc)
                    break
                result.written = self.ws.rel(path)
                result.output = text[: self.cfg.preview_chars]
            else:
                result.output = text[:MAX_INLINE_OUTPUT]
            # Without the fleet Claude would read the files and write the answer. With it, Claude writes the
            # brief and reads the tool result instead (in a batch: one summary row per file).
            payload = {**result.brief(), "output": None if spec.in_batch else result.output}
            result.returned_tokens = estimate_tokens(json.dumps(payload, default=str)) + SAVINGS_BLOCK_TOKENS
            saved_in = files_tokens - result.returned_tokens
            saved_out = result.tokens_out - (0 if spec.in_batch else brief_tokens(spec))
            result.saved = {"claude_input_tokens": saved_in, "claude_output_tokens": saved_out,
                            "total": saved_in + saved_out}
            result.ok = True
            break

        if not result.ok and not result.error:
            if result.attempts:
                result.error = "all candidate nodes failed"
            else:
                result.error = self._no_candidate_hint(category, est_in + max_out, spec)

    def _no_candidate_hint(self, category: str, needed_ctx: int, spec: TaskSpec) -> str:
        healthy = [n for n in self.cfg.enabled_nodes if self.fleet.state[n.name].healthy]
        if not healthy:
            return "no healthy Ollama node (check that the servers are running and reachable)"
        biggest = max(n.num_ctx for n in healthy)
        if needed_ctx > biggest:
            return (f"input too large: needs ~{needed_ctx} tokens of context, the largest node allows {biggest}. "
                    "Split the task into smaller files or raise num_ctx for a node.")
        offsite = [n.name for n in self.cfg.enabled_nodes if n.is_remote and not n.send_files]
        if offsite and any(n.is_remote for n in healthy):
            return (f"no node has a usable model for category '{category}'; the providers {offsite} are not "
                    "allowed to receive this project's files (set send_files = true for them, or use a local node)")
        return (f"no node has a usable model for category '{category}'"
                + (f" matching node={spec.node!r} model={spec.model!r}" if spec.node or spec.model else "")
                + " (install one, enable auto_pull, or add it to fleet.toml)")

    def _remember(self, result: TaskResult, spec: TaskSpec) -> None:
        if result.node and result.model:
            self._tasks[result.task_id] = (result.node, result.model, result.category)
        if result.saved:
            self.session_saved += result.saved["total"]
            self.session_tasks += 1
            self.stats.add_totals(result.tokens_in, result.tokens_out, result.returned_tokens,
                                  result.saved["claude_input_tokens"], result.saved["claude_output_tokens"])
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "project": str(self.ws.root), **result.brief(),
                    "instruction": spec.instruction[:200],
                }, default=str) + "\n")
        except OSError:
            pass

    def savings_report(self, this_task: dict | None = None) -> dict:
        """Claude tokens avoided: what the workers read and wrote, minus what came back to Claude (estimate)."""
        return {
            "this_task": this_task,
            "session_total": self.session_saved,
            "session_tasks": self.session_tasks,
            "all_time_total": describe_totals(self.stats)["estimated_claude_tokens_avoided"],
        }

    def job_savings(self, job: Job) -> dict | None:
        done = [r.saved for r in job.results if r.saved]
        if not done:
            return None
        if len(done) == 1 and not job.overhead_tokens:
            return self.savings_report(done[0])
        return self.savings_report({"total": sum(s["total"] for s in done) - job.overhead_tokens,
                                    "tasks": len(done)})

    def charge_poll(self, job: Job, input_tokens: int, output_tokens: int) -> None:
        """Every fleet_job poll of a batch is read (and written) by Claude: count it against the savings."""
        job.overhead_tokens += input_tokens + output_tokens
        self.session_saved -= input_tokens + output_tokens
        self.stats.add_savings(-input_tokens, -output_tokens)

    def feedback(self, task_id: str, verdict: str, note: str | None = None) -> dict:
        if verdict not in FEEDBACK_SCORES:
            raise ValueError(f"verdict must be one of {list(FEEDBACK_SCORES)}")
        target = self._tasks.get(task_id) or self._find_logged(task_id)
        if not target:
            raise ValueError(f"unknown task_id {task_id}")
        node, model, category = target
        self.stats.record_quality(node, model, category, FEEDBACK_SCORES[verdict], source="feedback")
        q, n = Stats.quality_of(self.stats.snapshot(), node, model, category)
        return {"recorded": True, "node": node, "model": model, "category": category,
                "quality_now": round(q or 0, 3), "samples": n, "note": note}

    def _find_logged(self, task_id: str) -> tuple[str, str, str] | None:
        try:
            with self.log_path.open(encoding="utf-8") as fh:
                for line in fh:
                    if task_id in line:
                        row = json.loads(line)
                        if row.get("task_id") == task_id and row.get("node"):
                            return row["node"], row["model"], row["category"]
        except (OSError, json.JSONDecodeError):
            pass
        return None

    # -- jobs ------------------------------------------------------------------

    def start_job(self, kind: str, total: int, coro_fn) -> Job:
        job = Job(id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, total=total)

        async def runner():
            try:
                await coro_fn(job)
                job.state = "done" if not job.failed else "done_with_errors"
            except asyncio.CancelledError:
                job.state = "cancelled"
                raise
            except Exception as exc:  # surfaced through fleet_job
                job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                job.finished = time.time()

        job.task = asyncio.ensure_future(runner())
        self.jobs[job.id] = job
        return job

    async def delegate(self, spec: TaskSpec, wait_s: float | None = None) -> dict:
        """Run one task; if it takes longer than wait_s, hand back a job id to poll instead."""
        async def body(job: Job):
            res = await self.run_task(spec)
            job.results.append(res)
            job.done, job.failed = (1, 0) if res.ok else (0, 1)

        job = self.start_job("task", 1, body)
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=wait_s or self.cfg.sync_wait_s)
        except asyncio.TimeoutError:
            return {"status": "running", "job_id": job.id,
                    "hint": "still generating; poll fleet_job with this job_id (the full answer comes back there)"}
        if not job.results:
            return {"status": "failed", "error": job.error}
        res = job.results[0]
        out = {k: v for k, v in asdict(res).items() if v not in (None, [], "")}
        out["status"] = "ok" if res.ok else "failed"
        out.pop("saved", None)
        if res.ok:
            out.pop("routing", None)  # only useful to debug a failure; every token here is paid by Claude
        if res.saved:
            out["tokens_saved"] = self.savings_report(res.saved)
        return out

    def expand_batch(self, files: list[str], shared_files: list[str]) -> tuple[list[Path], list[Path]]:
        """Blocking (walks the tree): call it off the event loop."""
        return self.ws.expand(files), (self.ws.expand(shared_files) if shared_files else [])

    def start_batch(self, spec: TaskSpec, paths: list[Path], output_pattern: str | None,
                    shared: list[Path], brief_files: list[str]) -> Job:
        if output_pattern:  # a bad pattern or a protected target fails now, not halfway through the batch
            for p in paths:
                self.ws.check_writable(render_output_path(output_pattern, self.ws.rel(p)))
        workers = max(1, sum(n.max_parallel for n in self.cfg.enabled_nodes))
        overhead_out = brief_tokens(replace(spec, output_path=output_pattern), brief_files)
        overhead_in = RESULT_OVERHEAD_TOKENS
        shared_abs = [str(p) for p in shared]

        async def body(job: Job):
            queue = list(paths)

            async def worker():
                while queue:
                    p = queue.pop(0)
                    rel = self.ws.rel(p)
                    sub = replace(
                        spec,
                        in_batch=True,
                        files=shared_abs + [str(p)],  # absolute: works for files under allowed_roots too
                        output_path=render_output_path(output_pattern, rel) if output_pattern else None,
                        context=((spec.context or "") + f"\n\nApply the task to the file `{rel}`.").strip(),
                    )
                    res = await self.run_task(sub)
                    job.results.append(res)
                    if res.ok:
                        job.done += 1
                    else:
                        job.failed += 1
                    job.progress = f"{job.done + job.failed}/{job.total}"

            await asyncio.gather(*(worker() for _ in range(min(workers, len(paths)) or 1)))
            job.overhead_tokens += overhead_out + overhead_in
            self.session_saved -= overhead_out + overhead_in
            self.stats.add_savings(-overhead_in, -overhead_out)

        return self.start_job("batch", len(paths), body)


async def queue_worker(engine_for: "callable", poll_s: float = 5.0, once: bool = False) -> int:
    """Run queued tasks one by one, forever (or once). `engine_for(root)` returns an Engine for that project."""
    from . import queue as work_queue

    handled = 0
    while True:
        claimed = work_queue.claim()
        if claimed is None:
            if once:
                return handled
            await asyncio.sleep(poll_s)
            continue
        running, item = claimed
        try:
            engine = engine_for(item.get("root"))
            spec = TaskSpec(**item.get("spec", {}))
            result = await engine.run_task(spec)
            payload = result.brief()
            if result.output and not result.written:
                payload["output"] = result.output[:MAX_INLINE_OUTPUT]
        except Exception as exc:  # a broken queue entry must not stop the worker
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        work_queue.complete(running, payload)
        handled += 1


def usage_log(log_path: Path, project: str | None = None, days: float | None = None) -> dict:
    """Read tasks.jsonl: Ollama tokens each machine processed and what that saved Claude, per project or for all."""
    cutoff = time.time() - days * 86400 if days else 0
    totals = {"tasks": 0, "ok": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0,
              "tokens_total": 0, "model_seconds": 0.0,
              "claude_tokens_saved": 0, "claude_input_saved": 0, "claude_output_saved": 0}
    by_node: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    negative = untagged = 0
    first = last = None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ts", 0) < cutoff:
            continue
        if project is not None:
            if not row.get("project"):
                untagged += 1
                continue
            if os.path.normcase(row["project"]) != os.path.normcase(project):
                continue
        saved = row.get("saved") or {}
        total = saved.get("total", 0)
        tokens_in, tokens_out = row.get("tokens_in", 0), row.get("tokens_out", 0)
        totals["tasks"] += 1
        totals["ok" if row.get("ok") else "failed"] += 1
        totals["tokens_in"] += tokens_in
        totals["tokens_out"] += tokens_out
        totals["model_seconds"] += row.get("seconds", 0.0)
        totals["claude_tokens_saved"] += total
        totals["claude_input_saved"] += saved.get("claude_input_tokens", 0)
        totals["claude_output_saved"] += saved.get("claude_output_tokens", 0)
        negative += 1 if total < 0 else 0
        category = by_category.setdefault(row.get("category", "?"), {"tasks": 0, "claude_tokens_saved": 0})
        category["tasks"] += 1
        category["claude_tokens_saved"] += total
        if row.get("node"):
            node = by_node.setdefault(row["node"], {"tasks": 0, "tokens_in": 0, "tokens_out": 0,
                                                    "tokens_total": 0, "model_seconds": 0.0,
                                                    "claude_tokens_saved": 0, "models": {}})
            node["tasks"] += 1
            node["tokens_in"] += tokens_in
            node["tokens_out"] += tokens_out
            node["tokens_total"] = node["tokens_in"] + node["tokens_out"]
            node["model_seconds"] = round(node["model_seconds"] + row.get("seconds", 0.0), 1)
            node["claude_tokens_saved"] += total
            model = node["models"].setdefault(row.get("model", "?"), {"tasks": 0, "tokens_total": 0})
            model["tasks"] += 1
            model["tokens_total"] += tokens_in + tokens_out
        ts = row.get("ts", 0)
        first = ts if first is None else min(first, ts)
        last = ts if last is None else max(last, ts)

    totals["tokens_total"] = totals["tokens_in"] + totals["tokens_out"]
    totals["model_seconds"] = round(totals["model_seconds"], 1)

    def stamp(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None

    report = {
        "scope": project or "all projects",
        "period": {"from": stamp(first), "to": stamp(last), "days": round(days, 2) if days else None},
        "totals": totals,
        "usage_by_node": dict(sorted(by_node.items(), key=lambda kv: -kv[1]["tokens_total"])),
        "claude_savings_by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1]["claude_tokens_saved"])),
        "tasks_that_cost_more_than_they_saved": negative,
    }
    if untagged:
        report["older_tasks_without_a_project_tag"] = untagged
    return report


def describe_totals(stats: Stats) -> dict:
    t = {"saved_input": 0, "saved_output": 0, **stats.snapshot()["totals"]}
    return {
        **t,
        "tokens_processed_locally": t["tokens_in"] + t["tokens_out"],
        "estimated_claude_tokens_avoided": t["saved_input"] + t["saved_output"],
    }


def default_workspace_roots(cfg: FleetConfig, project_dir: str | None) -> list[str]:
    roots = [project_dir or str(Path.cwd())]
    roots += cfg.allowed_roots
    return roots
