"""Run model-written code to grade it: temp directory, isolated interpreter, hard timeout.

This executes code produced by your own local models on the machine running
the benchmark. Every run happens in a throwaway directory with a minimal
environment and a timeout, which is enough for these small, well-defined
prompts; it is not a security sandbox.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_S = 20

MODULE_HARNESS = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("solution", sys.argv[1])
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except BaseException as e:
    print(json.dumps({"passed": 0, "total": 0, "error": "import: " + repr(e)[:300]}))
    sys.exit(0)
ns = {}
exec(open(sys.argv[2], encoding="utf-8").read(), ns)
passed, fails = 0, []
for name, fn in ns["CHECKS"]:
    try:
        if fn(m):
            passed += 1
        else:
            fails.append(name)
    except BaseException as e:
        fails.append(f"{name}: {e!r}"[:160])
print(json.dumps({"passed": passed, "total": len(ns["CHECKS"]), "failures": fails[:6]}))
'''

# Just enough of pytest for generated tests to run without pytest installed.
PYTEST_SHIM = r'''
import math, re

class _Raises:
    def __init__(self, exc, match=None):
        self.exc, self.match, self.value = exc, match, None
    def __enter__(self):
        return self
    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"DID NOT RAISE {self.exc}")
        if not issubclass(et, self.exc):
            return False
        if self.match and not re.search(self.match, str(ev)):
            raise AssertionError(f"pattern {self.match!r} not found in {str(ev)!r}")
        self.value = ev
        return True

def raises(exc, *args, match=None, **kwargs):
    if args:
        func, *rest = args
        try:
            func(*rest, **kwargs)
        except exc as e:
            return e
        raise AssertionError(f"DID NOT RAISE {exc}")
    return _Raises(exc, match)

class approx:
    def __init__(self, expected, rel=1e-6, abs=1e-12):
        self.expected, self.rel, self.abs = expected, rel, abs
    def __eq__(self, other):
        return math.isclose(other, self.expected, rel_tol=self.rel, abs_tol=self.abs)

class _Mark:
    def parametrize(self, names, values, **kwargs):
        def deco(fn):
            fn.__dict__.setdefault("_fleet_params", []).append((names, list(values)))
            return fn
        return deco
    def __getattr__(self, name):
        def marker(*args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return lambda fn: fn
        return marker

mark = _Mark()

def fixture(*args, **kwargs):
    if len(args) == 1 and callable(args[0]):
        return args[0]
    return lambda fn: fn

def fail(msg=""):
    raise AssertionError(msg)
'''

TESTS_HARNESS = r'''
import importlib.util, inspect, itertools, json, sys, unittest
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("test_generated", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except BaseException as e:
    print(json.dumps({"passed": 0, "failed": 1, "error": "import: " + repr(e)[:300]}))
    sys.exit(0)
passed = failed = 0
fails = []

def run(name, fn, kwargs):
    global passed, failed
    try:
        fn(**kwargs)
        passed += 1
    except BaseException as e:
        failed += 1
        fails.append(f"{name}: {e!r}"[:160])

def expand(names, values):
    keys = [k.strip() for k in names.split(",")] if isinstance(names, str) else list(names)
    for v in values:
        yield dict(zip(keys, v if len(keys) > 1 else (v,)))

for name, obj in list(vars(mod).items()):
    if name.startswith("test") and inspect.isfunction(obj):
        params = getattr(obj, "_fleet_params", None)
        if params:
            for i, combo in enumerate(itertools.product(*(list(expand(n, v)) for n, v in params))):
                kwargs = {}
                for part in combo:
                    kwargs.update(part)
                run(f"{name}[{i}]", obj, kwargs)
        elif not inspect.signature(obj).parameters:
            run(name, obj, {})
    elif inspect.isclass(obj) and issubclass(obj, unittest.TestCase) and obj is not unittest.TestCase:
        res = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(obj).run(res)
        bad = len(res.failures) + len(res.errors)
        passed += res.testsRun - bad
        failed += bad
print(json.dumps({"passed": passed, "failed": failed, "failures": fails[:6]}))
'''


MAX_CAPTURE_BYTES = 1_000_000


def _env(cwd: Path) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    # Model-written code gets the throwaway directory as home and temp, not the user's profile.
    env.update({"HOME": str(cwd), "USERPROFILE": str(cwd), "TEMP": str(cwd), "TMP": str(cwd),
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def _read_capped(path: Path) -> str:
    with path.open("rb") as fh:
        return fh.read(MAX_CAPTURE_BYTES).decode("utf-8", errors="replace")


def _run(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run Python in cwd with a timeout; output goes to capped files and the whole process tree is killed.

    Never inherit our stdin: inside the MCP server it is the JSON-RPC pipe, and on Windows a child
    started on a synchronous pipe with a pending read blocks until the next MCP message arrives.
    """
    out_path, err_path = cwd / ".fleet-stdout", cwd / ".fleet-stderr"
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-I", *args], cwd=cwd, env=_env(cwd), stdout=out, stderr=err,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, **group,
        )
        try:
            if stdin is not None:
                try:
                    proc.stdin.write(stdin.encode("utf-8"))
                    proc.stdin.close()
                except OSError:
                    pass
            code = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=10)
            raise
    return subprocess.CompletedProcess(args, code, _read_capped(out_path), _read_capped(err_path))


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def check_module(solution: str, checks: str) -> tuple[float, str]:
    """Import the solution as a module and run CHECKS against it."""
    with tempfile.TemporaryDirectory(prefix="fleet-bench-", ignore_cleanup_errors=True) as tmp:
        d = Path(tmp)
        (d / "solution.py").write_text(solution, encoding="utf-8")
        (d / "checks.py").write_text(checks, encoding="utf-8")
        (d / "harness.py").write_text(MODULE_HARNESS, encoding="utf-8")
        try:
            proc = _run(["harness.py", str(d / "solution.py"), str(d / "checks.py")], d)
        except subprocess.TimeoutExpired:
            return 0.0, "timeout"
        res = _last_json(proc.stdout)
        if not res.get("total"):
            return 0.0, res.get("error") or (proc.stderr.strip()[-300:] or "no result")
        return res["passed"] / res["total"], f"{res['passed']}/{res['total']} " + "; ".join(res.get("failures", []))


def check_script(script: str, cases: list[dict]) -> tuple[float, str]:
    """Run the script once per case: args, stdin, files -> expected stdout and exit code."""
    passed, notes = 0, []
    for i, case in enumerate(cases):
        with tempfile.TemporaryDirectory(prefix="fleet-bench-", ignore_cleanup_errors=True) as tmp:
            d = Path(tmp)
            (d / "script.py").write_text(script, encoding="utf-8")
            for name, content in case.get("files", {}).items():
                (d / name).write_text(content, encoding="utf-8")
            try:
                proc = _run(["script.py", *case.get("args", [])], d, case.get("stdin"))
            except subprocess.TimeoutExpired:
                notes.append(f"case{i}: timeout")
                continue
            ok = proc.returncode == case.get("code", 0)
            if ok and case.get("stdout") is not None:
                ok = proc.stdout.strip().splitlines() == case["stdout"].strip().splitlines()
            if ok:
                passed += 1
            else:
                notes.append(f"case{i}: exit={proc.returncode} out={proc.stdout.strip()[:80]!r}")
    return passed / len(cases), f"{passed}/{len(cases)} " + "; ".join(notes)


def _run_tests(tests: str, module_name: str, module_src: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="fleet-bench-", ignore_cleanup_errors=True) as tmp:
        d = Path(tmp)
        (d / f"{module_name}.py").write_text(module_src, encoding="utf-8")
        (d / "pytest.py").write_text(PYTEST_SHIM, encoding="utf-8")
        (d / "test_generated.py").write_text(tests, encoding="utf-8")
        (d / "harness.py").write_text(TESTS_HARNESS, encoding="utf-8")
        try:
            proc = _run(["harness.py", str(d), str(d / "test_generated.py")], d)
        except subprocess.TimeoutExpired:
            return {"passed": 0, "failed": 1, "error": "timeout"}
        return _last_json(proc.stdout) or {"passed": 0, "failed": 1, "error": proc.stderr[-300:]}


def check_tests(tests: str, module_name: str, correct: str, mutants: list[str]) -> tuple[float, str]:
    """Mutation score: tests must pass on the correct code and fail on each buggy variant."""
    base = _run_tests(tests, module_name, correct)
    if base.get("failed") or base.get("passed", 0) < 3:
        return 0.0, f"on correct code: {base.get('passed', 0)} passed, {base.get('failed', 0)} failed " + (
            base.get("error") or "; ".join(base.get("failures", [])))
    killed = 0
    for src in mutants:
        res = _run_tests(tests, module_name, src)
        if res.get("failed"):
            killed += 1
    return 0.2 + 0.8 * killed / len(mutants), f"{base['passed']} tests, killed {killed}/{len(mutants)} mutants"
