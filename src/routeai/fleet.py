"""Node registry and router: which machine and which model gets a task.

Routing policy "priority" (default): a category prefers a tier (heavy = the
fast GPU box for complex code, light = slower machines for scripts/tests/build).
Preferred-tier nodes with a free slot always win; the other tier only gets
overflow. Among equals, the learned utility decides:

    utility = quality^2 / expected_seconds

where expected_seconds covers prompt processing, generation, model load (if
not already in memory), a pull (if missing) and the node's current queue.
Policy "fastest" skips the tier preference and uses utility alone.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .config import Category, FleetConfig, Node, fleet_home, is_cloud_model, normalize_model
from .ollama_client import ChatReply, OllamaClient, OllamaError
from .providers import OpenAICompatibleClient
from .spend import Spend
from .stats import Stats

HEALTH_TTL_S = 15.0
HEALTH_TTL_REMOTE_S = 300.0  # remote providers: do not burn quota on health checks
PRIOR_GEN_TPS = {"heavy": 30.0, "light": 8.0}
PRIOR_PROMPT_TPS = {"heavy": 400.0, "light": 40.0}
PRIOR_LOAD_S = 20.0
PULL_PENALTY_S = 600.0
PRIOR_QUALITY = 0.6
QUALITY_FLOOR = 0.5
AVG_TASK_S = 40.0


@dataclass
class NodeState:
    healthy: bool = False
    version: str = "?"
    error: str | None = None
    installed: dict[str, dict] = field(default_factory=dict)  # lower-case name -> tag info
    loaded: dict[str, dict] = field(default_factory=dict)  # lower-case name -> ps info
    checked_at: float = 0.0

    def has(self, model: str) -> bool:
        return normalize_model(model).lower() in self.installed

    def is_loaded(self, model: str) -> bool:
        return normalize_model(model).lower() in self.loaded

    def capabilities(self, model: str) -> list[str]:
        return self.installed.get(normalize_model(model).lower(), {}).get("capabilities", [])

    def is_remote(self, model: str) -> bool:
        """Installed entries that proxy to ollama.com (cloud models, also under a copied alias)."""
        info = self.installed.get(normalize_model(model).lower(), {})
        return bool(info.get("remote_host") or info.get("remote_model"))


def make_client(node: Node, timeout: float):
    if node.type == "openai":
        return OpenAICompatibleClient(node.url, node.api_key_env, node.headers, timeout)
    if node.via_ssh:
        from .sshtunnel import SshOllamaClient, tunnel_for
        return SshOllamaClient(tunnel_for(node.name, node.ssh, node.remote_port, node.ssh_key),
                               node.request_headers(), timeout)
    return OllamaClient(node.url, node.request_headers(), timeout)


@dataclass
class Candidate:
    node: str
    model: str
    tier: str
    utility: float
    expected_s: float
    quality: float
    samples: int
    preferred_free: bool
    installed: bool
    free: bool = True
    reason: str = ""

    def brief(self) -> dict:
        return {
            "node": self.node, "model": self.model, "tier": self.tier, "free": self.free,
            "expected_s": round(self.expected_s, 1), "quality": round(self.quality, 2),
            "samples": self.samples, "why": self.reason,
        }


class Fleet:
    def __init__(self, cfg: FleetConfig, stats: Stats):
        self.cfg = cfg
        self.stats = stats
        self.clients = {n.name: make_client(n, cfg.request_timeout_s) for n in cfg.nodes}
        self.spend = Spend(fleet_home() / "spend.json")
        self.state = {n.name: NodeState() for n in cfg.nodes}
        self.active = {n.name: 0 for n in cfg.nodes}
        self._sems = {n.name: asyncio.Semaphore(n.max_parallel) for n in cfg.nodes}
        self._pulls: dict[tuple[str, str], asyncio.Task] = {}
        self._rng = random.Random()

    # -- health ------------------------------------------------------------

    async def refresh(self, force: bool = False) -> None:
        now = time.time()
        stale = [n for n in self.cfg.enabled_nodes
                 if now - self.state[n.name].checked_at > (HEALTH_TTL_REMOTE_S if n.is_remote else HEALTH_TTL_S)
                 or (force and not (n.is_remote and self.state[n.name].healthy
                                    and now - self.state[n.name].checked_at < HEALTH_TTL_REMOTE_S))]
        if stale:
            await asyncio.gather(*(self._probe(n) for n in stale))

    async def _probe(self, node: Node) -> None:
        client, st = self.clients[node.name], self.state[node.name]
        try:
            st.version = await client.version()
            st.installed = {m["name"].lower(): m for m in await client.tags()}
            st.loaded = {m["name"].lower(): m for m in await client.ps()}
            st.healthy, st.error = True, None
        except OllamaError as exc:
            st.healthy, st.error = False, str(exc)
        st.checked_at = time.time()

    # -- tiers ---------------------------------------------------------------

    def tier_of(self, node: Node, snapshot: dict | None = None) -> str:
        """Configured tier, or for tier="auto" the one the benchmark measured."""
        if node.tier != "auto":
            return node.tier
        snapshot = snapshot or self.stats.snapshot()
        best = {}
        for key, e in snapshot["speed"].items():
            name = key.split("|", 1)[0]
            best[name] = max(best.get(name, 0.0), e.get("gen_tps", 0.0))
        if not best.get(node.name):
            return "light"
        return "heavy" if best[node.name] >= 0.6 * max(best.values()) else "light"

    # -- routing -------------------------------------------------------------

    def rank(
        self,
        category: str,
        est_in: int,
        est_out: int,
        *,
        exclude: set[tuple[str, str]] = frozenset(),
        node: str | None = None,
        model: str | None = None,
        has_files: bool = False,
    ) -> list[Candidate]:
        cat = self.cfg.categories.get(category) or self.cfg.categories["general"]
        snap = self.stats.snapshot()
        cands: list[Candidate] = []
        for n in self.cfg.enabled_nodes:
            st = self.state[n.name]
            if not st.healthy or (node and n.name != node):
                continue
            tier = self.tier_of(n, snap)
            names = [model] if model else self.models_for(n, st, category)
            for rank_idx, m in enumerate(names):
                # ":latest" is an Ollama convention; a provider's model id must be passed through untouched
                m = m.strip() if n.is_remote else normalize_model(m)
                if (n.name, m) in exclude:
                    continue
                c = self._score(n, st, tier, m, rank_idx, cat, est_in, est_out, snap, has_files)
                if c:
                    cands.append(c)

        paid_last = (lambda c: not c.free) if self.cfg.prefer_free else (lambda c: False)
        if self.cfg.routing == "priority":
            cands.sort(key=lambda c: (paid_last(c), not c.preferred_free, -c.utility))
        else:
            cands.sort(key=lambda c: (paid_last(c), -c.utility))

        # Exploration: occasionally try an under-sampled peer so the fleet keeps learning.
        if len(cands) > 1 and not model and self._rng.random() < self.cfg.explore_rate:
            peers = [c for c in cands[:3] if c.preferred_free == cands[0].preferred_free and c.installed]
            rookie = min(peers, key=lambda c: c.samples, default=None)
            if rookie and rookie.samples < 5 and rookie is not cands[0]:
                rookie.reason += " (exploration)"
                cands.remove(rookie)
                cands.insert(0, rookie)
        return cands

    def models_for(self, n: Node, st: NodeState, category: str) -> list[str]:
        if n.models or n.is_remote:  # a provider lists hundreds of models: only what the user configured
            return n.models_for(category)
        # No models configured on this node: fall back to what is installed and chat-capable.
        chat = [
            info["name"] for info in st.installed.values()
            if "completion" in info.get("capabilities", ["completion"])
        ]
        coder = [m for m in chat if "coder" in m.lower()]
        return (coder or chat)[:3]

    def _score(self, n: Node, st: NodeState, tier: str, m: str, rank_idx: int, cat: Category,
               est_in: int, est_out: int, snap: dict, has_files: bool = False) -> Candidate | None:
        if not n.is_remote and (is_cloud_model(m) or st.is_remote(m)) and not self.cfg.allow_cloud_models:
            return None
        if n.is_remote:
            if has_files and not n.send_files:
                return None  # this provider was not allowed to receive the project's files
            if self.spend.exhausted(n):
                return None
        installed = st.has(m) or n.is_remote
        if not installed and not self.cfg.auto_pull:
            return None
        if est_in + est_out > n.ctx_for(m):
            return None
        speed = Stats.speed_of(snap, n.name, m)
        q, samples = Stats.quality_of(snap, n.name, m, cat.name)
        quality = q if q is not None else PRIOR_QUALITY - 0.05 * rank_idx
        if q is not None and samples >= 3 and q < cat.min_quality:
            return None
        base = tier if tier in PRIOR_GEN_TPS else "light"
        gen_tps = speed.get("gen_tps") or PRIOR_GEN_TPS[base]
        prompt_tps = speed.get("prompt_tps") or PRIOR_PROMPT_TPS[base]
        expected = est_in / prompt_tps + est_out * 0.5 / gen_tps
        if not installed:
            expected += PULL_PENALTY_S
        elif not st.is_loaded(m):
            expected += speed.get("load_s", PRIOR_LOAD_S)
        queue = self.active[n.name] / n.max_parallel
        expected += queue * AVG_TASK_S
        expected *= 1.0 - min(n.priority, 5) * 0.05  # small nudge for preferred machines
        free = self.active[n.name] < n.max_parallel
        # A model that measured poorly loses its tier precedence; utility then decides.
        acceptable = q is None or q >= max(cat.min_quality, QUALITY_FLOOR)
        preferred_free = free and acceptable and (cat.prefer == "any" or cat.prefer == tier)
        kind = "provider" if n.is_remote else "node"
        reason = f"{tier} {kind}, {'free slot' if free else f'{self.active[n.name]} busy'}"
        if n.is_remote:
            reason += ", paid" if not n.is_free else ", free tier"
        if not acceptable:
            reason += ", below quality floor"
        reason += ", loaded" if st.is_loaded(m) else (", not loaded" if installed else ", needs pull")
        reason += f", q={quality:.2f}" + ("" if q is not None else " (prior)")
        return Candidate(
            node=n.name, model=m, tier=tier, utility=quality ** 2 / (expected + 1.0),
            expected_s=expected, quality=quality, samples=samples,
            preferred_free=preferred_free, installed=installed, free=n.is_free, reason=reason,
        )

    # -- execution -----------------------------------------------------------

    @asynccontextmanager
    async def slot(self, node: str):
        """Reserve capacity on a node. Counted immediately so concurrent routing sees it."""
        self.active[node] += 1
        try:
            async with self._sems[node]:
                yield
        finally:
            self.active[node] -= 1

    async def ensure_model(self, node: str, model: str) -> None:
        st = self.state[node]
        if st.has(model) or self.cfg.node(node).is_remote:
            return
        if not self.cfg.auto_pull:
            raise OllamaError(f"{model} is not installed on {node} and auto_pull is off")
        key = (node, normalize_model(model))
        if key not in self._pulls:
            self._pulls[key] = asyncio.ensure_future(self.clients[node].pull(model))
        try:
            await self._pulls[key]
        finally:
            self._pulls.pop(key, None)
        await self._probe(self.cfg.node(node))

    async def chat(self, cand: Candidate, messages: list[dict], cat: Category, *,
                   max_tokens: int, fmt: dict | str | None = None) -> ChatReply:
        n = self.cfg.node(cand.node)
        await self.ensure_model(n.name, cand.model)
        st = self.state[n.name]
        think = cat.think if "thinking" in st.capabilities(cand.model) else None
        options = {"num_ctx": n.ctx_for(cand.model), "num_predict": max_tokens, "temperature": cat.temperature}
        try:
            reply = await self.clients[n.name].chat(
                cand.model, messages, options=options, keep_alive=n.keep_alive, think=think, fmt=fmt,
            )
        except OllamaError:
            self.stats.record_failure(n.name, cand.model)
            st.checked_at = 0.0  # re-probe the node before the next routing decision
            raise
        if not n.is_remote:
            st.loaded[normalize_model(cand.model).lower()] = {"name": cand.model}
        else:
            self.spend.record(n, reply.prompt_tokens, reply.output_tokens)
        self.stats.record_speed(
            n.name, cand.model, gen_tps=reply.gen_tps, prompt_tps=reply.prompt_tps, load_s=reply.load_s,
        )
        return reply

    async def gpu_ratio(self, node: str, model: str) -> float | None:
        """Share of the loaded model that sits in VRAM (1.0 = fully on GPU)."""
        try:
            for m in await self.clients[node].ps():
                if normalize_model(m["name"]).lower() == normalize_model(model).lower():
                    return m.get("size_vram", 0) / max(m.get("size", 1), 1)
        except OllamaError:
            pass
        return None

    # -- reporting -----------------------------------------------------------

    def describe(self) -> list[dict]:
        snap = self.stats.snapshot()
        out = []
        for n in self.cfg.nodes:
            st = self.state[n.name]
            used = self.spend.today(n.name)
            out.append({
                "name": n.name, "url": n.url, "type": n.type, "free": n.is_free, "enabled": n.enabled,
                "transport": "ssh" if n.via_ssh else "http",
                "healthy": st.healthy, "today": used, "quota_blocked": self.spend.exhausted(n),
                "sends_files_offsite": n.send_files if n.is_remote else False,
                "tier": self.tier_of(n, snap), "configured_tier": n.tier, "version": st.version,
                "active": self.active[n.name], "max_parallel": n.max_parallel, "num_ctx": n.num_ctx,
                "loaded": [m.get("name") for m in st.loaded.values()],
                "installed": len(st.installed),
                "missing_models": [m for m in n.all_models() if st.healthy and not st.has(m)],
                "error": st.error,
            })
        return out
