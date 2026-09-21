"""Command line: `python run.py [serve|status|bench|nodes|usage|queue|init|ssh-*|send-stats]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
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
            ssh = re.search(r'^ssh\s*=\s*"([^"]+)"', text[start:end], re.M)
            url = re.search(r'^url\s*=\s*"([^"]+)"', text[start:end], re.M)
            where = f"ssh://{ssh.group(1)}" if ssh else (url.group(1) if url else "")
            disabled = re.search(r"^enabled\s*=\s*false", text[start:end], re.M)
            print(f"{name:<14} {where:<34}{'(disabled)' if disabled else ''}")
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


def _ask_vram(node: str) -> int:
    from .community import VRAM_BUCKETS

    print(f"     how much VRAM does the GPU of '{node}' have, in GB? {list(VRAM_BUCKETS)}")
    try:  # a closed stdin can still report isatty(): ask, but never crash
        answer = input("     GB (empty to skip this machine): ").strip()
    except EOFError:
        return 0
    return int(answer) if answer.isdigit() else 0


async def _send_stats(args) -> int:
    """Manual, never automatic: build the payload, show it, ask, then send."""
    from . import community as com
    from .bench.suite import SUITE_VERSION
    from .config import load_config

    state = com.load_state()
    if args.status:
        if not state:
            print("nothing shared from this machine yet")
            return 0
        print(f"install registered on {SITE_LABEL}: "
              f"{'certified (GitHub)' if state.get('certified') else 'not certified'}")
        print(f"declared hardware: {state.get('hardware') or '(none yet)'}")
        return 0
    if args.forget:
        com.forget_state()
        print("local identity removed; your results on the site are untouched (use --delete for those)")
        return 0
    if args.delete:
        if not state.get("install_token"):
            print("nothing to delete: this machine never sent anything", file=sys.stderr)
            return 1
        answer = com.delete(state)
        print(f"deleted {answer.get('deleted', 0)} result(s) from {com.SITE}")
        return 0

    report_path = fleet_home() / "reports" / "latest.json"
    if not report_path.exists():
        print("no benchmark report yet: run /routeai:bench first", file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("suite_version") != SUITE_VERSION:
        print(f"this benchmark report comes from another suite ({report.get('suite_version') or 'unversioned'}): "
              "run /routeai:bench again so the results can be compared with everyone else's", file=sys.stderr)
        return 1
    cfg = load_config()

    hardware = dict(state.get("hardware") or {})
    for pair in args.vram or []:
        node, _, gb = pair.partition("=")
        if not gb.isdigit():
            print(f"expected --vram NODE=GB, got {pair!r}", file=sys.stderr)
            return 2
        hardware[node.strip()] = int(gb)
    for node in com.nodes_needing_vram(report):
        if not hardware.get(node):
            if not sys.stdin.isatty():
                print(f"the GPU size of '{node}' is unknown: pass --vram {node}=12 (in GB)", file=sys.stderr)
                return 2
            hardware[node] = _ask_vram(node)
    hardware = {k: v for k, v in hardware.items() if v}

    try:
        payload = com.build_payload(report, cfg, hardware, suite_versions=(SUITE_VERSION,))
    except com.CommunityError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"This is everything that would be sent to {com.SITE} - no machine names, addresses, paths, "
          "prompts or project data:\n")
    print(json.dumps(payload, indent=2))
    print("\n" + com.summarise(payload))
    if args.dry_run:
        print("\n--dry-run: nothing was sent")
        return 0

    if state.get("install_token") and not (args.login and not state.get("certified")):
        pass  # already registered
    else:
        github_token = None
        if not args.anonymous:
            if not sys.stdin.isatty():
                print("signing in with GitHub needs your own terminal; use --anonymous to send without it",
                      file=sys.stderr)
                return 2
            print("\nSigning in with GitHub (your results go in the certified statistics):")
            try:
                github_token = com.sign_in_with_github()
            except com.CommunityError as exc:
                print(f"     NO  {exc}", file=sys.stderr)
                return 1
        try:
            registered = com.register(github_token, state)
        except com.CommunityError as exc:
            print(exc, file=sys.stderr)
            return 1
        state = com.load_state()
        print(f"     registered as {'certified' if registered.get('certified') else 'not certified'}")

    state["hardware"] = hardware
    com.save_state(state)
    if not args.yes:
        if not sys.stdin.isatty():
            print("add --yes to confirm the send", file=sys.stderr)
            return 2
        try:
            confirmed = input(f"\nSend these results to {com.SITE}? [yes/no] ").strip().lower()
        except EOFError:
            print("add --yes to confirm the send", file=sys.stderr)
            return 2
        if confirmed not in ("yes", "y"):
            print("nothing was sent")
            return 0
    try:
        answer = com.send(payload, state)
    except com.CommunityError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"sent: {answer.get('accepted', 0)} result(s) published as "
          f"{'certified' if answer.get('certified') else 'not certified'}"
          + (f", {answer['outliers']} flagged as out of scale" if answer.get("outliers") else ""))
    print(f"they appear on {com.SITE}/community/ - remove them any time with `send-stats --delete`")
    return 0


SITE_LABEL = "routeai.bais.info"


def _stage(ok: bool, label: str, detail: str) -> None:
    print(f"     {'ok' if ok else 'NO'}  {label}: {detail}")


async def _ssh_setup(args) -> int:
    from . import sshtunnel as st
    from .setup import add_node, drop_node_option, load_text, node_blocks, probe, save_text, set_node_option

    try:
        target = st.parse_target(args.target)
        st.key_path(args.name)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not args.use_ssh_config and not sys.stdin.isatty():
        print("ssh-setup lets ssh ask for the server password, so it must run in your own terminal - not through "
              "Claude, and never with the password in a chat or a file.", file=sys.stderr)
        return 2

    print(f"1/5  reaching {target}")
    for label, ok, detail in st.check_reachable(target):
        _stage(ok, label, detail)
        if not ok:
            return 1

    key = None
    if args.use_ssh_config:
        print("2/5  using your own ssh configuration, agent and known_hosts (no new key)")
        print("3/5  nothing to install")
    else:
        try:
            path, created = st.generate_key(args.name)
        except (st.TunnelError, OSError, subprocess.CalledProcessError) as exc:
            print(f"2/5  key\n     NO  {exc}", file=sys.stderr)
            return 1
        print(f"2/5  key: {'created' if created else 'reusing'} {path}")
        restricted = not args.full_access
        print(f"3/5  installing it on {target}. ssh asks you to confirm the server fingerprint (first time) and for "
              "the password - type them here, RouteAI never sees them.")
        if not st.install_key_interactively(target, st.public_key_path(path).read_text(encoding="utf-8"),
                                            args.remote_port, restricted):
            print("     NO  the key was not installed: wrong password, fingerprint refused, or the server allows no "
                  f"password login - then append {st.public_key_path(path)} to ~/.ssh/authorized_keys on the server "
                  "yourself, or rerun with --use-ssh-config", file=sys.stderr)
            return 1
        _stage(True, "authorized_keys", "restricted: this key can only open the tunnel to Ollama" if restricted
               else "full access (--full-access)")
        key = str(path)

    print("4/5  opening the tunnel with the key alone")
    probed = await probe(args.name, f"ssh://{target}", ssh_key=key, remote_port=args.remote_port)
    st.close_all()
    if not probed["reachable"]:
        _stage(False, "tunnel", probed["error"])
        return 1
    _stage(True, "Ollama", f"version {probed['version']}, {len(probed['installed'])} models installed")

    print("5/5  saving the node")
    text = load_text()
    try:
        if any(name == args.name for name, _, _ in node_blocks(text)):
            text = set_node_option(text, args.name, "ssh", str(target))
            if key:
                text = set_node_option(text, args.name, "ssh_key", key)
            else:
                text = drop_node_option(text, args.name, "ssh_key")
            text = drop_node_option(text, args.name, "url")
            if args.remote_port != st.DEFAULT_REMOTE_PORT:
                text = set_node_option(text, args.name, "remote_port", args.remote_port)
            action = "updated"
        else:
            text = add_node(text, probed)
            action = "added"
        path, backup = save_text(text)
    except ValueError as exc:
        print(f"     NO  {exc}", file=sys.stderr)
        return 1
    _stage(True, action, f"{args.name} in {path}" + (f" (previous saved as {backup.name})" if backup else ""))
    for pull in probed["suggested_pulls"][:3]:
        print(f"     suggested on that machine: ollama pull {pull['model']}  ({pull['size_gb']} GB, {pull['hardware']})")
    return 0


async def _ssh_check(args) -> int:
    from pathlib import Path as _Path

    from . import sshtunnel as st
    from .config import load_config

    node = load_config().node(args.name)
    if node is None or not node.via_ssh:
        print(f"no SSH node named {args.name!r} in the configuration", file=sys.stderr)
        return 2
    target = st.parse_target(node.ssh)
    print(f"{node.name}: ssh://{target} -> 127.0.0.1:{node.remote_port} on the server")
    for label, ok, detail in st.check_reachable(target):
        _stage(ok, label, detail)
        if not ok:
            return 1
    if node.ssh_key:
        key = _Path(node.ssh_key).expanduser()
        _stage(key.exists(), "key", str(key) if key.exists() else f"{key} is missing: run ssh-setup again")
        if not key.exists():
            return 1
    tunnel = st.tunnel_for(node.name, node.ssh, node.remote_port, node.ssh_key)
    try:
        tunnel.ensure()
        _stage(True, "ssh", f"authenticated, tunnel on 127.0.0.1:{tunnel.local_port}")
        client = st.SshOllamaClient(tunnel, timeout=15)
        version = await client.version()
        models = await client.tags()
        _stage(True, "Ollama", f"version {version}, {len(models)} models")
        return 0
    except st.TunnelError as exc:
        _stage(False, "ssh", str(exc))
        return 1
    except st.OllamaError as exc:
        _stage(False, "Ollama", str(exc))
        return 1
    finally:
        st.close_all()


def _ssh_forget(args) -> int:
    from . import sshtunnel as st
    from .config import load_config

    node = load_config().node(args.name)
    if node is None or not node.via_ssh:
        print(f"no SSH node named {args.name!r} in the configuration", file=sys.stderr)
        return 2
    if st.forget_host(st.parse_target(node.ssh)):
        print(f"forgot the host key of {node.ssh}: run ssh-setup again to confirm the new fingerprint")
        return 0
    print("nothing to forget (no fingerprint stored by RouteAI for that host)")
    return 1


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
    s = sub.add_parser("ssh-setup", help="reach a remote Ollama through SSH: key, tunnel, node (run in a terminal)")
    s.add_argument("name", help="node name, e.g. linux-gpu")
    s.add_argument("target", help="user@host, user@host:2222 or an ~/.ssh/config alias")
    s.add_argument("--remote-port", type=int, default=11434, help="where Ollama listens on the server")
    s.add_argument("--use-ssh-config", action="store_true",
                   help="use your existing ssh config and keys instead of creating a dedicated key")
    s.add_argument("--full-access", action="store_true",
                   help="install the key without restrictions (default: it can only open the tunnel to Ollama)")
    c = sub.add_parser("ssh-check", help="diagnose an SSH node stage by stage")
    c.add_argument("name")
    f = sub.add_parser("ssh-forget", help="forget a server's host key after reinstalling it")
    f.add_argument("name")
    ss = sub.add_parser("send-stats", help="share your benchmark results on routeai.bais.info (manual, opt-in)")
    ss.add_argument("--dry-run", action="store_true", help="print exactly what would be sent, send nothing")
    ss.add_argument("--yes", action="store_true", help="skip the confirmation (the payload is still printed)")
    ss.add_argument("--anonymous", action="store_true", help="send without signing in: uncertified statistics")
    ss.add_argument("--login", action="store_true", help="sign in with GitHub to certify an existing install")
    ss.add_argument("--vram", action="append", metavar="NODE=GB", help="GPU size of a machine, e.g. gpu=12")
    ss.add_argument("--delete", action="store_true", help="remove this machine's results from the site")
    ss.add_argument("--forget", action="store_true", help="forget the local install token")
    ss.add_argument("--status", action="store_true", help="show what this machine registered")
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
    if args.cmd == "ssh-setup":
        return asyncio.run(_ssh_setup(args))
    if args.cmd == "ssh-check":
        return asyncio.run(_ssh_check(args))
    if args.cmd == "ssh-forget":
        return _ssh_forget(args)
    if args.cmd == "send-stats":
        return asyncio.run(_send_stats(args))
    return asyncio.run(_init(args))


if __name__ == "__main__":
    sys.exit(main())
