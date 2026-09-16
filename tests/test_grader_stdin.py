"""Grading inside the MCP server must not touch the server's stdin (the JSON-RPC pipe).

On Windows a child process that inherits a synchronous pipe on which the parent has a pending
read blocks during start-up until the next MCP message arrives, so every graded run timed out.
"""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = textwrap.dedent(f"""
    import os, sys, threading, time
    sys.path.insert(0, {str(SRC)!r})
    from routeai.bench.grader import check_module
    threading.Thread(target=sys.stdin.buffer.read, daemon=True).start()  # like the MCP reader thread
    time.sleep(0.5)
    score, detail = check_module("def f():\\n    return 1\\n", "CHECKS = [('f', lambda m: m.f() == 1)]")
    print(score, detail, flush=True)
    os._exit(0)  # skip interpreter shutdown while the reader thread still holds stdin
""")


class GraderStdinTests(unittest.TestCase):
    def test_grading_while_stdin_pipe_is_being_read(self):
        proc = subprocess.Popen([sys.executable, "-c", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            proc.wait(timeout=90)  # stdin stays open and silent, as between two MCP messages
        finally:
            if proc.poll() is None:
                proc.kill()
            out, err = proc.communicate(timeout=30)  # closes stdin and drains both pipes
        self.assertTrue(out.startswith("1.0"), f"stdout={out!r} stderr={err[-500:]!r}")


if __name__ == "__main__":
    unittest.main()
