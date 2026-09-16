"""Benchmark report: recommendations must be right, and silent when there is nothing to say."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routeai.bench.runner import Row, build_report  # noqa: E402
from routeai.config import parse_config  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.stats import Stats  # noqa: E402

CFG = {
    "nodes": [
        {"name": "gpu", "url": "http://gpu", "tier": "heavy",
         "models": {"complex": ["big:30b", "small:14b"], "code": ["small:14b"]}},
        {"name": "laptop", "url": "http://laptop", "tier": "light",
         "models": {"scripts": ["tiny:3b"], "docs": ["tiny:3b"]}},
    ],
}


def row(node, model, cat, score, tps):
    return Row(node, model, cat, f"{cat}-task", score, 10.0, tps, 100)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fleet = Fleet(parse_config(CFG), Stats(Path(self.tmp.name) / "stats.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, rows, gpu):
        return build_report(self.fleet, rows, gpu, {}, "quick", False)

    def test_recommendations(self):
        rows = [row("gpu", "big:30b", "complex", 0.5, 20), row("gpu", "small:14b", "complex", 1.0, 50),
                row("laptop", "tiny:3b", "scripts", 0.2, 12), row("laptop", "tiny:3b", "docs", 1.0, 12)]
        recs = "\n".join(self.report(rows, {"gpu|big:30b": 0.5, "gpu|small:14b": 1.0, "laptop|tiny:3b": 0.0})["recommendations"])
        self.assertIn("Put small:14b first for 'complex' on gpu", recs)
        self.assertIn("Drop tiny:3b from 'scripts' on laptop", recs)
        self.assertIn("laptop runs every model on CPU only", recs)
        self.assertIn("big:30b on gpu is only 50% in VRAM", recs)
        self.assertNotIn("tiny:3b on laptop is only", recs)

    def test_tie_needs_clear_speed_win(self):
        rows = [row("gpu", "big:30b", "complex", 1.0, 40), row("gpu", "small:14b", "complex", 1.0, 50)]
        self.assertEqual(self.report(rows, {})["recommendations"], ["No changes suggested."])
        rows = [row("gpu", "big:30b", "complex", 1.0, 20), row("gpu", "small:14b", "complex", 1.0, 50)]
        self.assertIn("Put small:14b first", self.report(rows, {})["recommendations"][0])

    def test_markdown_and_best(self):
        rep = self.report([row("gpu", "small:14b", "code", 0.8, 49)], {"gpu|small:14b": 1.0})
        self.assertIn("| gpu | small:14b | code | 80% | 49 |", rep["markdown"])
        self.assertIn("small:14b @ gpu", rep["best_per_category"]["code"])


if __name__ == "__main__":
    unittest.main()
