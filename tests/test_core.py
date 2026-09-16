import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routeai.config import ConfigError, is_cloud_model, parse_config  # noqa: E402
from routeai.engine import render_output_path  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.prompts import build_user_message, classify, extract_file_content  # noqa: E402
from routeai.stats import Stats  # noqa: E402
from routeai.workspace import Workspace, WorkspaceError  # noqa: E402

CFG = {
    "fleet": {"explore_rate": 0},
    "nodes": [
        {"name": "gpu", "url": "http://gpu:11434", "tier": "heavy",
         "models": {"complex": ["big-coder:30b"], "code": ["coder:14b"], "general": ["coder:14b"]}},
        {"name": "laptop", "url": "http://localhost:11434", "tier": "light",
         "models": {"complex": [], "scripts": ["small-coder:7b"], "general": ["small-coder:7b", "glm:cloud"]}},
    ],
}


def make_fleet(tmp: str, data: dict = CFG) -> Fleet:
    fleet = Fleet(parse_config(data), Stats(Path(tmp) / "stats.json"))
    for name, models in {"gpu": ["big-coder:30b", "coder:14b"], "laptop": ["small-coder:7b", "glm:cloud"]}.items():
        st = fleet.state[name]
        st.healthy = True
        st.installed = {m: {"name": m, "capabilities": ["completion"]} for m in models}
    return fleet


class ConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        cfg = parse_config({"nodes": [{"name": "a", "url": "http://a/"}], "categories": {"docs": {"prefer": "heavy"}}})
        self.assertEqual(cfg.nodes[0].url, "http://a")
        self.assertEqual(cfg.categories["docs"].prefer, "heavy")
        self.assertEqual(cfg.categories["tests"].prefer, "light")

    def test_invalid_tier(self):
        with self.assertRaises(ConfigError):
            parse_config({"nodes": [{"name": "a", "url": "http://a", "tier": "huge"}]})

    def test_empty_list_disables_category(self):
        node = parse_config(CFG).node("laptop")
        self.assertEqual(node.models_for("complex"), [])
        self.assertEqual(node.models_for("docs"), ["small-coder:7b", "glm:cloud"])

    def test_cloud_detection(self):
        self.assertTrue(is_cloud_model("glm-5.2:cloud"))
        self.assertTrue(is_cloud_model("gpt-oss:120b-cloud"))
        self.assertFalse(is_cloud_model("qwen3:8b"))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fleet = make_fleet(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_complex_goes_to_heavy_only(self):
        ranked = self.fleet.rank("complex", 1000, 500)
        self.assertEqual([c.node for c in ranked], ["gpu"])

    def test_scripts_prefer_light_even_if_slower(self):
        ranked = self.fleet.rank("scripts", 1000, 500)
        self.assertEqual(ranked[0].node, "laptop")

    def test_overflow_when_preferred_busy(self):
        self.fleet.active["laptop"] = 1  # max_parallel = 1
        ranked = self.fleet.rank("scripts", 1000, 500)
        self.assertEqual(ranked[0].node, "gpu")

    def test_cloud_models_skipped(self):
        models = {c.model for c in self.fleet.rank("general", 1000, 500)}
        self.assertNotIn("glm:cloud", models)

    def test_context_limit(self):
        self.assertEqual(self.fleet.rank("code", 9000, 500), [])

    def test_poor_model_loses_tier_precedence(self):
        self.fleet.stats.record_quality("laptop", "small-coder:7b", "scripts", 0.0, "bench")
        ranked = self.fleet.rank("scripts", 1000, 500)
        self.assertEqual(ranked[0].node, "gpu")
        self.assertIn("below quality floor", ranked[-1].reason)

    def test_learned_quality_reorders(self):
        for _ in range(3):
            self.fleet.stats.record_quality("gpu", "big-coder:30b", "complex", 0.2, "bench")
            self.fleet.stats.record_quality("gpu", "coder:14b", "complex", 0.9, "bench")
        data = {**CFG, "nodes": [dict(CFG["nodes"][0], models={"complex": ["big-coder:30b", "coder:14b"]}), CFG["nodes"][1]]}
        fleet = make_fleet(self.tmp.name, data)
        self.assertEqual(fleet.rank("complex", 1000, 500)[0].model, "coder:14b")


class WorkspaceTests(unittest.TestCase):
    def test_escape_and_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "b.py").write_text("y = 2\n")
            ws = Workspace([root])
            self.assertEqual([ws.rel(p) for p in ws.expand(["**/*.py"])], ["src/a.py"])
            with self.assertRaises(WorkspaceError):
                ws.resolve("../outside.txt")
            for bad in ("/src/**/*.py", "../*.py", "src/../../*.py"):  # found by a fleet-written test
                with self.assertRaises(WorkspaceError, msg=bad):
                    ws.expand([bad])
            written = ws.write("out/x.txt", "hi")
            self.assertEqual(written.read_text(), "hi")


class PromptTests(unittest.TestCase):
    def test_extract_largest_block(self):
        reply = "Here:\n```python\nx = 1\n```\nand\n```python\ndef f():\n    return 2\n```\n"
        self.assertIn("def f", extract_file_content(reply, "a.py"))

    def test_markdown_kept_raw(self):
        reply = "# Title\n\n```bash\nls\n```\n"
        self.assertTrue(extract_file_content(reply, "README.md").startswith("# Title"))

    def test_nested_fence_in_input(self):
        msg = build_user_message("do it", [("a.md", "```py\nx\n```")])
        self.assertIn("````markdown", msg)

    def test_classify(self):
        self.assertEqual(classify("Write pytest unit tests for parser.py"), "tests")
        self.assertEqual(classify("Add a docstring to every function"), "docs")

    def test_output_pattern(self):
        self.assertEqual(render_output_path("tests/test_{stem}.py", "src/pkg/mod.py"), "tests/test_mod.py")
        self.assertEqual(render_output_path("{dir}/{stem}.md", "mod.py"), "mod.md")


if __name__ == "__main__":
    unittest.main()
