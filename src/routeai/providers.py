"""Remote providers: any OpenAI-compatible API (Gemini, Groq, OpenRouter, DeepSeek, Mistral, OpenAI, vLLM...).

One adapter covers them all: they differ only in base URL, the environment variable holding the key, and
whether they have a free tier. Model names are never hardcoded - they are read from the provider's /models.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from .ollama_client import _OPENER, ChatReply, OllamaError

# Providers the plugin knows how to set up. `free_tier` means the provider offers a daily free quota,
# so the router can prefer it over a paid one; prices always come from the node's own [nodes.cost].
PROVIDERS = {
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "free_tier": True, "docs": "https://aistudio.google.com/apikey"},
    "groq": {"url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "free_tier": True, "docs": "https://console.groq.com/keys"},
    "openrouter": {"url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "free_tier": True, "docs": "https://openrouter.ai/keys"},
    "deepseek": {"url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY",
                 "free_tier": False, "docs": "https://platform.deepseek.com/api_keys"},
    "mistral": {"url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
                "free_tier": True, "docs": "https://console.mistral.ai/api-keys"},
    "openai": {"url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "free_tier": False, "docs": "https://platform.openai.com/api-keys"},
    "custom": {"url": "", "key_env": "", "free_tier": False, "docs": "any OpenAI-compatible endpoint"},
}


class OpenAICompatibleClient:
    """Same interface as OllamaClient, so the fleet treats a provider like any other node."""

    def __init__(self, base_url: str, api_key_env: str | None = None, headers: dict[str, str] | None = None,
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.headers = headers or {}
        self.timeout = timeout
        self.is_remote = True

    def _auth(self) -> dict[str, str]:
        key = os.environ.get(self.api_key_env or "", "")
        if not key:
            raise OllamaError(f"no API key: set {self.api_key_env} in your environment (never in fleet.toml)")
        return {"Authorization": f"Bearer {key}", **self.headers}

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **self._auth()})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise OllamaError(f"{self.base_url}{path}: HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"{self.base_url}{path}: {exc}") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            raise OllamaError(f"{self.base_url}{path}: invalid JSON response") from None

    async def version(self) -> str:
        await asyncio.to_thread(self._call, "GET", "/models", None, 15.0)  # also checks the key
        return "openai-compatible"

    async def tags(self) -> list[dict]:
        data = await asyncio.to_thread(self._call, "GET", "/models", None, 30.0)
        models = data.get("data") if isinstance(data, dict) else None
        return [{"name": m.get("id"), "capabilities": ["completion", "tools"], "remote": True}
                for m in (models or []) if m.get("id")]

    async def ps(self) -> list[dict]:
        return []  # nothing is loaded or swapped on a remote provider

    async def pull(self, model: str, timeout: float = 0) -> dict:
        raise OllamaError("a remote provider has no models to download")

    async def chat(self, model: str, messages: list[dict], *, options: dict, keep_alive: str | None = None,
                   think: bool | None = None, fmt: dict | str | None = None,
                   timeout: float | None = None) -> ChatReply:
        body: dict = {"model": model, "messages": messages,
                      "temperature": options.get("temperature", 0.2)}
        if options.get("num_predict"):
            body["max_tokens"] = options["num_predict"]
        if isinstance(fmt, dict):
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": fmt, "strict": False}}
        elif fmt:
            body["response_format"] = {"type": "json_object"}

        started = time.time()
        try:
            data = await asyncio.to_thread(self._call, "POST", "/chat/completions", body, timeout or self.timeout)
        except OllamaError as exc:
            if "response_format" in body and "HTTP 400" in str(exc):  # provider without JSON-schema support
                body["response_format"] = {"type": "json_object"}
                data = await asyncio.to_thread(self._call, "POST", "/chat/completions", body, timeout or self.timeout)
            else:
                raise
        elapsed = max(time.time() - started, 0.001)
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        finish = str(choice.get("finish_reason") or "")
        return ChatReply(
            content=(choice.get("message") or {}).get("content") or "",
            thinking=(choice.get("message") or {}).get("reasoning_content") or "",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            prompt_s=0.0, gen_s=elapsed, load_s=0.0, total_s=elapsed,
            done_reason="length" if finish == "length" else finish,
        )
