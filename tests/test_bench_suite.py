"""The benchmark must be fair: reference solutions score 100%, broken ones do not."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routeai.bench.suite import TASKS  # noqa: E402

REFERENCE = {
    "ttl_cache": '''
import time
from collections import OrderedDict

class TTLCache:
    def __init__(self, capacity, ttl, clock=time.monotonic):
        if capacity < 1 or ttl <= 0:
            raise ValueError("bad args")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._d = OrderedDict()

    def _purge(self):
        now = self.clock()
        for k in [k for k, (_, t) in self._d.items() if now - t >= self.ttl]:
            del self._d[k]

    def put(self, key, value):
        self._purge()
        self._d[key] = (value, self.clock())
        self._d.move_to_end(key)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)

    def get(self, key, default=None):
        self._purge()
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key][0]
        return default

    def __len__(self):
        self._purge()
        return len(self._d)

    def __contains__(self, key):
        self._purge()
        return key in self._d
''',
    "expr_eval": '''
import re
_TOK = re.compile(r"\\s*(?:(\\d+\\.\\d+|\\d+)|([-+*/()]))")

def evaluate(expr):
    s, pos, toks = expr.rstrip(), 0, []
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m:
            raise ValueError("bad input")
        toks.append(float(m.group(1)) if m.group(1) else m.group(2))
        pos = m.end()
    if not toks:
        raise ValueError("empty")
    i = 0

    def peek():
        return toks[i] if i < len(toks) else None

    def take():
        nonlocal i
        t = peek()
        i += 1
        return t

    def expr_():
        v = term()
        while peek() in ("+", "-"):
            op, r = take(), term()
            v = v + r if op == "+" else v - r
        return v

    def term():
        v = factor()
        while peek() in ("*", "/"):
            op, r = take(), factor()
            v = v * r if op == "*" else v / r
        return v

    def factor():
        t = take()
        if t in ("+", "-"):
            v = factor()
            return v if t == "+" else -v
        if t == "(":
            v = expr_()
            if take() != ")":
                raise ValueError("unbalanced")
            return v
        if isinstance(t, float):
            return t
        raise ValueError("unexpected token")

    v = expr_()
    if i != len(toks):
        raise ValueError("trailing input")
    return v
''',
    "parse_duration": '''
import re
_RE = re.compile(r"\\s*(?:(\\d+)\\s*d)?\\s*(?:(\\d+)\\s*h)?\\s*(?:(\\d+)\\s*m)?\\s*(?:(\\d+)\\s*s)?\\s*", re.I)

def parse_duration(text):
    m = _RE.fullmatch(text)
    if not m or not any(m.groups()):
        raise ValueError(text)
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s
''',
    "roman_tests": '''
import pytest
from roman import roman_to_int

@pytest.mark.parametrize("s,v", [("I", 1), ("III", 3), ("IV", 4), ("IX", 9), ("XL", 40), ("XC", 90),
                                 ("CD", 400), ("CM", 900), ("MCMXCIV", 1994), ("MMMCMXCIX", 3999), ("IIII", 4)])
def test_values(s, v):
    assert roman_to_int(s) == v

def test_empty():
    with pytest.raises(ValueError):
        roman_to_int("")

def test_invalid():
    with pytest.raises(ValueError):
        roman_to_int("ABC")

def test_lowercase():
    with pytest.raises(ValueError):
        roman_to_int("iv")
''',
    "csv_totals": '''
import csv, sys
from decimal import Decimal

def main():
    reader = csv.DictReader(sys.stdin)
    if not reader.fieldnames or "name" not in reader.fieldnames or "amount" not in reader.fieldnames:
        print("missing column", file=sys.stderr)
        return 2
    totals = {}
    for row in reader:
        if row["amount"]:
            totals[row["name"]] = totals.get(row["name"], Decimal(0)) + Decimal(row["amount"])
    for name in sorted(totals):
        print(f"{name},{totals[name]:.2f}")
    return 0

sys.exit(main())
''',
    "topwords": '''
import argparse, re, sys
from collections import Counter
p = argparse.ArgumentParser()
p.add_argument("path")
p.add_argument("--top", type=int, default=3)
a = p.parse_args()
try:
    text = open(a.path, encoding="utf-8").read()
except FileNotFoundError:
    print("not found", file=sys.stderr)
    sys.exit(1)
c = Counter(re.findall(r"[a-z]+", text.lower()))
for w, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[: a.top]:
    print(w, n)
''',
    "pyproject": '''
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "acme-tools"
version = "0.3.1"
requires-python = ">=3.10"
dependencies = ["requests>=2.31", "click"]

[project.scripts]
acme = "acme_tools.cli:main"
''',
    "build_logs": json.dumps({"diagnoses": [
        {"log": 1, "root_cause": "missing_dependency", "fix": "pip install pyyaml"},
        {"log": 2, "root_cause": "dependency_conflict", "fix": "npm install --legacy-peer-deps"},
        {"log": 3, "root_cause": "out_of_memory", "fix": "org.gradle.jvmargs=-Xmx4g"},
    ]}),
    "docstring": '''def chunk_text(text: str, size: int, overlap: int = 0) -> list[str]:
    """Split text into chunks.

    Args:
        text: The input text.
        size: Chunk length.
        overlap: Characters shared by consecutive chunks.

    Returns:
        The list of chunks.

    Raises:
        ValueError: If size or overlap are out of range.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be in [0, size)")
    step = size - overlap
    return [text[i:i + size] for i in range(0, max(len(text) - overlap, 1), step)]
''',
    "extract": json.dumps({"name": "Maria Rossi", "company": "Contoso Srl",
                           "email": "maria.rossi@contoso.example", "amount_eur": 1250.5}),
}

BROKEN = {
    "ttl_cache": "class TTLCache:\n    pass\n",
    "expr_eval": "def evaluate(expr):\n    return float(eval(expr))\n",
    "parse_duration": "def parse_duration(text):\n    return 0\n",
    "roman_tests": "def test_nothing():\n    assert True\n",
    "csv_totals": "print('alice,2.00')\n",
    "pyproject": "[project]\nname = 'x'\n",
    "docstring": "def chunk_text(text, size, overlap=0):\n    return [text]\n",
    "extract": "{}",
}


class SuiteTests(unittest.TestCase):
    def test_every_task_has_a_reference(self):
        self.assertEqual({t.id for t in TASKS}, set(REFERENCE))

    def test_reference_solutions_score_full(self):
        for task in TASKS:
            with self.subTest(task=task.id):
                score, detail = task.grade(REFERENCE[task.id].strip() + "\n")
                self.assertEqual(score, 1.0, detail)

    def test_broken_solutions_score_low(self):
        for task in TASKS:
            if task.id in BROKEN:
                with self.subTest(task=task.id):
                    score, detail = task.grade(BROKEN[task.id])
                    self.assertLess(score, 0.5, detail)


if __name__ == "__main__":
    unittest.main()
