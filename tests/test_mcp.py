"""End-to-end check of the stdio MCP server against a fleet with an unreachable node."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpProtocolTests(unittest.TestCase):
    def test_initialize_list_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "fleet.toml"
            cfg.write_text('[[nodes]]\nname = "ghost"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
            env = {**os.environ, "ROUTEAI_HOME": tmp, "ROUTEAI_CONFIG": str(cfg),
                   "ROUTEAI_PROJECT_DIR": tmp}
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fleet_status", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "fleet_delegate", "arguments": {"instruction": "say hi", "files": ["../../etc/passwd"]}}},
                {"jsonrpc": "2.0", "id": 5, "method": "nope"},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "fleet_setup", "arguments": {
                    "nodes": [{"name": "ghost2", "url": "http://127.0.0.1:9"}], "overwrite": True}}},
            ]
            stdin = "".join(json.dumps(r) + "\n" for r in requests)
            proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "serve"], input=stdin, capture_output=True,
                                  text=True, encoding="utf-8", env=env, timeout=60)
            replies = {m["id"]: m for m in map(json.loads, proc.stdout.splitlines()) if "id" in m}

        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in replies[2]["result"]["tools"]}
        self.assertTrue({"fleet_status", "fleet_setup", "fleet_delegate", "fleet_delegate_batch", "fleet_job",
                         "fleet_feedback", "fleet_bench", "fleet_pull"} <= names)
        status = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertFalse(status["nodes"][0]["healthy"])
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("outside the allowed roots", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["error"]["code"], -32601)
        setup = json.loads(replies[6]["result"]["content"][0]["text"])
        self.assertFalse(replies[6]["result"]["isError"], setup)
        self.assertTrue(setup["written"].endswith("fleet.toml"))
        self.assertTrue(setup["backup"])
        self.assertFalse(setup["nodes"][0]["reachable"])


if __name__ == "__main__":
    unittest.main()
