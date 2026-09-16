"""Benchmark tasks. Every task is graded automatically, so scores are comparable across runs.

"quick" runs one task per category; "full" adds a second task for the categories
where models differ the most.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

from .grader import check_module, check_script, check_tests


@dataclass(frozen=True)
class BenchTask:
    id: str
    category: str
    prompt: str
    grade: Callable[[str], tuple[float, str]]
    filename: str | None = None  # expected file (code tasks); None = JSON answer
    schema: dict | None = None
    quick: bool = True
    max_tokens: int = 1536


# -- complex -------------------------------------------------------------------

TTL_PROMPT = """Write a Python module that defines `class TTLCache`.

Constructor: `TTLCache(capacity: int, ttl: float, clock=time.monotonic)`.
- `put(key, value)`: insert or update. The entry's age restarts at the current `clock()` value. If the number of non-expired entries then exceeds `capacity`, evict the least recently used non-expired entry.
- `get(key, default=None)`: if the key is present and not expired, mark it most recently used and return its value; otherwise return `default`. An entry is expired when `clock() - inserted_at >= ttl`. `get` does not extend the TTL. Expired entries must be removed.
- `__len__()`: number of non-expired entries.
- `__contains__(key)`: True if present and not expired. It must NOT change recency.
- Raise `ValueError` if `capacity < 1` or `ttl <= 0`.
Standard library only. Return only the module code."""

TTL_CHECKS = r'''
class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t

def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False

def c_validation(m):
    return raises(ValueError, lambda: m.TTLCache(0, 1.0)) and raises(ValueError, lambda: m.TTLCache(1, 0))

def c_basic(m):
    c = m.TTLCache(2, 10, clock=Clock())
    c.put("a", 1)
    return c.get("a") == 1 and c.get("zz") is None and c.get("zz", 5) == 5

def c_lru(m):
    c = m.TTLCache(2, 10, clock=Clock())
    c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
    return c.get("b") is None and c.get("a") == 1 and c.get("c") == 3

def c_update(m):
    k = Clock(); c = m.TTLCache(2, 10, clock=k)
    c.put("a", 1); k.t = 8; c.put("a", 2); k.t = 15
    return c.get("a") == 2

def c_expiry(m):
    k = Clock(); c = m.TTLCache(2, 5, clock=k)
    c.put("a", 1); k.t = 4.9
    alive = c.get("a") == 1
    k.t = 5.0
    return alive and c.get("a") is None

def c_len(m):
    k = Clock(); c = m.TTLCache(3, 5, clock=k)
    c.put("a", 1); k.t = 3; c.put("b", 2); k.t = 6
    return len(c) == 1

def c_contains(m):
    k = Clock(); c = m.TTLCache(2, 10, clock=k)
    c.put("a", 1); c.put("b", 2)
    inside = "a" in c
    c.put("c", 3)
    return inside and "a" not in c and "b" in c and "c" in c

def c_expired_frees_room(m):
    k = Clock(); c = m.TTLCache(2, 5, clock=k)
    c.put("a", 1); k.t = 1; c.put("b", 2); k.t = 2; c.get("a"); k.t = 5.5; c.put("c", 3)
    return c.get("b") == 2 and c.get("c") == 3

def c_get_keeps_ttl(m):
    k = Clock(); c = m.TTLCache(2, 5, clock=k)
    c.put("a", 1); k.t = 4; c.get("a"); k.t = 5.5
    return c.get("a") is None

CHECKS = [("validation", c_validation), ("basic", c_basic), ("lru", c_lru), ("update", c_update),
          ("expiry", c_expiry), ("len", c_len), ("contains", c_contains),
          ("expired_frees_room", c_expired_frees_room), ("get_keeps_ttl", c_get_keeps_ttl)]
'''

EXPR_PROMPT = """Write a Python module with `evaluate(expr: str) -> float` that evaluates arithmetic expressions with numbers (integers or decimals like 1.5), `+ - * /`, parentheses, unary minus/plus and arbitrary whitespace, with the usual precedence and left associativity.
Raise `ValueError` for any malformed input (empty string, dangling operators, unbalanced parentheses, unknown characters, two numbers in a row).
Do NOT use eval, exec, compile or the ast module: write a real parser. Standard library only. Return only the module code."""

EXPR_CHECKS = r'''
def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False

OK = [("1+2*3", 7), ("(1+2)*3", 9), ("-3+5", 2), ("2*-3", -6), ("10/4", 2.5), ("2*(3+(4-1))/3", 4),
      (" 7 ", 7), ("1.5*2", 3), ("--2", 2), ("8-3-2", 3), ("16/4/2", 2), ("+4", 4)]
BAD = ["1+", "(1", "", "2*/3", "abc", "1 2", "1)"]
CHECKS = [(f"ok {e!r}", (lambda m, e=e, v=v: abs(m.evaluate(e) - v) < 1e-9)) for e, v in OK]
CHECKS += [(f"bad {e!r}", (lambda m, e=e: raises(ValueError, lambda: m.evaluate(e)))) for e in BAD]
'''


_FORBIDDEN = re.compile(r"(?<![.\w])(eval|exec|compile)\s*\(|^\s*(import ast|from ast)\b", re.M)


def _grade_expr(answer: str) -> tuple[float, str]:
    if _FORBIDDEN.search(answer):  # re.compile(...) is fine, builtin compile(...) is not
        return 0.0, "used eval/exec/compile/ast"
    return check_module(answer, EXPR_CHECKS)


# -- code ----------------------------------------------------------------------

DURATION_PROMPT = """Write a Python module with `parse_duration(text: str) -> int` returning the total number of seconds for strings like "1h30m", "2d", "45s", "1d2h3m4s".
Rules: units are d, h, m, s (case-insensitive); each unit appears at most once and in the order d, h, m, s; numbers are non-negative integers; spaces between parts and surrounding whitespace are allowed ("1h 30m", " 90m ").
Raise `ValueError` for empty strings, unknown units, a unit without a number, repeated or out-of-order units, decimals and negative numbers. Return only the module code."""

DURATION_CHECKS = r'''
def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False

OK = [("1h30m", 5400), ("2d", 172800), ("45s", 45), ("1d2h3m4s", 93784), ("1h 30m", 5400),
      ("1H30M", 5400), ("0s", 0), (" 90m ", 5400)]
BAD = ["", "10x", "h", "1m1m", "1s1m", "1.5h", "-1h"]
CHECKS = [(f"ok {t!r}", (lambda m, t=t, v=v: m.parse_duration(t) == v)) for t, v in OK]
CHECKS += [(f"bad {t!r}", (lambda m, t=t: raises(ValueError, lambda: m.parse_duration(t)))) for t in BAD]
'''


# -- tests ---------------------------------------------------------------------

ROMAN_SRC = '''VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int:
    """Convert a Roman numeral to an integer.

    Only the upper-case characters IVXLCDM are accepted: an empty string or any other
    character raises ValueError. Non-canonical forms such as "IIII" are accepted (4).
    """
    if not s:
        raise ValueError("empty numeral")
    total = 0
    for i, ch in enumerate(s):
        if ch not in VALUES:
            raise ValueError(f"invalid character {ch!r}")
        value = VALUES[ch]
        nxt = VALUES.get(s[i + 1]) if i + 1 < len(s) else None
        if nxt is not None and nxt > value:
            total -= value
        else:
            total += value
    return total
'''


def _mutant(old: str, new: str) -> str:
    assert old in ROMAN_SRC, old
    return ROMAN_SRC.replace(old, new)


ROMAN_MUTANTS = [
    _mutant("total -= value", "total += value"),
    _mutant('raise ValueError("empty numeral")', "return 0"),
    _mutant("if nxt is not None and nxt > value:", 'if nxt is not None and nxt > value and ch == "I":'),
    _mutant('raise ValueError(f"invalid character {ch!r}")', "continue"),
]

TESTS_PROMPT = f"""Write pytest unit tests for the function below, which lives in the module `roman` (import it with `from roman import roman_to_int`).
Cover normal numerals, subtractive pairs (IV, IX, XL, XC, CD, CM), larger numbers, and every documented error case. Test only the documented behaviour.
Return only the test file.

```python
{ROMAN_SRC}```"""


# -- scripts ---------------------------------------------------------------------

CSV_PROMPT = """Write a Python 3 script that reads CSV data from standard input. The first line is a header that contains at least the columns `name` and `amount`, in any order, possibly among other columns.
For each distinct name, sum the amounts (decimal numbers, possibly negative); skip rows whose amount is empty.
Print one line per name, sorted alphabetically by name, formatted as `name,total` with the total shown with exactly 2 decimals. Names never contain commas.
If the header lacks either column, print an error to stderr and exit with status 2. Standard library only. Return only the script."""

CSV_CASES = [
    {"stdin": "name,amount\nbob,1.5\nalice,2\nbob,-0.25\n", "stdout": "alice,2.00\nbob,1.25"},
    {"stdin": "id,amount,name,x\n1,3.333,zed,a\n2,,zed,b\n3,1,amy,c\n", "stdout": "amy,1.00\nzed,3.33"},
    {"stdin": "name,value\na,1\n", "code": 2},
    {"stdin": "amount,name\n", "stdout": ""},
]

TOPWORDS_PROMPT = """Write a Python 3 script used as `python script.py PATH [--top N]`. It reads the UTF-8 text file PATH, extracts words as maximal runs of ASCII letters a-z (case-insensitive, printed in lower case) and prints the N most frequent words (default 3), one per line as `word count`, sorted by count descending, then alphabetically.
If PATH does not exist, print an error to stderr and exit with status 1. Standard library only. Return only the script."""

TOPWORDS_CASES = [
    {"files": {"a.txt": "The cat and the hat. THE end, cat!"}, "args": ["a.txt", "--top", "2"], "stdout": "the 3\ncat 2"},
    {"files": {"b.txt": "b a c b a b"}, "args": ["b.txt"], "stdout": "b 3\na 2\nc 1"},
    {"args": ["missing.txt"], "code": 1},
    {"files": {"c.txt": "x1y z"}, "args": ["c.txt", "--top", "5"], "stdout": "x 1\ny 1\nz 1"},
]


# -- build -----------------------------------------------------------------------

PYPROJECT_PROMPT = """Write a complete `pyproject.toml` for a Python package with:
- distribution name `acme-tools`, version `0.3.1`, requires Python 3.10 or newer
- dependencies: `requests` version 2.31 or newer, and `click` (any version)
- a console script `acme` that calls the function `main` in the module `acme_tools.cli`
- build backend: hatchling
Return only the file."""


def _grade_pyproject(answer: str) -> tuple[float, str]:
    try:
        data = tomllib.loads(answer)
    except tomllib.TOMLDecodeError as exc:
        return 0.0, f"invalid TOML: {exc}"
    p, bs = data.get("project", {}), data.get("build-system", {})
    deps = [str(d).replace(" ", "").lower() for d in p.get("dependencies", [])]
    checks = {
        "name": p.get("name") == "acme-tools",
        "version": p.get("version") == "0.3.1",
        "python": ">=3.10" in str(p.get("requires-python", "")).replace(" ", ""),
        "requests": any(d.startswith("requests>=2.31") for d in deps),
        "click": any(d.startswith("click") for d in deps),
        "script": p.get("scripts", {}).get("acme") == "acme_tools.cli:main",
        "backend": bs.get("build-backend") == "hatchling.build" and any("hatchling" in r for r in bs.get("requires", [])),
    }
    failed = [k for k, ok in checks.items() if not ok]
    return (len(checks) - len(failed)) / len(checks), "missing: " + ", ".join(failed) if failed else "all checks"


ROOT_CAUSES = ["missing_dependency", "dependency_conflict", "syntax_error", "test_failure",
               "out_of_memory", "permission_denied", "network_error"]

BUILD_LOGS = """Diagnose each build log below.

Log 1:
```
$ python -m app.main
Traceback (most recent call last):
  File "/srv/app/app/main.py", line 3, in <module>
    import yaml
ModuleNotFoundError: No module named 'yaml'
```

Log 2:
```
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! While resolving: web@1.0.0
npm ERR! Found: react@18.2.0
npm ERR! Could not resolve dependency:
npm ERR! peer react@"^17.0.0" from react-beautiful-dnd@13.1.0
```

Log 3:
```
> Task :app:compileReleaseKotlin
e: java.lang.OutOfMemoryError: Java heap space
FAILURE: Build failed with an exception.
```

For each log give its number, the root cause (one of the allowed values) and the minimal fix (a command or a one-line change)."""

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnoses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "log": {"type": "integer"},
                    "root_cause": {"type": "string", "enum": ROOT_CAUSES},
                    "fix": {"type": "string"},
                },
                "required": ["log", "root_cause", "fix"],
            },
        }
    },
    "required": ["diagnoses"],
}


def _grade_build_logs(answer: str) -> tuple[float, str]:
    try:
        items = json.loads(answer)["diagnoses"]
        by_log = {int(d["log"]): d for d in items}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0.0, "invalid JSON"
    expected = {1: "missing_dependency", 2: "dependency_conflict", 3: "out_of_memory"}
    score, notes = 0.0, []
    for log, cause in expected.items():
        if by_log.get(log, {}).get("root_cause") == cause:
            score += 0.25
        else:
            notes.append(f"log{log}")
    if "pyyaml" in str(by_log.get(1, {}).get("fix", "")).lower():
        score += 0.25
    else:
        notes.append("fix1 lacks pyyaml")
    return score, "wrong: " + ", ".join(notes) if notes else "all correct"


# -- docs ------------------------------------------------------------------------

DOC_SRC = '''def chunk_text(text: str, size: int, overlap: int = 0) -> list[str]:
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be in [0, size)")
    step = size - overlap
    return [text[i:i + size] for i in range(0, max(len(text) - overlap, 1), step)]
'''

DOC_PROMPT = f"""Add a Google-style docstring (summary line, Args, Returns, Raises sections) to this function. Do not change the code in any other way. Return the complete function.

```python
{DOC_SRC}```"""


def _grade_docstring(answer: str) -> tuple[float, str]:
    try:
        tree = ast.parse(answer)
    except (SyntaxError, ValueError):  # ValueError: NUL bytes
        return 0.0, "syntax error"
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "chunk_text"), None)
    if fn is None:
        return 0.0, "function missing"
    doc = ast.get_docstring(fn) or ""
    orig = ast.parse(DOC_SRC).body[0]
    score, notes = 0.0, []
    parts = [
        (0.2, bool(doc), "no docstring"),
        (0.3, "Args:" in doc and all(p in doc for p in ("text", "size", "overlap")), "Args incomplete"),
        (0.15, "Returns:" in doc, "no Returns"),
        (0.15, "Raises:" in doc and "ValueError" in doc, "no Raises"),
    ]
    body = fn.body[1:] if doc else fn.body
    same = [ast.dump(s) for s in body] == [ast.dump(s) for s in orig.body] and ast.dump(fn.args) == ast.dump(orig.args)
    parts.append((0.2, same, "code changed"))
    for weight, ok, note in parts:
        if ok:
            score += weight
        else:
            notes.append(note)
    return score, "; ".join(notes) or "all checks"


# -- general ---------------------------------------------------------------------

EXTRACT_PROMPT = """Extract the requested fields from this message. The amount uses the Italian number format.

"Buongiorno, sono Maria Rossi di Contoso Srl. Vi chiedo di inviare la fattura da 1.250,50 EUR all'indirizzo maria.rossi@contoso.example entro venerdì. Grazie!"
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "company": {"type": "string"},
        "email": {"type": "string"},
        "amount_eur": {"type": "number"},
    },
    "required": ["name", "company", "email", "amount_eur"],
}


def _grade_extract(answer: str) -> tuple[float, str]:
    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        return 0.0, "invalid JSON"
    if not isinstance(data, dict):
        return 0.0, "not a JSON object"
    checks = {
        "name": str(data.get("name", "")).strip().casefold() == "maria rossi",
        "company": str(data.get("company", "")).strip().casefold() == "contoso srl",
        "email": str(data.get("email", "")).strip() == "maria.rossi@contoso.example",
        "amount": isinstance(data.get("amount_eur"), (int, float)) and abs(data["amount_eur"] - 1250.5) < 0.01,
    }
    failed = [k for k, ok in checks.items() if not ok]
    return (4 - len(failed)) / 4, "wrong: " + ", ".join(failed) if failed else "all fields"


TASKS: list[BenchTask] = [
    BenchTask("ttl_cache", "complex", TTL_PROMPT, lambda a: check_module(a, TTL_CHECKS), "solution.py", max_tokens=2048),
    BenchTask("expr_eval", "complex", EXPR_PROMPT, _grade_expr, "solution.py", quick=False, max_tokens=2048),
    BenchTask("parse_duration", "code", DURATION_PROMPT, lambda a: check_module(a, DURATION_CHECKS), "solution.py"),
    BenchTask("roman_tests", "tests", TESTS_PROMPT,
              lambda a: check_tests(a, "roman", ROMAN_SRC, ROMAN_MUTANTS), "test_roman.py", max_tokens=2048),
    BenchTask("csv_totals", "scripts", CSV_PROMPT, lambda a: check_script(a, CSV_CASES), "script.py"),
    BenchTask("topwords", "scripts", TOPWORDS_PROMPT, lambda a: check_script(a, TOPWORDS_CASES), "script.py", quick=False),
    BenchTask("pyproject", "build", PYPROJECT_PROMPT, _grade_pyproject, "pyproject.toml", max_tokens=768),
    BenchTask("build_logs", "build", BUILD_LOGS, _grade_build_logs, None, BUILD_SCHEMA, quick=False, max_tokens=768),
    BenchTask("docstring", "docs", DOC_PROMPT, _grade_docstring, "chunk.py", max_tokens=1024),
    BenchTask("extract", "general", EXTRACT_PROMPT, _grade_extract, None, EXTRACT_SCHEMA, max_tokens=256),
]


def select_tasks(mode: str = "quick", categories: list[str] | None = None) -> list[BenchTask]:
    tasks = [t for t in TASKS if mode == "full" or t.quick]
    return [t for t in tasks if not categories or t.category in categories]
