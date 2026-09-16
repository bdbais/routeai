"""SSH nodes: target parsing, the ssh command line, the installed key, error hints and the tunnel lifecycle."""

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai import sshtunnel as st  # noqa: E402
from routeai.config import ConfigError, parse_config  # noqa: E402
from routeai.fleet import Fleet  # noqa: E402
from routeai.setup import add_node, drop_node_option, parse_spec, render, set_node_option  # noqa: E402
from routeai.stats import Stats  # noqa: E402

FAKE_SSH = json.dumps([sys.executable, str(ROOT / "tests" / "fake_ssh.py")])


class TargetTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(st.parse_target("fede@10.0.0.5"), st.SshTarget("fede", "10.0.0.5", None))
        self.assertEqual(st.parse_target("ssh://fede@gpu.example.net:2222/"), st.SshTarget("fede", "gpu.example.net", 2222))
        self.assertEqual(st.parse_target("me@[fe80::1]:22"), st.SshTarget("me", "fe80::1", 22))
        alias = st.parse_target("gpu-box")
        self.assertTrue(alias.is_alias)
        self.assertEqual(alias.destination, "gpu-box")
        self.assertEqual(str(st.parse_target("me@[::1]:2200")), "me@[::1]:2200")

    def test_anything_that_could_become_an_ssh_option_is_refused(self):
        for bad in ["-oProxyCommand=calc", "fede@-oProxyCommand=x", "fede@evil host", "fe;de@host", "",
                    "ssh://", "fede@host:99999", "fede@host:22x", 'fe"de@host', "me@[::1]x"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                st.parse_target(bad)

    def test_node_names_become_safe_file_names(self):
        self.assertTrue(st.key_path("linux-gpu").name.endswith("linux-gpu"))
        with self.assertRaises(ValueError):
            st.key_path("../../etc/passwd")


class CommandLineTests(unittest.TestCase):
    def test_managed_key_pins_identity_and_host_key(self):
        args = st.tunnel_args(st.parse_target("fede@10.0.0.5:2222"), 50000, 11434, Path("/k/id"))
        self.assertIn("127.0.0.1:50000:127.0.0.1:11434", args)
        for option in ("BatchMode=yes", "ExitOnForwardFailure=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes"):
            self.assertIn(option, args)
        known = next(a for a in args if a.startswith("UserKnownHostsFile="))
        self.assertRegex(known, r'^UserKnownHostsFile="[^"\\]+"$')  # quoted, forward slashes
        self.assertEqual(args[args.index("-p") + 1], "2222")
        self.assertEqual(args[-2:], ["--", "fede@10.0.0.5"])

    def test_alias_leaves_the_users_ssh_config_in_charge(self):
        args = st.tunnel_args(st.parse_target("gpu-box"), 50000, 11434, None)
        self.assertIn("BatchMode=yes", args)
        self.assertNotIn("-i", args)
        self.assertFalse(any(a.startswith(("UserKnownHostsFile", "StrictHostKeyChecking")) for a in args))
        self.assertEqual(args[-2:], ["--", "gpu-box"])

    def test_restricted_key_can_only_forward_to_ollama(self):
        line = st.authorized_key_line("ssh-ed25519 AAAAC3Nza routeai-gpu\n", 11434)
        self.assertEqual(line, 'restrict,port-forwarding,permitopen="127.0.0.1:11434",command="/bin/false" '
                               "ssh-ed25519 AAAAC3Nza routeai-gpu")
        self.assertEqual(st.authorized_key_line("ssh-ed25519 AAAA x", 11434, restricted=False), "ssh-ed25519 AAAA x")

    def test_install_script_survives_any_login_shell_and_is_idempotent(self):
        line = st.authorized_key_line("ssh-ed25519 AAAA routeai", 11434)
        script = st.install_script(line)
        self.assertTrue(script.startswith("sh -c '") and script.endswith("'"))
        self.assertEqual(script.count("'"), 2)
        encoded = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", script).group(1)
        self.assertEqual(base64.b64decode(encoded).decode(), line)
        self.assertIn("grep -qxF", script)
        self.assertIn(st.INSTALLED_MARK, script)


class KeyFileTests(unittest.TestCase):
    def test_public_key_path_keeps_dotted_names(self):
        self.assertEqual(st.public_key_path(Path("/k/id_ed25519_gpu.lan")).name, "id_ed25519_gpu.lan.pub")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH client not installed")
    def test_generated_key_is_accepted_by_openssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("ROUTEAI_HOME")
            os.environ["ROUTEAI_HOME"] = str(Path(tmp) / "home with spaces")
            try:
                path, created = st.generate_key("gpu.lan")
                self.assertTrue(created)
                self.assertFalse(st.generate_key("gpu.lan")[1])
                # ssh-keygen -y loads the private key with the same permission check ssh applies
                check = subprocess.run([shutil.which("ssh-keygen"), "-y", "-f", str(path)], stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
                self.assertEqual(check.stdout.split()[:2],
                                 st.public_key_path(path).read_text(encoding="utf-8").split()[:2])
            finally:
                if old is None:
                    os.environ.pop("ROUTEAI_HOME", None)
                else:
                    os.environ["ROUTEAI_HOME"] = old


class HintTests(unittest.TestCase):
    def test_each_stage_gets_its_own_advice(self):
        cases = {
            "Permission denied (publickey).": "ssh-setup gpu",
            "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!": "ssh-forget gpu",
            "ssh: Could not resolve hostname nope: Name or service not known": "does not resolve",
            "ssh: connect to host 10.0.0.5 port 22: Connection refused": "sshd",
            "ssh: connect to host 10.0.0.5 port 22: Connection timed out": "does not answer",
            "channel 2: open failed: administratively prohibited: open failed": "refused the forward to 127.0.0.1:11434",
            "channel 2: open failed: connect failed: Connection refused": "is Ollama running there",
        }
        for stderr, expected in cases.items():
            with self.subTest(stderr=stderr):
                self.assertIn(expected, st.classify(stderr, "gpu", 11434))
        self.assertEqual(st.classify("something new\n"), "something new")
        self.assertIsNone(st.known_problem("Warning: Permanently added 'x' to the list of known hosts."))


class ConfigTests(unittest.TestCase):
    def test_ssh_node_needs_no_url(self):
        cfg = parse_config({"nodes": [{"name": "gpu", "ssh": "fede@10.0.0.5", "ssh_key": "~/.routeai/ssh/k",
                                       "remote_port": 11500}]})
        node = cfg.node("gpu")
        self.assertTrue(node.via_ssh)
        self.assertEqual(node.url, "ssh://fede@10.0.0.5")
        self.assertEqual(node.remote_port, 11500)

    def test_invalid_ssh_nodes_fail_at_load(self):
        for raw in ({"name": "a", "ssh": "-oProxyCommand=calc"},
                    {"name": "a", "ssh": "me@h", "type": "openai", "api_key_env": "X"},
                    {"name": "a", "ssh": "me@h", "remote_port": 0},
                    {"name": "a"}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_config({"nodes": [raw]})

    def test_nodes_add_accepts_ssh_urls(self):
        self.assertEqual(parse_spec("gpu=ssh://gpu-box"), ("gpu", "ssh://gpu-box"))
        with self.assertRaises(ValueError):
            parse_spec("gpu=ssh://-oProxyCommand=x")

    def test_rendered_ssh_node_loads_back(self):
        probed = {"name": "gpu", "url": "ssh://fede@10.0.0.5:2222", "ssh": "fede@10.0.0.5:2222",
                  "ssh_key": "C:/Users/me/.routeai/ssh/id_ed25519_gpu", "remote_port": 11434, "reachable": True,
                  "models": {"code": ["qwen2.5-coder:14b"]}, "type": "ollama"}
        node = parse_config(tomllib.loads(render([probed]))).node("gpu")
        self.assertEqual((node.ssh, node.ssh_key, node.remote_port), (probed["ssh"], probed["ssh_key"], 11434))
        self.assertEqual(node.models["code"], ["qwen2.5-coder:14b"])

    def test_switching_an_http_node_to_ssh_keeps_its_tuning(self):
        text = render([{"name": "gpu", "url": "http://10.0.0.5:11434", "reachable": True,
                        "models": {"complex": ["qwen2.5-coder:14b"]}}])
        text = set_node_option(text, "gpu", "ssh", "fede@10.0.0.5")
        text = drop_node_option(text, "gpu", "url")
        node = parse_config(tomllib.loads(text)).node("gpu")
        self.assertEqual(node.url, "ssh://fede@10.0.0.5")
        self.assertEqual(node.models["complex"], ["qwen2.5-coder:14b"])
        self.assertNotIn("http://10.0.0.5", text)


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    return f'"{pid}"' in out.stdout or f",{pid}," in out.stdout.replace('"', "")


class TunnelTests(unittest.TestCase):
    """The real tunnel code driving a fake ssh that opens the local port and answers like Ollama."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("ROUTEAI_HOME", "ROUTEAI_SSH", "FAKE_SSH_MODE", "FAKE_SSH_LOG")}
        os.environ["ROUTEAI_HOME"] = self.tmp.name
        os.environ["ROUTEAI_SSH"] = FAKE_SSH
        os.environ["FAKE_SSH_LOG"] = str(Path(self.tmp.name) / "argv.jsonl")

    def tearDown(self):
        st.close_all()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def fleet(self, **node):
        cfg = parse_config({"nodes": [{"name": "gpu", "ssh": "fede@example.test", "tier": "heavy",
                                       "models": {"code": ["qwen2.5-coder:7b"]}, **node}]})
        return Fleet(cfg, Stats(Path(self.tmp.name) / "stats.json"))

    def test_node_is_reached_through_the_tunnel_and_reconnects(self):
        os.environ["FAKE_SSH_MODE"] = "ok"
        fleet = self.fleet()
        asyncio.run(fleet.refresh(force=True))
        state = fleet.state["gpu"]
        self.assertTrue(state.healthy, state.error)
        self.assertEqual(state.version, "fake-9.9")
        self.assertTrue(state.has("qwen2.5-coder:7b"))
        self.assertEqual(fleet.describe()[0]["transport"], "ssh")

        tunnel = fleet.clients["gpu"].tunnel
        first = tunnel.proc
        first.kill()
        first.wait(timeout=10)
        asyncio.run(fleet.refresh(force=True))
        self.assertTrue(fleet.state["gpu"].healthy, fleet.state["gpu"].error)
        self.assertIsNot(tunnel.proc, first)

        argv = json.loads((Path(self.tmp.name) / "argv.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2:], ["--", "fede@example.test"])

    def test_tunnels_are_closed_on_shutdown(self):
        os.environ["FAKE_SSH_MODE"] = "ok"
        fleet = self.fleet()
        asyncio.run(fleet.refresh(force=True))
        proc = fleet.clients["gpu"].tunnel.proc
        st.close_all()
        self.assertIsNotNone(proc.wait(timeout=10))

    @unittest.skipUnless(sys.platform == "win32", "job objects are a Windows mechanism")
    def test_tunnel_dies_when_routeai_is_killed(self):
        """A killed MCP server must not leave ssh processes behind holding ports and sessions."""
        os.environ["FAKE_SSH_MODE"] = "ok"
        pid_file = Path(self.tmp.name) / "ssh.pid"
        child = subprocess.Popen([sys.executable, "-c", (
            "import sys, time; sys.path.insert(0, sys.argv[1]);"
            "from routeai import sshtunnel as st;"
            "t = st.tunnel_for('gpu', 'fede@example.test'); t.ensure();"
            "open(sys.argv[2], 'w').write(str(t.proc.pid)); time.sleep(120)"),
            str(ROOT / "src"), str(pid_file)], stdin=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.2)
            ssh_pid = int(pid_file.read_text())
            self.assertTrue(_alive(ssh_pid))
            child.kill()  # like TerminateProcess: no atexit, no cleanup code runs
            child.wait(timeout=10)
            deadline = time.time() + 10
            while _alive(ssh_pid) and time.time() < deadline:
                time.sleep(0.2)
            self.assertFalse(_alive(ssh_pid), "ssh outlived the process that opened the tunnel")
        finally:
            if child.poll() is None:
                child.kill()

    def test_rejected_key_says_to_rerun_setup(self):
        os.environ["FAKE_SSH_MODE"] = "denied"
        fleet = self.fleet()
        asyncio.run(fleet.refresh(force=True))
        self.assertFalse(fleet.state["gpu"].healthy)
        self.assertIn("ssh-setup gpu", fleet.state["gpu"].error)

    def test_changed_host_key_blocks_and_says_so(self):
        os.environ["FAKE_SSH_MODE"] = "hostkey"
        fleet = self.fleet()
        asyncio.run(fleet.refresh(force=True))
        self.assertIn("ssh-forget gpu", fleet.state["gpu"].error)

    def test_missing_managed_key_is_reported_before_running_ssh(self):
        fleet = self.fleet(ssh_key=str(Path(self.tmp.name) / "nope"))
        asyncio.run(fleet.refresh(force=True))
        self.assertIn("run `routeai ssh-setup gpu", fleet.state["gpu"].error)
        self.assertFalse((Path(self.tmp.name) / "argv.jsonl").exists())

    def test_ollama_down_on_the_server(self):
        os.environ["FAKE_SSH_MODE"] = "noollama"
        fleet = self.fleet()
        asyncio.run(fleet.refresh(force=True))
        self.assertFalse(fleet.state["gpu"].healthy)
        deadline = time.time() + 5
        while "is Ollama running" not in (fleet.state["gpu"].error or "") and time.time() < deadline:
            asyncio.run(fleet.refresh(force=True))
        self.assertIn("nothing listens on 127.0.0.1:11434 of the server", fleet.state["gpu"].error)


if __name__ == "__main__":
    unittest.main()
