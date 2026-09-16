"""A small, dependency-free MCP server over stdio (JSON-RPC 2.0, one message per line).

Implements what a tools-only server needs: initialize, ping, tools/list,
tools/call and cancellation. Keeping it in the standard library means the
plugin runs with nothing but Python 3.11+.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import traceback
from collections.abc import Awaitable, Callable

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

ToolFn = Callable[[dict], Awaitable[object]]


class ToolError(Exception):
    """Raise from a tool to return a clean error message to the model."""


class StdioServer:
    def __init__(self, name: str, version: str, instructions: str | None = None):
        self.on_start = None  # optional coroutine started once the event loop is running
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: dict[str, tuple[dict, ToolFn]] = {}
        self._inflight: dict[object, asyncio.Task] = {}
        self._write_lock = threading.Lock()

    def tool(self, name: str, description: str, schema: dict, *, read_only: bool = False):
        def deco(fn: ToolFn) -> ToolFn:
            meta = {"name": name, "description": description, "inputSchema": schema}
            if read_only:
                meta["annotations"] = {"readOnlyHint": True}
            self._tools[name] = (meta, fn)
            return fn
        return deco

    # -- transport -------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._write_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def _reply(self, msg_id, result=None, error: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._send(msg)

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def reader():  # blocking stdin reads stay off the event loop (works on Windows too)
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, daemon=True).start()
        if self.on_start is not None:
            asyncio.ensure_future(self.on_start())
        while (line := await queue.get()) is not None:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:  # includes invalid UTF-8
                self._reply(None, error={"code": -32700, "message": "parse error"})
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                if not isinstance(m, dict):
                    self._reply(None, error={"code": -32600, "message": "invalid request"})
                    continue
                try:
                    self._dispatch(m)
                except Exception as exc:  # a bad message must never stop the server
                    print(traceback.format_exc(), file=sys.stderr, flush=True)
                    if m.get("id") is not None:
                        self._reply(m.get("id"), error={"code": -32603, "message": f"internal error: {exc}"})
        # stdin closed: let in-flight calls finish (bounded) so their replies are not lost.
        pending = list(self._inflight.values())
        if pending:
            _, late = await asyncio.wait(pending, timeout=30)
            for task in late:
                task.cancel()

    def _dispatch(self, msg: dict) -> None:
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if not isinstance(params, dict):
            if msg_id is not None:
                self._reply(msg_id, error={"code": -32602, "message": "params must be an object"})
            return
        if method is None:
            return  # a response to something we never send; ignore
        if method == "initialize":
            requested = params.get("protocolVersion")
            result = {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            self._reply(msg_id, result)
        elif method == "ping":
            self._reply(msg_id, {})
        elif method == "tools/list":
            self._reply(msg_id, {"tools": [meta for meta, _ in self._tools.values()]})
        elif method == "tools/call":
            task = asyncio.ensure_future(self._call_tool(msg_id, params))
            self._inflight[msg_id] = task
            task.add_done_callback(lambda _t, i=msg_id: self._inflight.pop(i, None))
        elif method == "notifications/cancelled":
            task = self._inflight.get(params.get("requestId"))
            if task:
                task.cancel()
        elif method.startswith("notifications/"):
            return
        elif msg_id is not None:
            self._reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})

    async def _call_tool(self, msg_id, params: dict) -> None:
        name = params.get("name")
        entry = self._tools.get(name)
        if entry is None:
            self._reply(msg_id, error={"code": -32602, "message": f"unknown tool: {name}"})
            return
        meta, fn = entry
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            self._reply(msg_id, error={"code": -32602, "message": "arguments must be an object"})
            return
        missing = [k for k in meta["inputSchema"].get("required", []) if k not in args]
        if missing:
            self._reply(msg_id, {"content": [{"type": "text", "text": f"missing required argument(s): {', '.join(missing)}"}],
                                 "isError": True})
            return
        try:
            value = await fn(args)
            is_error = isinstance(value, dict) and value.get("status") == "failed"
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str)
            self._reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        except asyncio.CancelledError:
            return  # the client cancelled; the spec says not to answer
        except ToolError as exc:
            self._reply(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            self._reply(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                 "isError": True})
