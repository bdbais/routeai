"""Regressions for the pre-release security and robustness review."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai.config import is_cloud_model, parse_config  # noqa: E402
from routeai.engine import Engine, TaskSpec  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.ollama_client import OllamaClient, OllamaError  # noqa: E402
from routeai.prompts import extract_file_content, looks_like_echo  # noqa: E402
from routeai.stats import Stats  # noqa: E402
from routeai.workspace import Workspace, WorkspaceError  # noqa: E402


class StatsRobustnessTests(unittest.TestCase):
    def test_unreadable_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            stats = Stats(path)
            stats.add_totals(10, 5, 1, 3, 4)
            path.write_text("{broken", encoding="utf-8")
            later = time.time() + 5
            os.utime(path, (later, later))
            stats.add_totals(1, 1, 1, 1, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_two_writers_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            a, b = Stats(path), Stats(path)  # like two MCP servers for two sessions
            for _ in range(5):
                a.add_totals(1, 0, 0)
                b.add_totals(1, 0, 0)
            self.assertEqual(Stats(path).snapshot()["totals"]["tasks"], 10)
            self.assertFalse(list(Path(tmp).glob("*.lock")))


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel in ("config/app.toml", "config/.npmrc", ".env", "keys/server.pem", ".github/workflows/ci.yml",
                    "src/a.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x", encoding="utf-8")
        self.ws = Workspace([self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def rels(self, patterns):
        return sorted(self.ws.rel(p) for p in self.ws.expand(patterns))

    def test_globs_skip_secrets_and_hidden_files(self):
        self.assertEqual(self.rels(["config/*"]), ["config/app.toml"])
        everything = self.rels(["**/*"])
        self.assertNotIn(".env", everything)
        self.assertNotIn("keys/server.pem", everything)
        self.assertEqual(self.rels([".github/workflows/*.yml"]), [".github/workflows/ci.yml"])
        self.assertEqual(self.rels([".env"]), [".env"])  # explicit paths are the caller's choice

    def test_protected_write_locations(self):
        for bad in (".git/hooks/pre-commit", ".claude/settings.json", "node_modules/x.js", ".vscode/tasks.json"):
            with self.assertRaises(WorkspaceError, msg=bad):
                self.ws.write(bad, "x")
        self.ws.write(".github/workflows/new.yml", "on: push\n")

    def test_network_and_escaping_paths(self):
        for bad in ("\\\\attacker\\share\\x.py", "//attacker/share/x.py", "../../etc/passwd", "src/../../x"):
            with self.assertRaises(WorkspaceError, msg=bad):
                self.ws.resolve(bad)


class OutputSafetyTests(unittest.TestCase):
    def test_unclosed_fence_is_refused(self):
        with self.assertRaises(ValueError):
            extract_file_content("Here:\n```python\ndef f():\n    return", "a.py")
        with self.assertRaises(ValueError):
            extract_file_content("```markdown\n# Title\nhalf", "README.md")

    def test_echoed_prompt_is_refused(self):
        instruction = "Write a short Markdown reference for this module: one heading and a bullet per function."
        echo = f"## Task\n{instruction}\n\n## File: queue.py\n```python\nimport json\n```\n"
        self.assertTrue(looks_like_echo(echo, instruction))
        self.assertTrue(looks_like_echo(echo.replace("## Task", "##  Task "), instruction))
        self.assertFalse(looks_like_echo("# Queue\n\nA durable work queue.\n\n- `enqueue(spec)`\n", instruction))

    def test_cloud_detection_ignores_case(self):
        self.assertTrue(is_cloud_model("qwen3-coder:480B-Cloud"))
        self.assertTrue(is_cloud_model("GLM:CLOUD"))


class Stub(BaseHTTPRequestHandler):
    def _send(self, code, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/redirect/api/version":
            self._send(302, {}, [("Location", "http://127.0.0.1:9/steal")])
        elif self.path == "/api/tags":
            self._send(200, {"models": [{"name": "coder:7b", "capabilities": ["completion"]},
                                        {"name": "alias:latest", "capabilities": ["completion"],
                                         "remote_host": "https://ollama.com:443"}]})
        else:
            self._send(200, {"version": "stub", "models": []})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._send(200, {"message": {"content": "```python\ndef f():\n    return 1\n"}, "done_reason": "length",
                         "eval_count": 10, "eval_duration": 10**9})

    def log_message(self, *args):
        pass


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_redirects_are_not_followed(self):
        client = OllamaClient(self.url + "/redirect", headers={"Authorization": "Bearer secret"})
        with self.assertRaises(OllamaError) as ctx:
            asyncio.run(client.version())
        self.assertIn("302", str(ctx.exception))

    def test_truncated_answer_writes_nothing_and_remote_alias_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("ROUTEAI_HOME")
            os.environ["ROUTEAI_HOME"] = tmp
            try:
                cfg = parse_config({"fleet": {"explore_rate": 0}, "nodes": [
                    {"name": "stub", "url": self.url, "models": {"general": ["alias:latest", "coder:7b"]}}]})
                stats = Stats(Path(tmp) / "stats.json")
                fleet = Fleet(cfg, stats)
                engine = Engine(cfg, fleet, stats, Workspace([tmp]))
                res = asyncio.run(engine.delegate(TaskSpec(instruction="x", category="code", output_path="f.py")))
            finally:
                if old is None:
                    os.environ.pop("ROUTEAI_HOME", None)
                else:
                    os.environ["ROUTEAI_HOME"] = old
            self.assertEqual(res["status"], "failed", res)
            self.assertIn("cut off", res["error"])
            self.assertEqual(res["model"], "coder:7b")  # the ollama.com alias was never used
            self.assertFalse((Path(tmp) / "f.py").exists())


class McpMalformedInputTests(unittest.TestCase):
    def test_server_survives_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "fleet.toml"
            cfg.write_text('[[nodes]]\nname = "ghost"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
            env = {**os.environ, "ROUTEAI_HOME": tmp, "ROUTEAI_CONFIG": str(cfg),
                   "ROUTEAI_PROJECT_DIR": tmp}
            lines = [b"123", b"null", b"[1]", b"\xff\xfe garbage", b"{not json",
                     b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":[1]}',
                     b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fleet_delegate","arguments":{}}}',
                     b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fleet_job","arguments":[1]}}',
                     b'{"jsonrpc":"2.0","id":5,"method":"tools/list"}']
            proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "serve"], input=b"\n".join(lines) + b"\n",
                                  capture_output=True, env=env, timeout=60)
            replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
        by_id = {r.get("id"): r for r in replies if r.get("id") is not None}
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace")[-500:])
        self.assertEqual(by_id[2]["error"]["code"], -32602)
        self.assertIn("missing required", by_id[3]["result"]["content"][0]["text"])
        self.assertEqual(by_id[4]["error"]["code"], -32602)
        self.assertTrue(by_id[5]["result"]["tools"])


if __name__ == "__main__":
    unittest.main()
