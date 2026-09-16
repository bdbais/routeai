"""Engine end-to-end against a stub Ollama server: delegate, file output, JSON, batch, feedback."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routeai.config import parse_config  # noqa: E402
from routeai.engine import Engine, TaskSpec, brief_tokens  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.stats import Stats  # noqa: E402
from routeai.workspace import Workspace  # noqa: E402


class StubOllama(BaseHTTPRequestHandler):
    chats = 0

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        bodies = {
            "/api/version": {"version": "0.0-stub"},
            "/api/tags": {"models": [{"name": "coder:7b", "capabilities": ["completion"]}]},
            "/api/ps": {"models": []},
        }
        self._send(bodies.get(self.path, {}))

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).chats += 1
        if req.get("format"):
            content = json.dumps({"answer": 42})
        else:
            content = "Sure:\n```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```\n"
        self._send({"message": {"content": content}, "eval_count": 20, "eval_duration": 1_000_000_000,
                    "prompt_eval_count": 50, "prompt_eval_duration": 100_000_000, "load_duration": 0})

    def log_message(self, *args):
        pass


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ROUTEAI_HOME")
        os.environ["ROUTEAI_HOME"] = str(Path(self.tmp.name) / "home")
        self.project = Path(self.tmp.name) / "project"
        (self.project / "src").mkdir(parents=True)
        for name in ("a", "b", "c"):
            (self.project / "src" / f"{name}.py").write_text(f"# module {name}\n", encoding="utf-8")
        port = self.server.server_address[1]
        cfg = parse_config({"fleet": {"explore_rate": 0},
                            "nodes": [{"name": "stub", "url": f"http://127.0.0.1:{port}", "max_parallel": 2,
                                       "models": {"general": ["coder:7b"]}}]})
        self.stats = Stats(Path(self.tmp.name) / "home" / "stats.json")
        self.engine = Engine(cfg, Fleet(cfg, self.stats), self.stats, Workspace([self.project]))

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("ROUTEAI_HOME", None)
        else:
            os.environ["ROUTEAI_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_delegate_writes_file_and_learns(self):
        async def scenario():
            spec = TaskSpec(instruction="write add()", category="code", files=["src/a.py"], output_path="out/add.py")
            res = await self.engine.delegate(spec)
            self.assertEqual(res["status"], "ok", res)
            self.assertEqual(res["written"], "out/add.py")
            text = (self.project / "out" / "add.py").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("def add("), text)
            saved = res["tokens_saved"]
            this = saved["this_task"]
            self.assertEqual(this["claude_output_tokens"], 20 - brief_tokens(spec))  # 20 = eval_count of the stub
            self.assertEqual(this["total"], this["claude_input_tokens"] + this["claude_output_tokens"])
            self.assertEqual(saved["session_total"], this["total"])
            self.assertEqual(saved["all_time_total"], this["total"])
            self.assertNotIn("routing", res)
            fb = self.engine.feedback(res["task_id"], "good")
            self.assertEqual(fb["samples"], 1)
            return res

        asyncio.run(scenario())
        snap = self.stats.snapshot()
        self.assertEqual(snap["totals"]["tasks"], 1)
        self.assertGreater(snap["speed"]["stub|coder:7b"]["gen_tps"], 0)

    def test_json_schema(self):
        res = asyncio.run(self.engine.delegate(TaskSpec(
            instruction="answer", category="general", json_schema={"type": "object"})))
        self.assertEqual(res["json"], {"answer": 42})

    def test_batch(self):
        async def scenario():
            paths, shared = self.engine.expand_batch(["src/*.py"], [])
            job = self.engine.start_batch(TaskSpec(instruction="document", category="docs"),
                                          paths, "docs/{stem}.py", shared, ["src/*.py"])
            await job.task
            return job

        job = asyncio.run(scenario())
        self.assertEqual((job.state, job.done, job.total), ("done", 3, 3))
        self.assertTrue((self.project / "docs" / "c.py").exists())
        report = self.engine.job_savings(job)
        self.assertEqual(report["this_task"]["tasks"], 3)
        self.assertGreater(job.overhead_tokens, 0)  # the brief is charged once for the whole batch
        self.assertEqual(report["this_task"]["total"], self.engine.session_saved)
        totals = self.stats.snapshot()["totals"]
        self.assertEqual(totals["saved_input"] + totals["saved_output"], self.engine.session_saved)
        self.assertNotIn("results", job.summary())  # all ok: a poll returns counts only
        self.assertEqual(len(job.summary(include_outputs=True)["results"]), 3)

    def test_batch_rejects_bad_pattern_and_protected_targets(self):
        paths, shared = self.engine.expand_batch(["src/*.py"], [])
        for pattern in ("docs/{bogus}.md", ".git/hooks/{stem}"):
            with self.assertRaises(Exception, msg=pattern) as ctx:
                self.engine.start_batch(TaskSpec(instruction="x"), paths, pattern, shared, ["src/*.py"])
            self.assertIn("WorkspaceError", type(ctx.exception).__name__)

    def test_outside_path_rejected(self):
        res = asyncio.run(self.engine.delegate(TaskSpec(instruction="x", output_path="../escape.py")))
        self.assertEqual(res["status"], "failed")
        self.assertIn("outside", res["error"])


if __name__ == "__main__":
    unittest.main()
