"""The bin/ launchers must find a Python 3.11+ and start run.py on every OS."""

import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / ("routeai.cmd" if os.name == "nt" else "routeai")


class LauncherTests(unittest.TestCase):
    def run_launcher(self, *args, env=None):
        return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, timeout=60,
                              env={**os.environ, **(env or {})})

    def test_help(self):
        proc = self.run_launcher("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: routeai", proc.stdout)

    def test_exit_code_is_propagated(self):
        proc = self.run_launcher("no-such-command")
        self.assertEqual(proc.returncode, 2, proc.stderr)  # argparse usage error

    def test_forced_interpreter(self):
        proc = self.run_launcher("--help", env={"ROUTEAI_PYTHON": sys.executable})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_posix_script_is_lf_and_executable(self):
        script = ROOT / "bin" / "routeai"
        self.assertNotIn(b"\r\n", script.read_bytes())
        if os.name != "nt":
            self.assertTrue(script.stat().st_mode & stat.S_IXUSR, "git must store bin/routeai as executable")


if __name__ == "__main__":
    unittest.main()
