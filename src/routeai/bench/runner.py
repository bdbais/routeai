"""Runs the benchmark, feeds the learned stats and writes a report with recommendations.

The benchmark only *recommends* configuration changes: Claude (or you) decides
what to apply to fleet.toml. Routing, however, uses the learned quality and
speed immediately.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass

from ..config import fleet_home, is_cloud_model, normalize_model
from ..fleet import Candidate, Fleet
from ..ollama_client import OllamaError
from ..prompts import extract_file_content, strip_thinking, system_prompt
from ..stats import Stats
from .suite import BenchTask, select_tasks

EXPLORE_MAX_BYTES = 20e9
WEAK_SCORE = 0.4


@dataclass
class Row:
    node: str
    model: str
    category: str
    task: str
    score: float
    seconds: float
    gen_tps: float
    tokens_out: int
    detail: str = ""
    error: str | None = None


def plan(fleet: Fleet, tasks: list[BenchTask], *, explore: bool, nodes: list[str] | None,
         models: list[str] | None, include_remote: bool = False) -> dict[str, list[tuple[str, list[BenchTask]]]]:
    """Which model runs which tasks on which node. Loaded models go first to avoid swaps."""
    suite_cats = sorted({t.category for t in tasks})
    work: dict[str, list[tuple[str, list[BenchTask]]]] = {}
    for n in fleet.cfg.enabled_nodes:
        st = fleet.state[n.name]
        if (nodes and n.name not in nodes) or not st.healthy:
            continue
        if n.is_remote and not (include_remote or (nodes and n.name in nodes)):
            continue  # benchmarking a provider spends quota or money: opt in explicitly
        per_model: dict[str, set[str]] = defaultdict(set)
        for cat in suite_cats:
            for m in fleet.models_for(n, st, cat):
                per_model[normalize_model(m)].add(cat)
        if explore:
            for info in st.installed.values():
                caps = info.get("capabilities", ["completion"])
                if "completion" in caps and info.get("size", 0) <= EXPLORE_MAX_BYTES:
                    per_model[normalize_model(info["name"])].update(suite_cats)
        wanted = {normalize_model(m).lower() for m in models or []}
        entries = []
        for m, cats in per_model.items():
            if wanted and m.lower() not in wanted:
                continue
            if (is_cloud_model(m) or st.is_remote(m)) and not fleet.cfg.allow_cloud_models:
                continue
            if not st.has(m) and not fleet.cfg.auto_pull:
                continue
            chosen = [t for t in tasks if t.category in cats]
            if chosen:
                entries.append((m, chosen))
        entries.sort(key=lambda e: not st.is_loaded(e[0]))
        if entries:
            work[n.name] = entries
    return work


async def run_one(fleet: Fleet, node: str, model: str, task: BenchTask) -> Row:
    n = fleet.cfg.node(node)
    cat = fleet.cfg.categories.get(task.category) or fleet.cfg.categories["general"]
    cand = Candidate(node=node, model=model, tier=fleet.tier_of(n), utility=0.0, expected_s=0.0,
                     quality=0.0, samples=0, preferred_free=True, installed=True)
    messages = [
        {"role": "system", "content": system_prompt(task.category, task.filename, task.schema is not None)},
        {"role": "user", "content": task.prompt},
    ]
    started = time.time()
    try:
        async with fleet.slot(node):
            reply = await fleet.chat(cand, messages, cat, max_tokens=task.max_tokens, fmt=task.schema)
    except OllamaError as exc:
        return Row(node, model, task.category, task.id, 0.0, round(time.time() - started, 1), 0.0, 0,
                   error=str(exc)[:200])
    text = strip_thinking(reply.content)
    try:
        answer = extract_file_content(text, task.filename) if task.filename else text
        score, detail = await asyncio.to_thread(task.grade, answer)
    except Exception as exc:  # a crashing grader or a cut-off answer scores 0, it never stops the benchmark
        score, detail = 0.0, f"grader error: {type(exc).__name__}: {exc}"
    fleet.stats.record_quality(node, model, task.category, score, source="bench")
    return Row(node, model, task.category, task.id, round(score, 3), round(time.time() - started, 1),
               round(reply.gen_tps, 1), reply.output_tokens, detail[:200])


async def probe_parallel(fleet: Fleet, node: str, model: str) -> dict:
    """Aggregate tokens/s with 1, 2 and 4 concurrent requests (bounded by OLLAMA_NUM_PARALLEL on the node)."""
    n, client = fleet.cfg.node(node), fleet.clients[node]
    think = False if "thinking" in fleet.state[node].capabilities(model) else None
    messages = [{"role": "user", "content": "List the numbers from 1 to 80 separated by commas. Output only the list."}]
    options = {"num_ctx": n.ctx_for(model), "num_predict": 160, "temperature": 0}

    async def once():
        return await client.chat(model, messages, options=options, keep_alive=n.keep_alive, think=think)

    await once()  # warm-up so load time does not skew k=1
    throughput = {}
    for k in (1, 2, 4):
        started = time.time()
        replies = await asyncio.gather(*(once() for _ in range(k)), return_exceptions=True)
        wall = time.time() - started
        tokens = sum(r.output_tokens for r in replies if not isinstance(r, BaseException))
        throughput[k] = round(tokens / wall, 1) if wall > 0 else 0.0
    best = max(throughput.values())
    recommended = min(k for k, v in throughput.items() if v >= 0.9 * best)
    return {"model": model, "throughput_tps": throughput, "recommended_parallel": recommended}


async def run_bench(fleet: Fleet, *, mode: str = "quick", explore: bool = False, deep: bool = False,
                    nodes: list[str] | None = None, models: list[str] | None = None,
                    categories: list[str] | None = None, include_remote: bool = False, progress=None) -> dict:
    await fleet.refresh(force=True)
    tasks = select_tasks(mode, categories)
    work = plan(fleet, tasks, explore=explore, nodes=nodes, models=models, include_remote=include_remote)
    total = sum(len(ts) for entries in work.values() for _, ts in entries)
    rows: list[Row] = []
    gpu: dict[str, float] = {}
    profiles: dict[str, dict] = {}
    done = 0

    async def run_node(name: str):
        nonlocal done
        for model, ts in work[name]:
            for task in ts:
                row = await run_one(fleet, name, model, task)
                rows.append(row)
                done += 1
                if progress:
                    progress(done, total, f"{name} · {model} · {task.id}: "
                                          + (f"error {row.error}" if row.error else f"{row.score:.0%} @ {row.gen_tps} tok/s"))
            ratio = await fleet.gpu_ratio(name, model)
            if ratio is not None:
                gpu[f"{name}|{model}"] = round(ratio, 2)
                fleet.stats.record_speed(name, model, gen_tps=0, prompt_tps=0, load_s=0, gpu_ratio=round(ratio, 2))
        if deep and work[name]:
            model = work[name][0][0]
            try:
                profiles[name] = await probe_parallel(fleet, name, model)
                fleet.stats.record_node(name, parallel=profiles[name], probed_at=time.time())
            except OllamaError as exc:
                profiles[name] = {"model": model, "error": str(exc)[:200]}

    outcomes = await asyncio.gather(*(run_node(n) for n in work), return_exceptions=True)
    node_errors = {name: f"{type(o).__name__}: {o}" for name, o in zip(work, outcomes) if isinstance(o, BaseException)}
    fleet.stats.mark_bench()
    report = build_report(fleet, rows, gpu, profiles, mode, explore)
    if node_errors:
        report["node_errors"] = node_errors
        report["recommendations"] = [f"The benchmark on {n} stopped early: {e}" for n, e in node_errors.items()] + [
            r for r in report["recommendations"] if r != "No changes suggested."]
        report["markdown"] = to_markdown(report)
    report["report_path"] = str(save_report(report))
    return report


def build_report(fleet: Fleet, rows: list[Row], gpu: dict[str, float], profiles: dict[str, dict],
                 mode: str, explore: bool) -> dict:
    groups: dict[tuple[str, str, str], list[Row]] = defaultdict(list)
    for r in rows:
        groups[(r.node, r.model, r.category)].append(r)
    table = []
    for (node, model, cat), rs in sorted(groups.items()):
        ok = [r for r in rs if not r.error]
        table.append({
            "node": node, "model": model, "category": cat,
            "score": round(statistics.mean(r.score for r in rs), 3),
            "gen_tps": round(statistics.mean(r.gen_tps for r in ok), 1) if ok else 0.0,
            "seconds": round(statistics.mean(r.seconds for r in rs), 1),
            "gpu": gpu.get(f"{node}|{model}"),
            "errors": sum(1 for r in rs if r.error),
            "runs": len(rs),
        })

    best: dict[str, dict] = {}
    for row in table:
        cur = best.get(row["category"])
        if cur is None or (row["score"], row["gen_tps"]) > (cur["score"], cur["gen_tps"]):
            best[row["category"]] = row

    recs: list[str] = []
    for row in table:
        n = fleet.cfg.node(row["node"])
        configured = [normalize_model(m) for m in n.models.get(row["category"], [])]
        if row["errors"]:
            recs.append(f"{row['model']} on {row['node']} failed {row['errors']} task(s) in '{row['category']}'.")
        elif row["score"] < WEAK_SCORE and row["model"] in configured:
            recs.append(f"Drop {row['model']} from '{row['category']}' on {row['node']}: it scored {row['score']:.0%}.")
        elif explore and row["model"] not in configured and row["score"] >= best[row["category"]]["score"] - 0.05:
            recs.append(f"Consider adding {row['model']} to '{row['category']}' on {row['node']} "
                        f"({row['score']:.0%} at {row['gen_tps']} tok/s).")
    by_key = {(r["node"], r["model"], r["category"]): r for r in table}
    for cat, row in best.items():
        n = fleet.cfg.node(row["node"])
        configured = [normalize_model(m) for m in n.models.get(cat, [])]
        if not configured or configured[0] == row["model"] or row["model"] not in configured:
            continue
        first = by_key.get((row["node"], configured[0], cat))
        clearly_better = first is None or row["score"] > first["score"] or (
            row["score"] == first["score"] and row["gen_tps"] >= 1.5 * max(first["gen_tps"], 0.1))
        if clearly_better:
            recs.append(f"Put {row['model']} first for '{cat}' on {row['node']} "
                        f"(best measured: {row['score']:.0%} at {row['gen_tps']} tok/s over {row['runs']} run(s)).")
    cpu_only = defaultdict(list)
    for key, ratio in gpu.items():
        cpu_only[key.split("|", 1)[0]].append(ratio)
    for node, ratios in cpu_only.items():
        if max(ratios) == 0:
            recs.append(f"{node} runs every model on CPU only: keep small models (3-7B) and max_parallel = 1 there.")
    for key, ratio in gpu.items():
        if 0 < ratio < 0.95:
            node, model = key.split("|", 1)
            recs.append(f"{model} on {node} is only {ratio:.0%} in VRAM, the rest runs on CPU: lower its context "
                        f"(model_ctx) or use a smaller model if speed matters more than quality.")

    speeds = defaultdict(float)
    for row in table:
        speeds[row["node"]] = max(speeds[row["node"]], row["gen_tps"])
    if len(speeds) > 1:
        fastest = max(speeds, key=speeds.get)
        for n in fleet.cfg.enabled_nodes:
            if n.name == fastest and n.tier == "light":
                recs.append(f"{n.name} is the fastest node ({speeds[n.name]} tok/s) but has tier='light'; consider 'heavy'.")
            if n.name != fastest and n.tier == "heavy" and speeds.get(n.name, 0) < 0.5 * speeds[fastest]:
                recs.append(f"{n.name} is much slower than {fastest}; consider tier='light'.")
    for name, prof in profiles.items():
        n = fleet.cfg.node(name)
        k = prof.get("recommended_parallel")
        if k and k != n.max_parallel:
            recs.append(f"Set max_parallel={k} for {name} (throughput by concurrency: {prof['throughput_tps']}); "
                        f"the Ollama server there needs OLLAMA_NUM_PARALLEL>={k}.")

    advice = bench_advice(fleet)
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode, "explore": explore, "tasks_run": len(rows),
        "table": table,
        "best_per_category": {c: f"{r['model']} @ {r['node']} ({r['score']:.0%}, {r['gen_tps']} tok/s)" for c, r in sorted(best.items())},
        "parallel_probe": profiles,
        "recommendations": recs or ["No changes suggested."],
        "learning": advice,
        "rows": [asdict(r) for r in rows],
    }
    report["markdown"] = to_markdown(report)
    return report


def to_markdown(report: dict) -> str:
    lines = [f"# routeai benchmark — {report['generated_at']}", "",
             f"Mode: {report['mode']}{' + explore' if report['explore'] else ''} · tasks: {report['tasks_run']}", "",
             "| node | model | category | score | tok/s | avg s | GPU |", "|---|---|---|---:|---:|---:|---:|"]
    for r in report["table"]:
        gpu = f"{r['gpu']:.0%}" if r["gpu"] is not None else "–"
        lines.append(f"| {r['node']} | {r['model']} | {r['category']} | {r['score']:.0%} | {r['gen_tps']} | {r['seconds']} | {gpu} |")
    lines += ["", "## Best per category", ""]
    lines += [f"- **{c}**: {v}" for c, v in report["best_per_category"].items()]
    lines += ["", "## Recommendations", ""] + [f"- {r}" for r in report["recommendations"]]
    learn = report["learning"]
    lines += ["", "## Learning status", "",
              f"- configured model/category pairs: {learn['pairs']} · stable: {learn['stable']} · "
              f"still learning: {learn['learning']} · no data: {learn['unknown']}",
              f"- next bench: {learn['next_bench']}"]
    return "\n".join(lines) + "\n"


def save_report(report: dict):
    folder = fleet_home() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"bench-{stamp}.md"
    path.write_text(report["markdown"], encoding="utf-8")
    (folder / "latest.json").write_text(json.dumps({k: v for k, v in report.items() if k != "markdown"}, indent=1),
                                        encoding="utf-8")
    return path


def bench_advice(fleet: Fleet) -> dict:
    """Is a new benchmark worth running? Frequent while the fleet is still learning, monthly once stable."""
    snap = fleet.stats.snapshot()
    pairs = {
        (n.name, normalize_model(m), cat)
        for n in fleet.cfg.enabled_nodes for cat, ms in n.models.items() for m in ms
        if cat in fleet.cfg.categories and not is_cloud_model(m)
    }
    entries = {p: snap["quality"].get("|".join(p)) for p in pairs}
    unknown = [p for p, e in entries.items() if not e]
    learning = [p for p, e in entries.items() if e and not Stats.is_stable(e)]
    last = snap.get("last_bench")
    age = (time.time() - last) / 86400 if last else None
    reasons = []
    if last is None:
        reasons.append("never benchmarked")
    elif unknown:
        reasons.append(f"{len(unknown)} configured model/category pairs have no quality data yet")
    if age is not None and learning and age >= 2:
        reasons.append(f"still learning {len(learning)} pairs and the last bench was {age:.0f} days ago")
    if age is not None and not learning and not unknown and age >= 30:
        reasons.append("monthly re-check")
    if learning or unknown:
        next_bench = "every 2-3 days until all pairs are stable"
    else:
        next_bench = "monthly, or after installing new models / changing hardware"
    return {
        "due": bool(reasons), "reasons": reasons, "pairs": len(pairs),
        "stable": len(pairs) - len(unknown) - len(learning), "learning": len(learning), "unknown": len(unknown),
        "last_bench_days_ago": round(age, 1) if age is not None else None, "next_bench": next_bench,
    }
