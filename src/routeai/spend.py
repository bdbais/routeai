"""Daily ledger per node: requests, tokens and money, so free quotas and budgets are respected.

Local Ollama nodes cost nothing and have no quota, so their rows are only used for reporting.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path

from .config import Node


def _today() -> str:
    return date.today().isoformat()


class Spend:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # accounting must never break a task

    def today(self, node: str) -> dict:
        row = self._read().get(node, {}).get(_today(), {})
        return {"requests": row.get("requests", 0), "tokens_in": row.get("tokens_in", 0),
                "tokens_out": row.get("tokens_out", 0), "cost_usd": row.get("cost_usd", 0.0)}

    def record(self, node: Node, tokens_in: int, tokens_out: int) -> dict:
        cost = (tokens_in * node.cost_input + tokens_out * node.cost_output) / 1_000_000
        with self._lock:
            data = self._read()
            row = data.setdefault(node.name, {}).setdefault(_today(), {})
            row["requests"] = row.get("requests", 0) + 1
            row["tokens_in"] = row.get("tokens_in", 0) + tokens_in
            row["tokens_out"] = row.get("tokens_out", 0) + tokens_out
            row["cost_usd"] = round(row.get("cost_usd", 0.0) + cost, 6)
            row["last"] = time.time()
            for name, days in list(data.items()):  # keep a month of history
                for day in sorted(days)[:-31]:
                    days.pop(day, None)
            self._write(data)
            return dict(row)

    def exhausted(self, node: Node) -> str | None:
        """Why this node cannot be used right now, or None."""
        if not (node.daily_requests or node.daily_tokens or node.daily_cost_usd):
            return None
        used = self.today(node.name)
        if node.daily_requests and used["requests"] >= node.daily_requests:
            return f"daily request quota reached ({used['requests']}/{node.daily_requests})"
        if node.daily_tokens and used["tokens_in"] + used["tokens_out"] >= node.daily_tokens:
            return f"daily token quota reached ({used['tokens_in'] + used['tokens_out']}/{node.daily_tokens})"
        if node.daily_cost_usd and used["cost_usd"] >= node.daily_cost_usd:
            return f"daily budget reached (${used['cost_usd']:.2f}/${node.daily_cost_usd:.2f})"
        return None

    def report(self, nodes: list[Node]) -> dict:
        out = {}
        for node in nodes:
            used = self.today(node.name)
            row = {**used, "free": node.is_free}
            limits = {k: v for k, v in (("daily_requests", node.daily_requests),
                                        ("daily_tokens", node.daily_tokens),
                                        ("daily_cost_usd", node.daily_cost_usd)) if v}
            if limits:
                row["limits"] = limits
                row["blocked"] = self.exhausted(node)
            out[node.name] = row
        return out
