"""The plugin as Claude Code sees it: manifests, skills, and the MCP command launched the way Claude Code launches it."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def launch(env_overrides: dict, cwd: str) -> dict:
    """Start the server exactly as plugin.json says, with Claude Code's variables substituted."""
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    spec = manifest["mcpServers"]["fleet"]
    subst = {"${CLAUDE_PLUGIN_ROOT}": str(ROOT), **env_overrides.pop("_subst", {})}

    def expand(value: str) -> str:
        for k, v in subst.items():
            value = value.replace(k, v)
        return value

    command = expand(spec["command"])
    if os.name == "nt":
        command += ".cmd"  # what Windows' PATHEXT resolution picks for the extension-less launcher
    args = [expand(a) for a in spec["args"]]
    env = {**os.environ, **{k: expand(v) for k, v in spec.get("env", {}).items()}, **env_overrides}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "x"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fleet_status", "arguments": {}}},
    ]
    proc = subprocess.run([command, *args], input="".join(json.dumps(r) + "\n" for r in requests),
                          capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd, timeout=60)
    return {m["id"]: m for m in map(json.loads, proc.stdout.splitlines()) if "id" in m}


class ManifestTests(unittest.TestCase):
    def test_plugin_json(self):
        data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertRegex(data["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init = (ROOT / "src" / "routeai" / "__init__.py").read_text(encoding="utf-8")
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertIn(f'version = "{data["version"]}"', pyproject)
        self.assertIn(f'__version__ = "{data["version"]}"', init)
        self.assertEqual(market["plugins"][0]["version"], data["version"])
        self.assertEqual(market["plugins"][0]["name"], data["name"])

    def test_no_project_level_mcp_json(self):
        # A root .mcp.json is also read as a *project* server when the repo itself is opened in
        # Claude Code, where ${CLAUDE_PLUGIN_ROOT} is undefined and the server fails to start.
        self.assertFalse((ROOT / ".mcp.json").exists())

    def test_skills_frontmatter(self):
        skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual([s.parent.name for s in skills], ["add-ai", "bench", "delegate", "nodes", "queue", "setup", "status", "usage"])
        for path in skills:
            text = path.read_text(encoding="utf-8")
            m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            self.assertIsNotNone(m, path)
            fields = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
            self.assertEqual(fields["name"].strip(), path.parent.name)
            self.assertGreater(len(fields["description"].strip()), 40)

    def test_skills_reference_real_tools(self):
        tools = set(re.findall(r'@server\.tool\("(\w+)"', (ROOT / "src" / "routeai" / "server.py").read_text(encoding="utf-8")))
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            for name in re.findall(r"`(fleet_\w+)", path.read_text(encoding="utf-8")):
                self.assertIn(name, tools, f"{path.parent.name} mentions unknown tool {name}")

    def test_launch_like_claude_code_from_other_cwd(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            cfg = Path(elsewhere) / "fleet.toml"
            cfg.write_text('[[nodes]]\nname = "ghost"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
            replies = launch({"_subst": {"${CLAUDE_PROJECT_DIR}": project}, "ROUTEAI_HOME": elsewhere,
                              "ROUTEAI_CONFIG": str(cfg)}, cwd=elsewhere)
            self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "routeai")
            self.assertEqual(len(replies[2]["result"]["tools"]), 11)
            status = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(Path(status["workspace_roots"][0]), Path(project).resolve())

    def test_unexpanded_project_variable_falls_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as cwd:
            cfg = Path(cwd) / "fleet.toml"
            cfg.write_text('[[nodes]]\nname = "ghost"\nurl = "http://127.0.0.1:9"\n', encoding="utf-8")
            env = {"ROUTEAI_HOME": cwd, "ROUTEAI_CONFIG": str(cfg)}
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            replies = launch(env, cwd=cwd)  # ${CLAUDE_PROJECT_DIR} left literal
            status = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(Path(status["workspace_roots"][0]), Path(cwd).resolve())


if __name__ == "__main__":
    unittest.main()
