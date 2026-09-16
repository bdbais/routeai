"""Live end-to-end test: drives the real MCP server against your real Ollama fleet.

Skipped unless ROUTEAI_LIVE=1. Uses your fleet.toml (or ROUTEAI_CONFIG) but a
temporary ROUTEAI_HOME and project, so your learned stats are not touched.

    ROUTEAI_LIVE=1 python -m unittest tests.test_live -v
"""

import ast
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai.bench.grader import _run_tests  # noqa: E402
from routeai.config import config_path  # noqa: E402

CALC = '''def clamp(value: float, low: float, high: float) -> float:
    """Clamp value into [low, high]. Raises ValueError if low > high."""
    if low > high:
        raise ValueError("low > high")
    return max(low, min(value, high))
'''

SMALL_MODULES = {
    "slug.py": "def slugify(text: str) -> str:\n    return '-'.join(text.lower().split())\n",
    "stats.py": "def mean(xs: list[float]) -> float:\n    return sum(xs) / len(xs)\n",
    "greet.py": "def greet(name: str, excited: bool = False) -> str:\n    return f'Hello {name}' + ('!' if excited else '.')\n",
}


class McpClient:
    def __init__(self, env: dict, cwd: str):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "run.py"), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=cwd, bufsize=1,
        )
        self.inbox: queue.Queue = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.inbox.put(json.loads(line))

    def request(self, method: str, params: dict | None = None, timeout: float = 900) -> dict:
        self.next_id += 1
        msg_id = self.next_id
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            msg = self.inbox.get(timeout=max(1.0, deadline - time.time()))
            if msg.get("id") == msg_id:
                return msg

    def call(self, name: str, args: dict, timeout: float = 900):
        result = self.request("tools/call", {"name": name, "arguments": args}, timeout)["result"]
        text = result["content"][0]["text"]
        try:
            return result.get("isError", False), json.loads(text)
        except json.JSONDecodeError:
            return result.get("isError", False), text

    def wait_job(self, job_id: str, timeout: float = 1800) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job = self.call("fleet_job", {"job_id": job_id, "include_outputs": True})
            if job["state"] != "running":
                return job
            time.sleep(5)
        raise TimeoutError(job_id)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=60)


@unittest.skipUnless(os.environ.get("ROUTEAI_LIVE") == "1", "set ROUTEAI_LIVE=1 to run against real nodes")
class LiveFleetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tmp.name) / "project"
        cls.project.mkdir()
        (cls.project / "calc.py").write_text(CALC, encoding="utf-8")
        for name, src in SMALL_MODULES.items():
            (cls.project / "lib").mkdir(exist_ok=True)
            (cls.project / "lib" / name).write_text(src, encoding="utf-8")
        env = {**os.environ, "ROUTEAI_HOME": str(Path(cls.tmp.name) / "home"),
               "ROUTEAI_CONFIG": os.environ.get("ROUTEAI_CONFIG", str(config_path())),
               "ROUTEAI_PROJECT_DIR": str(cls.project)}
        cls.client = McpClient(env, cwd=cls.tmp.name)
        init = cls.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "live-test", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "routeai"
        cls.state: dict = {}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_01_status_and_tier_routing(self):
        err, status = self.client.call("fleet_status", {})
        self.assertFalse(err, status)
        healthy = {n["name"]: n["tier"] for n in status["nodes"] if n["healthy"]}
        self.assertTrue(healthy, status["nodes"])
        self.assertEqual(status["workspace_roots"][0], str(self.project.resolve()))
        preview = status["routing_preview"]
        if "heavy" in healthy.values() and "light" in healthy.values():
            self.assertEqual(healthy[preview["complex"]["node"]], "heavy", preview)
            self.assertEqual(healthy[preview["tests"]["node"]], "light", preview)
        print("\nrouting:", {k: f"{v['model']}@{v['node']}" for k, v in preview.items() if isinstance(v, dict)})

    def test_02_delegate_tests_to_file(self):
        err, res = self.client.call("fleet_delegate", {
            "instruction": "Write pytest tests for clamp(): values inside, below and above the range, the "
                           "boundaries, and ValueError when low > high. Import with `from calc import clamp`.",
            "category": "tests", "files": ["calc.py"], "output_path": "tests/test_calc.py",
            "max_output_tokens": 900, "wait_seconds": 600,
        })
        self.assertFalse(err, res)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["written"], "tests/test_calc.py")
        code = (self.project / "tests" / "test_calc.py").read_text(encoding="utf-8")
        ast.parse(code)
        self.assertIn("def test", code)
        run = _run_tests(code, "calc", CALC)
        saved = res["tokens_saved"]
        self.assertGreater(saved["this_task"]["claude_output_tokens"], 0)
        print(f"\ntests by {res['model']}@{res['node']} in {res['seconds']}s: {run.get('passed')} passed, "
              f"{run.get('failed')} failed; Claude tokens saved: {saved['this_task']} "
              f"(session {saved['session_total']})")
        self.state["task_id"] = res["task_id"]

    def test_03_long_task_hands_back_job(self):
        err, res = self.client.call("fleet_delegate", {
            "instruction": "Write a Python module with an `IntervalSet` class: add(start, end) merging overlaps, "
                           "remove(start, end), __contains__(x), __iter__ over sorted (start, end) tuples. "
                           "Half-open intervals. Type hints, no dependencies.",
            "category": "complex", "output_path": "interval_set.py", "max_output_tokens": 1200,
            "wait_seconds": 1,
        })
        self.assertFalse(err, res)
        if res.get("status") == "running":
            job = self.client.wait_job(res["job_id"])
            self.assertEqual(job["state"], "done", job)
            res = job["results"][0]
        self.assertTrue(res["ok"] if "ok" in res else res["status"] == "ok", res)
        ast.parse((self.project / "interval_set.py").read_text(encoding="utf-8"))
        print(f"\ncomplex by {res['model']}@{res['node']}")

    def test_04_structured_json(self):
        err, res = self.client.call("fleet_delegate", {
            "instruction": "Extract the fields from: 'Ticket 4411 opened by Luca Bianchi (luca@example.org), priority high.'",
            "category": "general",
            "json_schema": {"type": "object", "properties": {
                "ticket": {"type": "integer"}, "name": {"type": "string"}, "email": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]}},
                "required": ["ticket", "name", "email", "priority"]},
            "wait_seconds": 600,
        })
        self.assertFalse(err, res)
        self.assertEqual(res["json"]["ticket"], 4411, res)
        self.assertEqual(res["json"]["priority"], "high")

    def test_05_batch_across_nodes(self):
        err, res = self.client.call("fleet_delegate_batch", {
            "instruction": "Write a short Markdown reference for this module: one heading, a one-line summary "
                           "and a bullet per function with its signature and behaviour.",
            "files": ["lib/*.py"], "category": "docs", "output_pattern": "docs/{stem}.md", "max_output_tokens": 400,
        })
        self.assertFalse(err, res)
        self.assertEqual(res["total"], 3)
        job = self.client.wait_job(res["job_id"])
        self.assertEqual((job["state"], job["done"]), ("done", 3), job)
        for stem in ("slug", "stats", "greet"):
            self.assertTrue((self.project / "docs" / f"{stem}.md").read_text(encoding="utf-8").strip())
        self.assertEqual(job["tokens_saved"]["this_task"]["tasks"], 3)
        print("\nbatch nodes:", sorted({r["node"] for r in job["results"]}),
              "; Claude tokens saved:", job["tokens_saved"])

    def test_06_feedback(self):
        task_id = self.state.get("task_id")
        if not task_id:
            self.skipTest("no task from test_02")
        err, res = self.client.call("fleet_feedback", {"task_id": task_id, "verdict": "fixed"})
        self.assertFalse(err, res)
        self.assertTrue(res["recorded"])

    def test_07_bench_subset(self):
        err, res = self.client.call("fleet_bench", {"mode": "quick", "categories": ["general"]})
        self.assertFalse(err, res)
        job = self.client.wait_job(res["job_id"])
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(job["report"]["table"], job)
        self.assertTrue(Path(job["report"]["report_path"]).exists())

    def test_08_pull_installed_model_is_quick(self):
        err, status = self.client.call("fleet_status", {})
        node = next(n for n in status["nodes"] if n["healthy"])
        model = status["routing_preview"]["general"]["model"]
        err, res = self.client.call("fleet_pull", {"node": node["name"], "model": model})
        self.assertFalse(err, res)
        job = self.client.wait_job(res["job_id"], timeout=600)
        self.assertEqual(job["state"], "done", job)

    def test_09_errors_are_clean(self):
        err, res = self.client.call("fleet_delegate", {"instruction": "x", "files": ["missing.py"]})
        self.assertTrue(err)
        self.assertIn("not found", res["error"])
        err, res = self.client.call("fleet_job", {"job_id": "nope"})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
