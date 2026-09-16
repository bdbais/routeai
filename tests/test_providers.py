"""Remote providers: the OpenAI-compatible client, the spend ledger, consent and quota-aware routing."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai.config import ConfigError, parse_config  # noqa: E402
from routeai.engine import Engine, TaskSpec  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.ollama_client import OllamaError  # noqa: E402
from routeai.providers import OpenAICompatibleClient  # noqa: E402
from routeai.spend import Spend  # noqa: E402
from routeai.stats import Stats  # noqa: E402
from routeai.workspace import Workspace  # noqa: E402

KEY_ENV = "ROUTEAI_TEST_KEY"


class StubProvider(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint: /models and /chat/completions."""

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {KEY_ENV}-value":
            self._send(401, {"error": "bad key"})
        else:
            self._send(200, {"data": [{"id": "fast-1"}, {"id": "smart-1"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).last_body = body
        if (body.get("response_format") or {}).get("type") == "json_schema":
            self._send(400, {"error": "json_schema not supported here"})
            return
        finish = "length" if body.get("max_tokens", 0) < 20 else "stop"
        self._send(200, {"choices": [{"message": {"content": "hello from the provider"}, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 120, "completion_tokens": 30}})

    def log_message(self, *args):
        pass


class ProviderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubProvider)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ROUTEAI_HOME")
        os.environ["ROUTEAI_HOME"] = self.tmp.name
        os.environ[KEY_ENV] = f"{KEY_ENV}-value"

    def tearDown(self):
        os.environ.pop(KEY_ENV, None)
        if self.old_home is None:
            os.environ.pop("ROUTEAI_HOME", None)
        else:
            os.environ["ROUTEAI_HOME"] = self.old_home
        self.tmp.cleanup()


class OpenAIClientTests(ProviderTestCase):
    def client(self):
        return OpenAICompatibleClient(self.url, KEY_ENV)

    def test_models_and_chat(self):
        models = asyncio.run(self.client().tags())
        self.assertEqual([m["name"] for m in models], ["fast-1", "smart-1"])
        reply = asyncio.run(self.client().chat("fast-1", [{"role": "user", "content": "hi"}],
                                               options={"num_predict": 500, "temperature": 0.2}))
        self.assertEqual(reply.content, "hello from the provider")
        self.assertEqual((reply.prompt_tokens, reply.output_tokens), (120, 30))
        self.assertEqual(reply.done_reason, "stop")
        self.assertGreater(reply.gen_tps, 0)

    def test_truncation_is_reported(self):
        reply = asyncio.run(self.client().chat("fast-1", [{"role": "user", "content": "hi"}],
                                               options={"num_predict": 10}))
        self.assertEqual(reply.done_reason, "length")

    def test_json_schema_falls_back_to_json_object(self):
        asyncio.run(self.client().chat("fast-1", [{"role": "user", "content": "hi"}],
                                       options={"num_predict": 100}, fmt={"type": "object"}))
        self.assertEqual(StubProvider.last_body["response_format"], {"type": "json_object"})

    def test_missing_key_is_explained(self):
        os.environ.pop(KEY_ENV)
        with self.assertRaises(OllamaError) as ctx:
            asyncio.run(self.client().tags())
        self.assertIn(KEY_ENV, str(ctx.exception))
        self.assertIn("never in fleet.toml", str(ctx.exception))


class SpendTests(ProviderTestCase):
    def node(self, name="prov", **kwargs):
        raw = {"name": name, "url": self.url, "type": "openai", "api_key_env": KEY_ENV, **kwargs}
        return parse_config({"nodes": [raw]}).node(name)

    def test_cost_and_quotas(self):
        spend = Spend(Path(self.tmp.name) / "spend.json")
        paid = self.node(cost={"input": 1.0, "output": 2.0}, limits={"daily_cost_usd": 0.001})
        self.assertIsNone(spend.exhausted(paid))
        row = spend.record(paid, 1_000_000, 0)          # $1.00 of input
        self.assertAlmostEqual(row["cost_usd"], 1.0)
        self.assertIn("daily budget reached", spend.exhausted(paid))

        free = self.node(name="freebie", limits={"daily_requests": 2})
        self.assertTrue(free.is_free)
        spend.record(free, 10, 10)
        self.assertIsNone(spend.exhausted(free))
        spend.record(free, 10, 10)
        self.assertIn("daily request quota", spend.exhausted(free))
        self.assertEqual((spend.today("prov")["requests"], spend.today("freebie")["requests"]), (1, 2))

    def test_remote_node_needs_a_key_env(self):
        with self.assertRaises(ConfigError):
            parse_config({"nodes": [{"name": "p", "url": self.url, "type": "openai"}]})


class RoutingTests(ProviderTestCase):
    def build(self, **fleet_opts):
        cfg = parse_config({
            "fleet": {"explore_rate": 0, **fleet_opts},
            "nodes": [
                {"name": "local", "url": "http://localhost:11434", "tier": "light",
                 "models": {"docs": ["small:3b"], "general": ["small:3b"]}},
                {"name": "free_api", "url": self.url, "type": "openai", "api_key_env": KEY_ENV, "tier": "heavy",
                 "send_files": False, "limits": {"daily_requests": 5},
                 "models": {"docs": ["fast-1"], "general": ["fast-1"]}},
                {"name": "paid_api", "url": self.url, "type": "openai", "api_key_env": KEY_ENV, "tier": "heavy",
                 "send_files": True, "cost": {"input": 3.0, "output": 15.0},
                 "models": {"docs": ["smart-1"], "general": ["smart-1"]}},
            ],
        })
        stats = Stats(Path(self.tmp.name) / "stats.json")
        fleet = Fleet(cfg, stats)
        for name, models in (("local", ["small:3b"]), ("free_api", ["fast-1"]), ("paid_api", ["smart-1"])):
            state = fleet.state[name]
            state.healthy = True
            state.installed = {m: {"name": m, "capabilities": ["completion"]} for m in models}
        return fleet

    def test_files_only_go_where_allowed(self):
        fleet = self.build()
        with_files = [c.node for c in fleet.rank("general", 500, 200, has_files=True)]
        self.assertNotIn("free_api", with_files)  # send_files = false
        self.assertIn("paid_api", with_files)
        self.assertIn("local", with_files)

    def test_free_before_paid(self):
        fleet = self.build()
        order = [c.node for c in fleet.rank("general", 500, 200)]
        self.assertLess(order.index("free_api"), order.index("paid_api"))
        self.assertTrue(all(c.free for c in fleet.rank("general", 500, 200)[:2]))

    def test_exhausted_quota_removes_the_provider(self):
        fleet = self.build()
        node = fleet.cfg.node("free_api")
        for _ in range(5):
            fleet.spend.record(node, 10, 10)
        self.assertNotIn("free_api", [c.node for c in fleet.rank("general", 500, 200)])

    def test_paid_can_be_disabled_entirely(self):
        fleet = self.build(prefer_free=False)
        self.assertIn("paid_api", [c.node for c in fleet.rank("general", 500, 200)])


class ProviderTaskTests(ProviderTestCase):
    def test_delegating_to_a_provider_records_spend(self):
        cfg = parse_config({"fleet": {"explore_rate": 0},
                            "nodes": [{"name": "prov", "url": self.url, "type": "openai", "api_key_env": KEY_ENV,
                                       "cost": {"input": 1.0, "output": 2.0},
                                       "models": {"general": ["fast-1"]}}]})
        stats = Stats(Path(self.tmp.name) / "stats.json")
        fleet = Fleet(cfg, stats)
        engine = Engine(cfg, fleet, stats, Workspace([self.tmp.name]))
        result = asyncio.run(engine.delegate(TaskSpec(instruction="say hello", category="general",
                                                      max_output_tokens=500)))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["model"], "fast-1")
        today = fleet.spend.today("prov")
        self.assertEqual((today["requests"], today["tokens_in"], today["tokens_out"]), (1, 120, 30))
        self.assertAlmostEqual(today["cost_usd"], (120 * 1.0 + 30 * 2.0) / 1_000_000)


class McpAddAiTests(ProviderTestCase):
    """`fleet_nodes add` for a provider, driven through the server the way Claude drives it."""

    def call_server(self, requests: list[dict], config: Path) -> dict:
        env = {**os.environ, "ROUTEAI_HOME": self.tmp.name, "ROUTEAI_CONFIG": str(config),
               "ROUTEAI_PROJECT_DIR": self.tmp.name}
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "serve"], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=90)
        return {m["id"]: m for m in map(json.loads, proc.stdout.splitlines()) if "id" in m}

    def test_provider_is_added_with_consent_and_limits(self):
        config = Path(self.tmp.name) / "fleet.toml"
        config.write_text('[[nodes]]\nname = "local"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
        add = {"action": "add", "name": "freeai", "provider": "custom", "url": self.url,
               "api_key_env": KEY_ENV, "send_files": False, "daily_requests": 1500,
               "models": {"docs": ["fast-1"], "general": "fast-1"}}
        replies = self.call_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "fleet_nodes", "arguments": add}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "fleet_nodes", "arguments": add}},  # same name again: must be refused
        ], config)

        report = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertFalse(replies[2]["result"]["isError"], report)
        self.assertTrue(report["probe"]["reachable"], report["probe"])
        self.assertEqual(report["probe"]["installed"], ["fast-1", "smart-1"])

        written = tomllib.loads(config.read_text(encoding="utf-8"))
        node = next(n for n in written["nodes"] if n["name"] == "freeai")
        self.assertEqual((node["type"], node["api_key_env"], node["send_files"]), ("openai", KEY_ENV, False))
        self.assertEqual(node["limits"]["daily_requests"], 1500)
        self.assertEqual(node["models"], {"docs": ["fast-1"], "general": ["fast-1"]})
        self.assertNotIn(os.environ[KEY_ENV], config.read_text(encoding="utf-8"))  # the key is never stored
        self.assertEqual([n["name"] for n in written["nodes"]], ["local", "freeai"])

        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("already exists", replies[3]["result"]["content"][0]["text"])

    def test_remote_without_key_env_is_refused(self):
        config = Path(self.tmp.name) / "fleet.toml"
        config.write_text('[[nodes]]\nname = "local"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
        replies = self.call_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fleet_nodes", "arguments": {
                "action": "add", "name": "nokey", "provider": "custom", "url": self.url}}},
        ], config)
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("api_key_env", replies[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
