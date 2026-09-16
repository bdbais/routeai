import asyncio
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routeai.config import parse_config  # noqa: E402
from routeai.setup import generate, parse_spec, render, suggest_models, suggested_pulls, write_config  # noqa: E402


def model(name, size, caps=("completion",)):
    return {"name": name, "details": {"parameter_size": size}, "capabilities": list(caps)}


INSTALLED = [
    model("qwen2.5-coder:14b", "14.8B"), model("qwen3-coder:30b", "30.5B"), model("qwen3:8b", "8.2B"),
    model("nomic-embed-text:latest", "137M", ("embedding",)), model("glm-5.2:cloud", "756B"),
]


class SetupTests(unittest.TestCase):
    def test_suggest_models(self):
        m = suggest_models(INSTALLED)
        self.assertEqual(m["code"], ["qwen2.5-coder:14b"])
        self.assertEqual(m["complex"], ["qwen3-coder:30b", "qwen2.5-coder:14b"])
        self.assertEqual(m["general"], ["qwen3:8b"])
        self.assertNotIn("glm-5.2:cloud", sum(m.values(), []))

    def test_suggested_pulls_skip_installed(self):
        names = [p["model"] for p in suggested_pulls(INSTALLED)]
        self.assertNotIn("qwen2.5-coder:14b", names)
        self.assertIn("qwen2.5-coder:3b", names)

    def test_render_parses_and_marks_unreachable(self):
        text = render([
            {"name": "gpu", "url": "http://x:11434", "reachable": True, "models": {"code": ["a:1"]}},
            {"name": "old", "url": "http://y:11434", "reachable": False, "error": "refused", "models": {}},
        ])
        cfg = parse_config(tomllib.loads(text))
        self.assertEqual([n.name for n in cfg.nodes], ["gpu", "old"])
        self.assertEqual(cfg.node("gpu").models_for("code"), ["a:1"])
        self.assertIn("UNREACHABLE", text)

    def test_generate_with_unreachable_node(self):
        text, nodes = asyncio.run(generate([("ghost", "http://127.0.0.1:9")]))
        self.assertFalse(nodes[0]["reachable"])
        parse_config(tomllib.loads(text))

    def test_parse_spec(self):
        self.assertEqual(parse_spec("gpu=http://1.2.3.4:11434/"), ("gpu", "http://1.2.3.4:11434"))
        with self.assertRaises(ValueError):
            parse_spec("gpu")

    def test_write_config_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("ROUTEAI_CONFIG")
            os.environ["ROUTEAI_CONFIG"] = str(Path(tmp) / "fleet.toml")
            try:
                write_config("# one\n")
                with self.assertRaises(FileExistsError):
                    write_config("# two\n")
                path, backup = write_config("# two\n", overwrite=True)
                self.assertEqual(path.read_text(encoding="utf-8"), "# two\n")
                self.assertEqual(backup.read_text(encoding="utf-8"), "# one\n")
            finally:
                if old is None:
                    os.environ.pop("ROUTEAI_CONFIG", None)
                else:
                    os.environ["ROUTEAI_CONFIG"] = old


if __name__ == "__main__":
    unittest.main()
