"""Fleet configuration: one TOML file, parsed with the standard library only."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TIERS = ("heavy", "light", "auto")
NODE_TYPES = ("ollama", "openai")  # "openai" = any OpenAI-compatible API
PREFERENCES = ("heavy", "light", "any")
ROUTING_POLICIES = ("priority", "fastest")

# Built-in task categories. "prefer" is the node tier that should get the work
# first; the other tier only receives overflow when the preferred one is busy.
DEFAULT_CATEGORIES: dict[str, dict] = {
    "complex": {"prefer": "heavy", "max_output_tokens": 4096, "temperature": 0.2},
    "code": {"prefer": "heavy", "max_output_tokens": 3072, "temperature": 0.2},
    "tests": {"prefer": "light", "max_output_tokens": 3072, "temperature": 0.2},
    "scripts": {"prefer": "light", "max_output_tokens": 2048, "temperature": 0.2},
    "build": {"prefer": "light", "max_output_tokens": 2048, "temperature": 0.1},
    "docs": {"prefer": "light", "max_output_tokens": 2048, "temperature": 0.3},
    "general": {"prefer": "any", "max_output_tokens": 2048, "temperature": 0.3},
}


class ConfigError(ValueError):
    pass


def fleet_home() -> Path:
    """Directory holding fleet.toml, learned stats, logs and bench reports."""
    home = os.environ.get("ROUTEAI_HOME")
    return Path(home).expanduser() if home else Path.home() / ".routeai"


def config_path() -> Path:
    env = os.environ.get("ROUTEAI_CONFIG")
    return Path(env).expanduser() if env else fleet_home() / "fleet.toml"


def normalize_model(name: str) -> str:
    name = name.strip()
    return name if ":" in name else f"{name}:latest"


def is_cloud_model(name: str) -> bool:
    """Ollama cloud models run on ollama.com, so prompts would leave your network."""
    tag = normalize_model(name).lower().split(":", 1)[1]
    return tag == "cloud" or tag.endswith("-cloud")


@dataclass
class Node:
    name: str
    url: str
    type: str = "ollama"        # ollama | openai (any OpenAI-compatible provider)
    send_files: bool = False    # remote nodes: may this project's files leave the machine?
    cost_input: float = 0.0     # USD per million tokens
    cost_output: float = 0.0
    daily_requests: int = 0     # 0 = no limit
    daily_tokens: int = 0
    daily_cost_usd: float = 0.0
    tier: str = "auto"
    priority: int = 0
    max_parallel: int = 1
    num_ctx: int = 8192
    model_ctx: dict[str, int] = field(default_factory=dict)
    keep_alive: str = "30m"
    enabled: bool = True
    models: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    api_key_env: str | None = None
    ssh: str | None = None          # user@host[:port] or an ~/.ssh/config alias: reach Ollama through a tunnel
    ssh_key: str | None = None      # key created by `routeai ssh-setup`; unset = your own ssh config and agent
    remote_port: int = 11434        # where Ollama listens on the server (only on 127.0.0.1 is enough)

    @property
    def is_remote(self) -> bool:
        return self.type != "ollama"

    @property
    def via_ssh(self) -> bool:
        return bool(self.ssh)

    @property
    def is_free(self) -> bool:
        return self.cost_input == 0.0 and self.cost_output == 0.0

    def models_for(self, category: str) -> list[str]:
        """Models for a category; an explicit empty list means "never send this category here"."""
        if category in self.models:
            return self.models[category]
        return self.models.get("general", [])

    def all_models(self) -> list[str]:
        seen: dict[str, None] = {}
        for names in self.models.values():
            for n in names:
                seen.setdefault(n, None)
        return list(seen)

    def ctx_for(self, model: str) -> int:
        """Fixed context per model: changing num_ctx forces Ollama to reload the model."""
        wanted = normalize_model(model).lower()
        for name, ctx in self.model_ctx.items():
            if normalize_model(name).lower() == wanted:
                return ctx
        return self.num_ctx

    def request_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.api_key_env and os.environ.get(self.api_key_env):
            headers["Authorization"] = f"Bearer {os.environ[self.api_key_env]}"
        return headers


@dataclass
class Category:
    name: str
    prefer: str = "any"
    think: bool = False
    temperature: float = 0.2
    max_output_tokens: int = 2048
    min_quality: float = 0.0


@dataclass
class FleetConfig:
    nodes: list[Node]
    categories: dict[str, Category]
    routing: str = "priority"
    prefer_free: bool = True      # free nodes (local, free tiers) before paid ones
    auto_pull: bool = False
    allow_cloud_models: bool = False
    explore_rate: float = 0.1
    request_timeout_s: float = 900.0
    sync_wait_s: float = 240.0
    max_file_bytes: int = 256_000
    preview_chars: int = 400
    allowed_roots: list[str] = field(default_factory=list)
    source: Path | None = None

    def node(self, name: str) -> Node | None:
        return next((n for n in self.nodes if n.name == name), None)

    @property
    def enabled_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.enabled]


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"expected a model name or a list of model names, got {value!r}")


def parse_config(data: dict, source: Path | None = None) -> FleetConfig:
    fleet = data.get("fleet", {})
    nodes: list[Node] = []
    for raw in data.get("nodes", []):
        if "name" not in raw or not (raw.get("url") or raw.get("ssh")):
            raise ConfigError("every [[nodes]] entry needs 'name' and either 'url' or 'ssh'")
        cost = raw.get("cost", {})
        limits = raw.get("limits", {})
        node = Node(
            name=raw["name"],
            url=(raw.get("url") or f"ssh://{raw['ssh']}").rstrip("/"),
            type=raw.get("type", "ollama"),
            send_files=bool(raw.get("send_files", False)),
            cost_input=float(cost.get("input", 0.0)),
            cost_output=float(cost.get("output", 0.0)),
            daily_requests=int(limits.get("daily_requests", 0)),
            daily_tokens=int(limits.get("daily_tokens", 0)),
            daily_cost_usd=float(limits.get("daily_cost_usd", 0.0)),
            tier=raw.get("tier", "auto"),
            priority=int(raw.get("priority", 0)),
            max_parallel=max(1, int(raw.get("max_parallel", 1))),
            num_ctx=int(raw.get("num_ctx", 8192)),
            model_ctx={k: int(v) for k, v in raw.get("model_ctx", {}).items()},
            keep_alive=str(raw.get("keep_alive", "30m")),
            enabled=bool(raw.get("enabled", True)),
            models={k: _as_list(v) for k, v in raw.get("models", {}).items()},
            headers=dict(raw.get("headers", {})),
            api_key_env=raw.get("api_key_env"),
            ssh=raw.get("ssh") or None,
            ssh_key=raw.get("ssh_key") or None,
            remote_port=int(raw.get("remote_port", 11434)),
        )
        if node.tier not in TIERS:
            raise ConfigError(f"node {node.name}: tier must be one of {TIERS}")
        if node.type not in NODE_TYPES:
            raise ConfigError(f"node {node.name}: type must be one of {NODE_TYPES}")
        if node.via_ssh:
            from .sshtunnel import parse_target  # validated here so a bad target fails at load, not mid-task
            if node.type != "ollama":
                raise ConfigError(f"node {node.name}: ssh tunnels are for Ollama nodes")
            try:
                parse_target(node.ssh)
            except ValueError as exc:
                raise ConfigError(f"node {node.name}: {exc}") from None
            if not 0 < node.remote_port < 65536:
                raise ConfigError(f"node {node.name}: remote_port must be a TCP port")
        if node.is_remote and not node.api_key_env and not node.headers:
            raise ConfigError(f"node {node.name}: a remote provider needs api_key_env (the key itself never "
                              "belongs in fleet.toml)")
        nodes.append(node)
    names = [n.name for n in nodes]
    if len(set(names)) != len(names):
        raise ConfigError("node names must be unique")

    categories: dict[str, Category] = {}
    overrides = data.get("categories", {})
    for name in {**DEFAULT_CATEGORIES, **overrides}:
        merged = {**DEFAULT_CATEGORIES.get(name, {}), **overrides.get(name, {})}
        cat = Category(name=name, **merged)
        if cat.prefer not in PREFERENCES:
            raise ConfigError(f"category {name}: prefer must be one of {PREFERENCES}")
        categories[name] = cat

    cfg = FleetConfig(
        nodes=nodes,
        categories=categories,
        routing=fleet.get("routing", "priority"),
        prefer_free=bool(fleet.get("prefer_free", True)),
        auto_pull=bool(fleet.get("auto_pull", False)),
        allow_cloud_models=bool(fleet.get("allow_cloud_models", False)),
        explore_rate=float(fleet.get("explore_rate", 0.1)),
        request_timeout_s=float(fleet.get("request_timeout_s", 900)),
        sync_wait_s=float(fleet.get("sync_wait_s", 240)),
        max_file_bytes=int(fleet.get("max_file_bytes", 256_000)),
        preview_chars=int(fleet.get("preview_chars", 400)),
        allowed_roots=list(fleet.get("allowed_roots", [])),
        source=source,
    )
    if cfg.routing not in ROUTING_POLICIES:
        raise ConfigError(f"fleet.routing must be one of {ROUTING_POLICIES}")
    return cfg


def default_config() -> FleetConfig:
    """Used when no fleet.toml exists: a single local node, models picked from what is installed."""
    return parse_config({"nodes": [{"name": "local", "url": "http://localhost:11434"}]})


def load_config(path: Path | None = None) -> FleetConfig:
    path = path or config_path()
    if not path.exists():
        return default_config()
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
    return parse_config(data, source=path)
