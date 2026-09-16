"""MCP tools exposed to Claude."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from . import __version__
from .bench.runner import bench_advice, run_bench
from .config import ConfigError, config_path, fleet_home, load_config
from . import queue as work_queue
from .engine import (TOOL_CALL_TOKENS, Engine, TaskSpec, default_workspace_roots,
                     describe_totals, queue_worker, usage_log)
from .fleet import Fleet
from .mcp_stdio import StdioServer, ToolError
from .prompts import estimate_tokens
from .providers import PROVIDERS
from .setup import (add_node, generate, load_text, node_blocks, parse_spec, probe,
                    remove_node, save_text, set_node_option, write_config)
from .stats import Stats
from .workspace import Workspace, WorkspaceError

INSTRUCTIONS = """routeai runs small, well-scoped tasks on the user's own Ollama machines, at no Claude token cost.
You stay in charge: decide what to delegate, write a precise self-contained brief, and review what comes back.
Good fits: unit tests, scripts, docstrings/docs, boilerplate, build/CI files, log triage, bulk per-file edits.
Keep for yourself: design decisions, cross-cutting changes, security-sensitive code, anything you cannot verify.
Pass file paths in `files` (the server reads them; do not paste contents). Use `output_path` to have the result
written to disk and only a preview returned. After checking a result, call fleet_feedback so routing keeps learning.
Every finished task or batch carries `tokens_saved`: always tell the user, in one line, how many Claude tokens it
saved (this task and the session total). If fleet_status reports setup_needed, offer /routeai:setup.
When the user is close to their Claude usage limit or wants work to continue while they are away, queue tasks with
fleet_queue: the fleet keeps working without you and you review the results afterwards."""

CATEGORY_DOC = ("complex/code → fast GPU node first; tests/scripts/build/docs → light nodes first (overflow to the "
                "other tier when busy); general → any; auto → keyword guess.")


def _project_dir() -> str | None:
    value = os.environ.get("ROUTEAI_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    return value if value and not value.startswith("${") else None


class Runtime:
    """Config, fleet and engine, reloadable after the configuration is rewritten."""

    def __init__(self):
        # Tools run concurrently: every write to fleet.toml goes through this lock, so two adds in the
        # same turn cannot both read the old file and clobber each other.
        self.config_lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        old = getattr(self, "engine", None)
        self.cfg = load_config()
        self.stats = Stats(fleet_home() / "stats.json")
        self.fleet = Fleet(self.cfg, self.stats)
        self.ws = Workspace(default_workspace_roots(self.cfg, _project_dir()))
        self.engine = Engine(self.cfg, self.fleet, self.stats, self.ws)
        if old is not None:
            # Running jobs keep their own references and finish normally; keep them (and the task ids used by
            # fleet_feedback, and the session's savings) visible through the new engine.
            self.engine.jobs, self.engine._tasks = old.jobs, old._tasks
            self.engine.session_saved, self.engine.session_tasks = old.session_saved, old.session_tasks

    def engine_for(self, root: str | None):
        """An engine whose workspace is the queued task's own project."""
        if not root or Path(root).resolve() == self.ws.root:
            return self.engine
        return Engine(self.cfg, self.fleet, self.stats, Workspace([root, *self.cfg.allowed_roots]))

    async def run_queue(self) -> None:
        workers = max(1, sum(n.max_parallel for n in self.cfg.enabled_nodes))
        await asyncio.gather(*(queue_worker(self.engine_for) for _ in range(workers)))

    def running_jobs(self) -> list[str]:
        return [j.id for j in self.engine.jobs.values() if j.state == "running"]


def build_server() -> StdioServer:
    rt = Runtime()
    categories = sorted(rt.cfg.categories) + ["auto"]
    server = StdioServer("routeai", __version__, INSTRUCTIONS)

    @server.tool("fleet_status", "Nodes, health, loaded models, routing preview per category, token savings and "
                 "whether a benchmark or the first setup is due.", {"type": "object", "properties": {}}, read_only=True)
    async def fleet_status(_args: dict):
        await rt.fleet.refresh(force=True)
        preview = {}
        for cat in sorted(rt.cfg.categories):
            ranked = rt.fleet.rank(cat, 2000, 800)
            preview[cat] = ranked[0].brief() if ranked else "no usable node/model"
        return {
            "config": str(rt.cfg.source or f"(defaults: local Ollama only; create {config_path()})"),
            "setup_needed": rt.cfg.source is None,
            "workspace_roots": [str(r) for r in rt.ws.roots],
            "routing_policy": rt.cfg.routing,
            "nodes": rt.fleet.describe(),
            "routing_preview": preview,
            "tokens_saved": rt.engine.savings_report(),
            "totals": describe_totals(rt.stats),
            "bench": bench_advice(rt.fleet),
            "running_jobs": rt.running_jobs(),
            "queue": {k: v for k, v in work_queue.snapshot(limit=3).items() if k in ("pending", "running", "done")},
        }

    @server.tool("fleet_setup", "Create or replace the fleet configuration by probing the user's Ollama servers: "
                 "picks installed models per category and lists recommended models that are missing (with size "
                 "and hardware). Without `nodes` it only probes the configured machines. Never pull models "
                 "without the user's approval.", {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "url": {"type": "string", "description": "e.g. http://192.168.1.13:11434"}},
                "required": ["name", "url"]}},
            "overwrite": {"type": "boolean", "default": False,
                          "description": "Replace an existing fleet.toml (the old one is kept as a backup)."},
        },
    })
    async def fleet_setup(args: dict):
        nodes = args.get("nodes") or []
        try:
            specs = [parse_spec(f"{n['name']}={n['url']}") for n in nodes] or [(n.name, n.url) for n in rt.cfg.nodes]
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"invalid node: {exc}") from None
        text, probed = await generate(specs)
        report = {"config": str(config_path()), "nodes": probed}
        if not nodes:
            report["hint"] = "probe only; pass nodes=[{name, url}] to write a new configuration"
            return report
        async with rt.config_lock:
            return await _write_setup(rt, text, probed, report, bool(args.get("overwrite")))

    async def _write_setup(rt, text, probed, report, overwrite):
        try:
            path, backup = write_config(text, overwrite=overwrite)
        except FileExistsError:
            raise ToolError(f"{config_path()} already exists: ask the user, then call again with overwrite=true "
                            "(the current file is kept as a backup)") from None
        try:
            rt.load()
        except ConfigError as exc:
            raise ToolError(f"wrote {path} but it does not load: {exc}") from None
        report.update(written=str(path), backup=str(backup) if backup else None,
                      next_steps=["ask which suggested models to pull, then fleet_pull them",
                                  "run /routeai:bench (with explore after new pulls)"])
        return report

    @server.tool("fleet_nodes", "Add, remove, enable or disable ONE machine in the fleet configuration, "
                 "keeping every other machine's settings and comments. 'add' probes the new server and picks its "
                 "models per category; 'list' only shows the current machines. The previous file is kept as a backup.", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable"]},
            "name": {"type": "string", "description": "short name of the machine, e.g. gpu"},
            "url": {"type": "string", "description": "for 'add': http://192.168.1.13:11434, or a provider base URL"},
            "provider": {"type": "string", "enum": sorted(PROVIDERS),
                         "description": "for 'add' of a remote AI: fills url and the usual key variable "
                                        "(gemini, groq, openrouter, deepseek, mistral, openai, custom)"},
            "api_key_env": {"type": "string", "description": "name of the environment variable holding the key - "
                                                             "never paste the key itself"},
            "send_files": {"type": "boolean", "default": False,
                           "description": "may this project's files be sent to that provider? ask the user first"},
            "models": {"type": "object", "description": "models per category, e.g. {\"docs\": [\"gemini-2.5-flash\"]}"},
            "cost_input": {"type": "number", "description": "USD per million input tokens (0 = free tier)"},
            "cost_output": {"type": "number", "description": "USD per million output tokens"},
            "daily_requests": {"type": "integer", "description": "free-tier or self-imposed daily request cap"},
            "daily_tokens": {"type": "integer"},
            "daily_cost_usd": {"type": "number", "description": "stop using this provider after this much per day"},
            "tier": {"type": "string", "enum": ["heavy", "light", "auto"], "description": "for 'add' (default: auto, decided by the benchmark)"},
            "max_parallel": {"type": "integer", "minimum": 1, "maximum": 8},
            "num_ctx": {"type": "integer", "minimum": 2048, "maximum": 131072},
        },
        "required": ["action"],
    })
    async def fleet_nodes(args: dict):
        action = args["action"]
        if action == "list":
            await rt.fleet.refresh(force=True)
            return {"config": str(rt.cfg.source or config_path()), "nodes": rt.fleet.describe()}
        name = (args.get("name") or "").strip()
        if not name:
            raise ToolError("name is required for add, remove, enable and disable")
        async with rt.config_lock:
            return await _edit_node(rt, action, name, args)

    async def _edit_node(rt, action, name, args):
        text = load_text()
        report: dict = {"action": action, "node": name}
        try:
            if action == "add":
                preset = PROVIDERS.get(args.get("provider") or "")
                url = args.get("url") or (preset or {}).get("url") or ""
                key_env = args.get("api_key_env") or (preset or {}).get("key_env") or ""
                node_type = "openai" if (args.get("provider") or args.get("api_key_env")) else "ollama"
                _, url = parse_spec(f"{name}={url}")
                if node_type == "openai" and not key_env:
                    raise ToolError("a remote provider needs api_key_env: the name of the environment variable "
                                    "holding the key (never the key itself)")
                probed = await probe(name, url, node_type, key_env or None)
                probed.update(send_files=bool(args.get("send_files")),
                              cost={"input": args.get("cost_input", 0.0), "output": args.get("cost_output", 0.0)},
                              limits={k: args.get(k) for k in ("daily_requests", "daily_tokens", "daily_cost_usd")
                                      if args.get(k)})
                if args.get("models"):
                    probed["models"] = {k: (v if isinstance(v, list) else [v]) for k, v in args["models"].items()}
                options = {k: args[k] for k in ("tier", "max_parallel", "num_ctx") if args.get(k) is not None}
                text = add_node(text, probed, options)
                report["probe"] = probed
            elif action == "remove":
                text = remove_node(text, name)
            else:
                text = set_node_option(text, name, "enabled", action == "enable")
            path, backup = save_text(text)
        except (ConfigError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        rt.load()
        await rt.fleet.refresh(force=True)
        report.update(written=str(path), backup=str(backup) if backup else None, nodes=rt.fleet.describe())
        if action == "add":
            report["next_steps"] = ["ask the user before pulling any suggested model, then fleet_pull it",
                                    "run /routeai:bench so the new machine gets measured"]
        return report

    @server.tool("fleet_delegate", "Delegate ONE small task to a local model and get the result. The server reads "
                 "`files` itself. With `output_path` the answer is written to that file and only a preview comes "
                 "back. Long tasks return a job_id to poll with fleet_job. " + CATEGORY_DOC, {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "Self-contained brief: what to produce, constraints, "
                            "conventions, acceptance criteria. The worker sees only this, `context` and `files`."},
            "category": {"type": "string", "enum": categories, "default": "auto"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Project-relative paths or globs (e.g. src/**/*.py) to include."},
            "context": {"type": "string", "description": "Extra context: conventions, interfaces, examples."},
            "output_path": {"type": "string", "description": "Project-relative file to write the result to."},
            "overwrite": {"type": "boolean", "default": True},
            "json_schema": {"type": "object", "description": "JSON Schema for structured output; parsed JSON is returned in `json`."},
            "node": {"type": "string", "description": "Force a node by name (normally leave empty)."},
            "model": {"type": "string", "description": "Force a model (normally leave empty)."},
            "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 16384},
            "wait_seconds": {"type": "number", "description": "Max wait before returning a job_id (default from config)."},
        },
        "required": ["instruction"],
    })
    async def fleet_delegate(args: dict):
        spec = TaskSpec(
            instruction=args["instruction"], category=args.get("category", "auto"),
            files=args.get("files") or [], context=args.get("context"), output_path=args.get("output_path"),
            json_schema=args.get("json_schema"), node=args.get("node"), model=args.get("model"),
            max_output_tokens=args.get("max_output_tokens"), overwrite=args.get("overwrite", True),
        )
        return await rt.engine.delegate(spec, args.get("wait_seconds"))

    @server.tool("fleet_delegate_batch", "Apply the same instruction to many files in parallel across all nodes "
                 "(one task per file). Returns a job_id immediately; poll fleet_job. Use `output_pattern` with "
                 "{path} {dir} {stem} {name} {ext}, e.g. 'tests/test_{stem}.py'. " + CATEGORY_DOC, {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Paths or globs; one task per file."},
            "category": {"type": "string", "enum": categories, "default": "auto"},
            "output_pattern": {"type": "string"},
            "shared_files": {"type": "array", "items": {"type": "string"},
                             "description": "Files included in every task (interfaces, style guide, conftest)."},
            "context": {"type": "string"},
            "overwrite": {"type": "boolean", "default": True},
            "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 16384},
        },
        "required": ["instruction", "files"],
    })
    async def fleet_delegate_batch(args: dict):
        spec = TaskSpec(instruction=args["instruction"], category=args.get("category", "auto"),
                        context=args.get("context"), max_output_tokens=args.get("max_output_tokens"),
                        overwrite=args.get("overwrite", True))
        shared_files = args.get("shared_files") or []
        try:
            paths, shared = await asyncio.to_thread(rt.engine.expand_batch, args["files"], shared_files)
            job = rt.engine.start_batch(spec, paths, args.get("output_pattern"), shared, args["files"] + shared_files)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from None
        return {"job_id": job.id, "total": job.total, "hint": "poll fleet_job with this job_id"}

    @server.tool("fleet_job", "Progress and results of a background job (batch, long task, benchmark or pull).", {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "include_outputs": {"type": "boolean", "default": False,
                                "description": "Include output previews of each task."},
        },
        "required": ["job_id"],
    }, read_only=True)
    async def fleet_job(args: dict):
        job = rt.engine.jobs.get(args["job_id"])
        if not job:
            raise ToolError(f"unknown job_id {args['job_id']}")
        summary = job.summary(bool(args.get("include_outputs")), rt.cfg.preview_chars)
        if saved := rt.engine.job_savings(job):
            summary["tokens_saved"] = saved
        if job.kind == "batch":  # Claude pays for every poll: count it against the batch's savings
            rt.engine.charge_poll(job, estimate_tokens(json.dumps(summary, default=str)), TOOL_CALL_TOKENS)
        return summary

    @server.tool("fleet_feedback", "Tell the fleet how good a delegated result was, after you reviewed it. "
                 "good = used as is, fixed = needed corrections, rejected = unusable. This trains the router.", {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["good", "fixed", "rejected"]},
            "note": {"type": "string"},
        },
        "required": ["task_id", "verdict"],
    })
    async def fleet_feedback(args: dict):
        try:
            return rt.engine.feedback(args["task_id"], args["verdict"], args.get("note"))
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @server.tool("fleet_bench", "Start the self-learning benchmark in the background: graded coding tasks per "
                 "category on every node/model, plus optional resource probes. Returns a job_id; the finished job "
                 "contains a report with recommendations for fleet.toml.", {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["quick", "full"], "default": "quick"},
            "explore": {"type": "boolean", "default": False,
                        "description": "Also try installed models that are not configured."},
            "deep": {"type": "boolean", "default": False, "description": "Probe parallel throughput per node."},
            "nodes": {"type": "array", "items": {"type": "string"}},
            "models": {"type": "array", "items": {"type": "string"}},
            "categories": {"type": "array", "items": {"type": "string"}},
        },
    })
    async def fleet_bench(args: dict):
        async def body(job):
            def progress(done, total, msg):
                job.total, job.done, job.progress = total, done, msg
            report = await run_bench(
                rt.fleet, mode=args.get("mode", "quick"), explore=bool(args.get("explore")),
                deep=bool(args.get("deep")), nodes=args.get("nodes"), models=args.get("models"),
                categories=args.get("categories"), progress=progress,
            )
            job.report = {k: report[k] for k in ("report_path", "best_per_category", "recommendations", "learning", "table")}

        job = rt.engine.start_job("bench", 0, body)
        return {"job_id": job.id, "hint": "benchmarks take minutes; poll fleet_job"}

    @server.tool("fleet_usage", "How much the fleet was used and what it saved: tokens processed per node "
                 "(local machines and providers) and per model, cost in USD, today's quota use, Claude tokens "
                 "saved, and a breakdown per category. This project by default; scope 'all' for every project, "
                 "`days` for a period.", {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["project", "all"], "default": "project"},
            "days": {"type": "number", "minimum": 0.1, "description": "only the last N days"},
        },
    }, read_only=True)
    async def fleet_usage(args: dict):
        project = str(rt.ws.root) if args.get("scope", "project") != "all" else None
        report = await asyncio.to_thread(usage_log, rt.engine.log_path, project, args.get("days"))
        for name, row in report["usage_by_node"].items():
            node = rt.cfg.node(name)
            if node is None:
                continue
            row["type"] = node.type
            row["free"] = node.is_free
            row["cost_usd"] = round((row["tokens_in"] * node.cost_input
                                     + row["tokens_out"] * node.cost_output) / 1_000_000, 4)
        report["total_cost_usd"] = round(sum(r.get("cost_usd", 0.0) for r in report["usage_by_node"].values()), 4)
        report["today_per_node"] = rt.fleet.spend.report(rt.cfg.enabled_nodes)
        report["this_session"] = rt.engine.savings_report()
        return report

    @server.tool("fleet_queue", "Queue work the fleet finishes on its own, so it keeps going while you are "
                 "paused (usage limit reached, session closed) - slow but steady. Actions: 'add' (same fields as "
                 "fleet_delegate; give an output_path so the result lands on disk), 'list' (queued, running and "
                 "finished, with results to review), 'cancel' one by id, 'clear' (done | pending | all).", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "cancel", "clear"]},
            "instruction": {"type": "string", "description": "for 'add': the self-contained brief"},
            "category": {"type": "string", "enum": categories, "default": "auto"},
            "files": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "string"},
            "output_path": {"type": "string", "description": "where the result is written; strongly recommended"},
            "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 16384},
            "note": {"type": "string", "description": "why this was queued, shown when reviewing"},
            "id": {"type": "string", "description": "for 'cancel'"},
            "what": {"type": "string", "enum": ["done", "pending", "all"], "default": "done"},
        },
        "required": ["action"],
    })
    async def fleet_queue(args: dict):
        action = args["action"]
        if action == "list":
            snapshot = await asyncio.to_thread(work_queue.snapshot)
            snapshot["hint"] = ("the queue is worked by this session and by `python run.py queue work`; "
                                "review finished results, then fleet_feedback each one")
            return snapshot
        if action == "cancel":
            if not args.get("id"):
                raise ToolError("id is required to cancel a queued task")
            return {"cancelled": work_queue.cancel(args["id"]), "id": args["id"]}
        if action == "clear":
            return {"removed": work_queue.clear(args.get("what", "done")), "what": args.get("what", "done")}
        if not args.get("instruction"):
            raise ToolError("instruction is required to queue a task")
        spec = {k: args[k] for k in ("instruction", "category", "files", "context", "output_path",
                                     "max_output_tokens") if args.get(k) is not None}
        try:
            if spec.get("output_path"):
                rt.ws.check_writable(spec["output_path"])
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from None
        item = await asyncio.to_thread(work_queue.enqueue, spec, str(rt.ws.root), args.get("note"))
        counts = await asyncio.to_thread(work_queue.snapshot, 0)
        return {"queued": item["id"], "pending": counts["pending"], "running": counts["running"],
                "hint": "the fleet works through the queue on its own; check back with fleet_queue list"}

    @server.tool("fleet_pull", "Download a model onto a node (ollama pull). Downloads can be many GB and take "
                 "minutes: only with the user's approval.", {
        "type": "object",
        "properties": {"node": {"type": "string"}, "model": {"type": "string"}},
        "required": ["node", "model"],
    })
    async def fleet_pull(args: dict):
        if not rt.cfg.node(args["node"]):
            raise ToolError(f"unknown node {args['node']}")

        async def body(job):
            await rt.fleet.clients[args["node"]].pull(args["model"])
            await rt.fleet.refresh(force=True)
            job.done = 1

        job = rt.engine.start_job("pull", 1, body)
        return {"job_id": job.id, "hint": "poll fleet_job"}

    server.on_start = rt.run_queue
    return server
