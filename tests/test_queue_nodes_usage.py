"""The work queue, the per-node usage report and surgical node editing."""

import asyncio
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai import queue as work_queue  # noqa: E402
from routeai.config import parse_config  # noqa: E402
from routeai.engine import TaskResult, queue_worker, usage_log  # noqa: E402
from routeai.setup import add_node, node_blocks, remove_node, set_node_option  # noqa: E402

# Windows folds path case, POSIX does not: os.path.normcase is the rule the report follows.
PATHS_IGNORE_CASE = os.path.normcase("A") == "a"

SAMPLE = '''# my fleet
[fleet]
routing = "priority"

# the fast one
[[nodes]]
name = "gpu"
url = "http://192.168.1.13:11434"
tier = "heavy"
num_ctx = 8192

[nodes.models]
code = ["big:14b"]

[[nodes]]
name = "laptop"
url = "http://localhost:11434"
tier = "light"

[nodes.models]
docs = ["small:3b"]

# [[nodes]]
# name = "rented"
'''


class NodeEditingTests(unittest.TestCase):
    def test_blocks_are_found(self):
        self.assertEqual([n for n, _, _ in node_blocks(SAMPLE)], ["gpu", "laptop"])

    def test_remove_keeps_the_others_and_the_comments(self):
        text = remove_node(SAMPLE, "gpu")
        cfg = parse_config(tomllib.loads(text))
        self.assertEqual([n.name for n in cfg.nodes], ["laptop"])
        self.assertIn("# my fleet", text)
        self.assertEqual(cfg.node("laptop").models_for("docs"), ["small:3b"])
        with self.assertRaises(ValueError):
            remove_node(text, "gpu")

    def test_add_probed_node_with_options(self):
        probed = {"name": "rented", "url": "https://ollama.example.net", "reachable": True,
                  "models": {"complex": ["big:30b"]}}
        text = add_node(SAMPLE, probed, {"tier": "heavy", "max_parallel": 2})
        cfg = parse_config(tomllib.loads(text))
        self.assertEqual([n.name for n in cfg.nodes], ["gpu", "laptop", "rented"])
        self.assertEqual((cfg.node("rented").tier, cfg.node("rented").max_parallel), ("heavy", 2))
        self.assertEqual(cfg.node("rented").models_for("complex"), ["big:30b"])
        self.assertEqual(cfg.node("gpu").models_for("code"), ["big:14b"])  # untouched
        with self.assertRaises(ValueError):
            add_node(text, probed)

    def test_enable_and_disable(self):
        text = set_node_option(SAMPLE, "laptop", "enabled", False)
        cfg = parse_config(tomllib.loads(text))
        self.assertFalse(cfg.node("laptop").enabled)
        self.assertTrue(cfg.node("gpu").enabled)
        self.assertEqual(cfg.node("laptop").models_for("docs"), ["small:3b"])
        back = set_node_option(text, "laptop", "enabled", True)
        self.assertTrue(parse_config(tomllib.loads(back)).node("laptop").enabled)
        with self.assertRaises(ValueError):
            set_node_option(SAMPLE, "nope", "enabled", False)


class FleetHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ROUTEAI_HOME")
        os.environ["ROUTEAI_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ROUTEAI_HOME", None)
        else:
            os.environ["ROUTEAI_HOME"] = self.old
        self.tmp.cleanup()


class QueueTests(FleetHomeTest):
    def test_claim_is_exclusive_and_results_are_kept(self):
        work_queue.enqueue({"instruction": "first", "output_path": "a.py"}, root=self.tmp.name, note="overnight")
        work_queue.enqueue({"instruction": "second"}, root=self.tmp.name)
        self.assertEqual(work_queue.snapshot()["pending"], 2)

        first_path, first = work_queue.claim()
        second_path, second = work_queue.claim()
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNone(work_queue.claim())
        self.assertEqual(work_queue.snapshot()["running"], 2)

        work_queue.complete(first_path, {"ok": True, "written": "a.py", "task_id": "abc"})
        work_queue.complete(second_path, {"ok": False, "error": "nope"})
        snapshot = work_queue.snapshot()
        self.assertEqual((snapshot["pending"], snapshot["running"], snapshot["done"]), (0, 0, 2))
        self.assertEqual({row["result"]["ok"] for row in snapshot["finished"]}, {True, False})
        self.assertIn("overnight", [row.get("note") for row in snapshot["finished"]])

    def test_stale_claims_come_back(self):
        work_queue.enqueue({"instruction": "x"}, root=self.tmp.name)
        running, _ = work_queue.claim()
        old = time.time() - 3600
        os.utime(running, (old, old))
        self.assertEqual(work_queue.requeue_stale(), 1)
        self.assertEqual(work_queue.snapshot()["pending"], 1)

    def test_cancel_and_clear(self):
        item = work_queue.enqueue({"instruction": "x"}, root=self.tmp.name)
        self.assertTrue(work_queue.cancel(item["id"]))
        self.assertEqual(work_queue.snapshot()["pending"], 0)
        work_queue.enqueue({"instruction": "y"}, root=self.tmp.name)
        self.assertEqual(work_queue.clear("pending"), 1)

    def test_worker_runs_queued_tasks(self):
        work_queue.enqueue({"instruction": "write docs", "category": "docs"}, root=self.tmp.name)
        work_queue.enqueue({"instruction": "boom", "category": "code"}, root=self.tmp.name)

        class FakeEngine:
            async def run_task(self, spec):
                if spec.instruction == "boom":
                    raise RuntimeError("worker must survive this")
                return TaskResult(task_id="t1", ok=True, category=spec.category, node="gpu", model="m",
                                  output="done", written="docs/x.md")

        handled = asyncio.run(queue_worker(lambda root: FakeEngine(), once=True))
        snapshot = work_queue.snapshot()
        self.assertEqual((handled, snapshot["pending"], snapshot["done"]), (2, 0, 2))
        results = [row["result"] for row in snapshot["finished"]]
        self.assertTrue(any(r.get("ok") for r in results))
        self.assertTrue(any("RuntimeError" in str(r.get("error", "")) for r in results))


class UsageReportTests(FleetHomeTest):
    def write_log(self, rows):
        path = Path(self.tmp.name) / "tasks.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    def test_tokens_per_node_and_project_filter(self):
        now = time.time()
        # on Windows the second row is the same project written differently; elsewhere it is the same string
        SAME_PROJECT_OTHER_CASE = r"c:\WORK\app" if PATHS_IGNORE_CASE else r"C:\work\app"
        log = self.write_log([
            {"ts": now, "project": r"C:\work\app", "ok": True, "category": "tests", "node": "gpu",
             "model": "big:14b", "tokens_in": 1000, "tokens_out": 500, "seconds": 10.0,
             "saved": {"total": 900, "claude_input_tokens": 400, "claude_output_tokens": 500}},
            {"ts": now, "project": SAME_PROJECT_OTHER_CASE, "ok": True, "category": "docs", "node": "laptop",
             "model": "small:3b", "tokens_in": 200, "tokens_out": 100, "seconds": 30.0,
             "saved": {"total": -50, "claude_input_tokens": -150, "claude_output_tokens": 100}},
            {"ts": now, "project": r"C:\work\other", "ok": False, "category": "code", "node": "gpu",
             "model": "big:14b", "tokens_in": 50, "tokens_out": 0, "seconds": 2.0},
        ])
        report = usage_log(log, project=r"C:\work\app")
        totals = report["totals"]
        self.assertEqual(totals["tasks"], 2)
        self.assertEqual(totals["tokens_total"], 1800)
        self.assertEqual(totals["claude_tokens_saved"], 850)
        self.assertEqual(report["tasks_that_cost_more_than_they_saved"], 1)
        by_node = report["usage_by_node"]
        self.assertEqual(list(by_node), ["gpu", "laptop"])  # sorted by tokens
        self.assertEqual(by_node["gpu"]["tokens_total"], 1500)
        self.assertEqual(by_node["gpu"]["models"]["big:14b"]["tasks"], 1)
        self.assertEqual(by_node["laptop"]["model_seconds"], 30.0)

        everything = usage_log(log)
        self.assertEqual(everything["totals"]["tasks"], 3)
        self.assertEqual(everything["totals"]["failed"], 1)
        self.assertEqual(everything["usage_by_node"]["gpu"]["tasks"], 2)

    @unittest.skipUnless(PATHS_IGNORE_CASE, "path case is only folded on Windows")
    def test_project_filter_folds_case_on_windows(self):
        log = self.write_log([
            {"ts": time.time(), "project": r"C:\work\app", "ok": True, "node": "gpu", "tokens_in": 10},
            {"ts": time.time(), "project": r"c:\WORK\APP", "ok": True, "node": "gpu", "tokens_in": 10},
        ])
        self.assertEqual(usage_log(log, project=r"C:\WORK\app")["totals"]["tasks"], 2)

    def test_project_filter_is_exact_on_posix(self):
        if PATHS_IGNORE_CASE:
            self.skipTest("path case is folded on Windows")
        log = self.write_log([
            {"ts": time.time(), "project": "/home/me/app", "ok": True, "node": "gpu", "tokens_in": 10},
            {"ts": time.time(), "project": "/home/me/APP", "ok": True, "node": "gpu", "tokens_in": 10},
        ])
        self.assertEqual(usage_log(log, project="/home/me/app")["totals"]["tasks"], 1)

    def test_days_window_and_untagged_rows(self):
        log = self.write_log([
            {"ts": time.time() - 86400 * 5, "project": "p", "ok": True, "node": "gpu", "tokens_in": 10},
            {"ts": time.time(), "ok": True, "node": "gpu", "tokens_in": 20},  # no project: older format
        ])
        recent = usage_log(log, project="p", days=1)
        self.assertEqual(recent["totals"]["tasks"], 0)
        self.assertEqual(recent["older_tasks_without_a_project_tag"], 1)
        self.assertEqual(usage_log(log)["totals"]["tasks"], 2)


if __name__ == "__main__":
    unittest.main()
