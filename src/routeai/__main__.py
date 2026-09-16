"""Command line: `python run.py [serve|status|bench|init]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from .config import config_path, fleet_home, load_config
from .fleet import Fleet
from .stats import Stats


def _fleet() -> Fleet:
    return Fleet(load_config(), Stats(fleet_home() / "stats.json"))


async def _status() -> int:
    from .bench.runner import bench_advice
    from .engine import describe_totals

    fleet = _fleet()
    await fleet.refresh(force=True)
    print(f"config: {fleet.cfg.source or '(defaults, run init) ' + str(config_path())}")
    for n in fleet.describe():
        mark = "OK " if n["healthy"] else "DOWN"
        print(f"[{mark}] {n['name']:<12} {n['url']:<28} tier={n['tier']:<5} v{n['version']} "
              f"loaded={n['loaded']} missing={n['missing_models']}" + (f"  {n['error']}" if n["error"] else ""))
    print("\nrouting preview (2k tokens in, 800 out):")
    for cat in sorted(fleet.cfg.categories):
        ranked = fleet.rank(cat, 2000, 800)
        print(f"  {cat:<8} -> " + (f"{ranked[0].model} @ {ranked[0].node}  ({ranked[0].reason})" if ranked else "none"))
    totals = describe_totals(fleet.stats)
    print(f"\nsavings: ~{totals['estimated_claude_tokens_avoided']} Claude tokens avoided over {totals['tasks']} "
          f"delegated tasks (input {totals['saved_input']}, output {totals['saved_output']})")
    print("bench:", json.dumps(bench_advice(fleet)))
    return 0


async def _bench(args) -> int:
    from .bench.runner import run_bench

    fleet = _fleet()

    def progress(done, total, msg):
        print(f"[{done}/{total}] {msg}", file=sys.stderr, flush=True)

    report = await run_bench(fleet, mode=args.mode, explore=args.explore, deep=args.deep,
                             nodes=args.node, models=args.model, categories=args.category, progress=progress)
    print(report["markdown"])
    print(f"report saved to {report['report_path']}")
    return 0


async def _init(args) -> int:
    from .setup import generate, parse_spec, write_config

    try:
        specs = [parse_spec(s) for s in args.node or ["local=http://localhost:11434"]]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    text, nodes = await generate(specs)
    for n in nodes:
        state = f"{len(n['installed'])} models" if n["reachable"] else f"UNREACHABLE: {n['error']}"
        print(f"{n['name']}: {n['url']} -> {state}")
        for pull in n["suggested_pulls"]:
            print(f"    suggested: ollama pull {pull['model']}  ({pull['size_gb']} GB, {pull['hardware']})")
    try:
        path, backup = write_config(text, overwrite=args.force)
    except FileExistsError:
        print(f"{config_path()} already exists (use --force to overwrite; a backup is kept)", file=sys.stderr)
        return 1
    print(f"wrote {path}" + (f" (previous saved as {backup.name})" if backup else ""))
    return 0


async def _nodes(args) -> int:
    from .setup import (add_node, generate, load_text, node_blocks, parse_spec, remove_node, save_text,
                        set_node_option)

    text = load_text()
    if args.action == "list":
        for name, start, end in node_blocks(text):
            url = re.search(r'^url\s*=\s*"([^"]+)"', text[start:end], re.M)
            disabled = re.search(r"^enabled\s*=\s*false", text[start:end], re.M)
            print(f"{name:<14} {url.group(1) if url else '':<34}{'(disabled)' if disabled else ''}")
        return 0
    if not args.target:
        print(f"{args.action} needs NAME" + ("=URL" if args.action == "add" else ""), file=sys.stderr)
        return 2
    try:
        if args.action == "add":
            name, url = parse_spec(args.target)
            _, probed = await generate([(name, url)])
            state = "reachable" if probed[0]["reachable"] else f"UNREACHABLE: {probed[0]['error']}"
            print(f"{name}: {url} -> {state}")
            text = add_node(text, probed[0])
        elif args.action == "remove":
            text = remove_node(text, args.target)
        else:
            text = set_node_option(text, args.target, "enabled", args.action == "enable")
        path, backup = save_text(text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"wrote {path}" + (f" (previous saved as {backup.name})" if backup else ""))
    return 0


async def _usage(args) -> int:
    from pathlib import Path as _Path

    from .engine import usage_log

    report = usage_log(fleet_home() / "tasks.jsonl", None if args.all else str(_Path.cwd().resolve()), args.days)
    totals, period = report["totals"], report["period"]
    print(f"scope: {report['scope']}" + (f"   ({period['from']} -> {period['to']})" if period["from"] else ""))
    print(f"\nAI tokens processed: {totals['tokens_total']} "
          f"({totals['tokens_in']} read + {totals['tokens_out']} written) "
          f"in {totals['model_seconds']}s of model time")
    for name, row in report["usage_by_node"].items():
        models = ", ".join(f"{model} x{d['tasks']}" for model, d in row["models"].items())
        print(f"  {name:<14} {row['tokens_total']:>8} tokens  {row['tasks']:>3} tasks  "
              f"{row['model_seconds']:>7}s  {models}")
    print(f"\ntasks: {totals['tasks']} ({totals['ok']} ok, {totals['failed']} failed), "
          f"{report['tasks_that_cost_more_than_they_saved']} cost more than they saved")
    print(f"Claude tokens saved: ~{totals['claude_tokens_saved']} "
          f"(input {totals['claude_input_saved']}, output {totals['claude_output_saved']})")
    for name, row in report["claude_savings_by_category"].items():
        print(f"  {name:<10} {row['tasks']:>3} tasks  {row['claude_tokens_saved']:>8} tokens")
    return 0


async def _queue(args) -> int:
    from pathlib import Path as _Path

    from . import queue as work_queue
    from .engine import Engine, queue_worker
    from .workspace import Workspace

    if args.action == "list":
        snapshot = work_queue.snapshot()
        print(f"pending {snapshot['pending']} | running {snapshot['running']} | done {snapshot['done']} "
              f"({snapshot['folder']})")
        for title, rows in (("queued", snapshot["queued"]), ("running", snapshot["in_progress"]),
                            ("finished", snapshot["finished"])):
            for row in rows:
                result = row.get("result") or {}
                state = "" if title != "finished" else ("  ok" if result.get("ok") else f"  FAILED {result.get('error', '')[:60]}")
                print(f"  [{title:<8}] {row['id']}  {row.get('category', '?'):<8} {row['instruction'][:60]}{state}")
        return 0
    if args.action == "clear":
        print(f"removed {work_queue.clear(args.what)} {args.what} entries")
        return 0

    fleet = _fleet()
    stats = fleet.stats

    def engine_for(root):
        base = root or str(_Path.cwd())
        return Engine(fleet.cfg, fleet, stats, Workspace([base, *fleet.cfg.allowed_roots]))

    print(f"working the queue ({'once' if args.once else 'until stopped with Ctrl+C'})", file=sys.stderr)
    handled = await queue_worker(engine_for, once=args.once)
    print(f"finished {handled} queued task(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="routeai")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    sub.add_parser("status", help="show nodes, routing preview, savings and bench advice")
    b = sub.add_parser("bench", help="run the self-learning benchmark")
    b.add_argument("--mode", choices=["quick", "full"], default="quick")
    b.add_argument("--explore", action="store_true", help="also try installed models that are not configured")
    b.add_argument("--deep", action="store_true", help="probe parallel throughput per node")
    b.add_argument("--node", action="append", help="limit to node (repeatable)")
    b.add_argument("--model", action="append", help="limit to model (repeatable)")
    b.add_argument("--category", action="append", help="limit to category (repeatable)")
    q = sub.add_parser("queue", help="work the queue, or list and clear it")
    q.add_argument("action", choices=["list", "work", "clear"])
    q.add_argument("--once", action="store_true", help="for work: stop when the queue is empty")
    q.add_argument("--what", choices=["done", "pending", "all"], default="done", help="for clear")
    u = sub.add_parser("usage", help="Ollama tokens per machine, and what the fleet saved")
    u.add_argument("--all", action="store_true", help="every project, not just the current directory")
    u.add_argument("--days", type=float, help="only the last N days")
    n = sub.add_parser("nodes", help="add, remove, enable or disable one machine")
    n.add_argument("action", choices=["list", "add", "remove", "enable", "disable"])
    n.add_argument("target", nargs="?", metavar="NAME=URL|NAME")
    i = sub.add_parser("init", help="write a starter fleet.toml by probing nodes")
    i.add_argument("--node", action="append", metavar="NAME=URL")
    i.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import build_server
        asyncio.run(build_server().serve())
        return 0
    if args.cmd == "status":
        return asyncio.run(_status())
    if args.cmd == "bench":
        return asyncio.run(_bench(args))
    if args.cmd == "nodes":
        return asyncio.run(_nodes(args))
    if args.cmd == "usage":
        return asyncio.run(_usage(args))
    if args.cmd == "queue":
        return asyncio.run(_queue(args))
    return asyncio.run(_init(args))


if __name__ == "__main__":
    sys.exit(main())
