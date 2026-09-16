"""Minimal async client for the Ollama HTTP API, built on urllib (no third-party deps)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never forward a node's bearer token to another URL
        return None


# Fleet nodes live on the LAN (or behind a VPN): never send their traffic
# through a corporate HTTP proxy picked up from the environment, and never follow redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class OllamaError(RuntimeError):
    pass


@dataclass
class ChatReply:
    content: str
    thinking: str
    prompt_tokens: int
    output_tokens: int
    prompt_s: float
    gen_s: float
    load_s: float
    total_s: float
    done_reason: str = ""  # "stop", or "length" when num_predict cut the answer off

    @property
    def gen_tps(self) -> float:
        return self.output_tokens / self.gen_s if self.gen_s > 0 else 0.0

    @property
    def prompt_tps(self) -> float:
        return self.prompt_tokens / self.prompt_s if self.prompt_s > 0 else 0.0


class OllamaClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = 900.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OllamaError(f"{self.base_url}{path}: HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"{self.base_url}{path}: {exc}") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            raise OllamaError(f"{self.base_url}{path}: invalid JSON response") from None

    async def get(self, path: str, timeout: float = 5.0) -> dict:
        return await asyncio.to_thread(self._call, "GET", path, None, timeout)

    async def post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        return await asyncio.to_thread(self._call, "POST", path, body, timeout or self.timeout)

    async def version(self) -> str:
        return (await self.get("/api/version", timeout=3.0)).get("version", "?")

    async def tags(self) -> list[dict]:
        return (await self.get("/api/tags")).get("models", [])

    async def ps(self) -> list[dict]:
        return (await self.get("/api/ps")).get("models", [])

    async def pull(self, model: str, timeout: float = 3600.0) -> dict:
        return await self.post("/api/pull", {"model": model, "stream": False}, timeout=timeout)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        options: dict,
        keep_alive: str | None = None,
        think: bool | None = None,
        fmt: dict | str | None = None,
        timeout: float | None = None,
    ) -> ChatReply:
        body: dict = {"model": model, "messages": messages, "stream": False, "options": options}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        if think is not None:
            body["think"] = think
        if fmt is not None:
            body["format"] = fmt
        data = await self.post("/api/chat", body, timeout=timeout)
        msg = data.get("message", {})
        ns = 1e9
        return ChatReply(
            content=msg.get("content", ""),
            thinking=msg.get("thinking", "") or "",
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            prompt_s=data.get("prompt_eval_duration", 0) / ns,
            gen_s=data.get("eval_duration", 0) / ns,
            load_s=data.get("load_duration", 0) / ns,
            total_s=data.get("total_duration", 0) / ns,
            done_reason=str(data.get("done_reason") or ""),
        )
