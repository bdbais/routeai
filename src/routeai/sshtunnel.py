"""Reach an Ollama that only listens on 127.0.0.1 of a remote machine, through an SSH tunnel.

Ollama has no authentication. Instead of exposing it and building a proxy with tokens on top, RouteAI
forwards a local port inside SSH - the same channel you already use to administer that machine:

    127.0.0.1:<free port> here  ==ssh==>  127.0.0.1:11434 on the server

The tunnel is opened by the system `ssh` client (no Python dependency) with BatchMode, so it can never
hang on a password prompt nobody can answer. The password is asked once, by `ssh` itself, in the user's
own terminal (`routeai ssh-setup`), only to install a dedicated key that can open this forward and
nothing else. RouteAI never sees or stores it.
"""

from __future__ import annotations

import atexit
import base64
import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import fleet_home
from .ollama_client import OllamaClient, OllamaError

DEFAULT_REMOTE_PORT = 11434
CONNECT_TIMEOUT_S = 10
START_TIMEOUT_S = 20.0

_USER = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9._-]{1,253}$")          # names, IPv4, ~/.ssh/config aliases
_IPV6 = re.compile(r"^[0-9A-Fa-f:.%a-zA-Z]{2,64}$")
_NODE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class TunnelError(RuntimeError):
    pass


def ssh_dir() -> Path:
    return fleet_home() / "ssh"


def known_hosts() -> Path:
    """RouteAI's own known_hosts: the fingerprint confirmed at setup, never silently replaced."""
    return ssh_dir() / "known_hosts"


def _known_hosts_option() -> str:
    # ssh splits -o values on whitespace: quote the path, with forward slashes (accepted on Windows too)
    return f'UserKnownHostsFile="{known_hosts().as_posix()}"'


def key_path(node_name: str) -> Path:
    if not _NODE_NAME.match(node_name):
        raise ValueError(f"node name {node_name!r}: use letters, digits, '.', '_' or '-'")
    return ssh_dir() / f"id_ed25519_{node_name}"


@dataclass(frozen=True)
class SshTarget:
    user: str | None
    host: str
    port: int | None = None

    @property
    def is_alias(self) -> bool:
        """A bare name with no user or port may be a Host entry of ~/.ssh/config."""
        return self.user is None and self.port is None

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (f"{self.user}@" if self.user else "") + host + (f":{self.port}" if self.port else "")


def parse_target(spec: str) -> SshTarget:
    """`user@host`, `user@host:2222`, `ssh://user@host:2222`, `user@[::1]:22` or an ~/.ssh/config alias.

    Everything here ends up on the ssh command line, so anything that could be read as an option
    (a leading '-', spaces, quotes) is refused rather than escaped."""
    raw = spec.strip()
    if raw.lower().startswith("ssh://"):
        raw = raw[6:].rstrip("/")
    if not raw or raw.startswith("-") or any(c.isspace() for c in raw):
        raise ValueError(f"not an SSH target: {spec!r} (expected user@host, user@host:port or an ssh config alias)")
    user = None
    if "@" in raw:
        user, _, raw = raw.rpartition("@")
        if not _USER.match(user):
            raise ValueError(f"invalid SSH user in {spec!r}")
    port = None
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise ValueError(f"invalid SSH target {spec!r}")
            port = int(rest[1:])
        if not _IPV6.match(host):
            raise ValueError(f"invalid IPv6 address in {spec!r}")
    else:
        host, sep, port_text = raw.partition(":")
        if sep:
            if not port_text.isdigit():
                raise ValueError(f"invalid SSH port in {spec!r}")
            port = int(port_text)
        if not _HOST.match(host) or host.startswith("-"):
            raise ValueError(f"invalid SSH host in {spec!r}")
    if port is not None and not 0 < port < 65536:
        raise ValueError(f"SSH port out of range in {spec!r}")
    return SshTarget(user, host, port)


def ssh_command() -> list[str]:
    """The ssh client to run. ROUTEAI_SSH may name another binary, or be a JSON list (used by the tests)."""
    override = os.environ.get("ROUTEAI_SSH", "").strip()
    if override:
        if override.startswith("["):
            return [str(part) for part in json.loads(override)]
        return [override]
    found = shutil.which("ssh")
    if not found:
        raise TunnelError("the 'ssh' client was not found: install OpenSSH (Windows: Settings > Optional features)")
    return [found]


def tunnel_args(target: SshTarget, local_port: int, remote_port: int, key: Path | None) -> list[str]:
    args = ["-N", "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}"]
    if key is not None:
        # A key managed by RouteAI: only that key, only the host fingerprint confirmed at setup.
        args += ["-i", str(key), "-o", "IdentitiesOnly=yes",
                 "-o", _known_hosts_option(), "-o", "StrictHostKeyChecking=yes"]
    # else: the user's own ~/.ssh/config, agent and known_hosts decide, exactly as for their own `ssh`.
    if target.port:
        args += ["-p", str(target.port)]
    return args + ["--", target.destination]


def authorized_key_line(public_key: str, remote_port: int, restricted: bool = True) -> str:
    """The line installed on the server. Restricted: the key can open this one forward - no shell,
    no other ports, no agent or X11 - so a stolen key reaches Ollama and nothing else."""
    public_key = public_key.strip()
    if not restricted:
        return public_key
    return (f'restrict,port-forwarding,permitopen="127.0.0.1:{remote_port}",command="/bin/false" '
            f"{public_key}")


INSTALLED_MARK = "ROUTEAI_KEY_INSTALLED"


def install_script(line: str) -> str:
    """Remote command that appends the line to authorized_keys once. The line travels base64-encoded, so no
    quoting can break it, and the whole script runs under sh whatever the user's login shell is."""
    encoded = base64.b64encode(line.encode("utf-8")).decode("ascii")
    script = (f'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
              f'L="$(echo {encoded} | base64 -d)" && '
              f'{{ grep -qxF "$L" "$HOME/.ssh/authorized_keys" || echo "$L" >> "$HOME/.ssh/authorized_keys"; }} && '
              f"echo {INSTALLED_MARK}")
    assert "'" not in script
    return f"sh -c '{script}'"


_HINTS = [
    (("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed", "host key for"),
     "the server's host key does not match the one confirmed at setup. If you reinstalled that machine, run "
     "`routeai ssh-forget {node}` and then `routeai ssh-setup` again; otherwise do not connect"),
    (("Permission denied", "Too many authentication failures"),
     "the server did not accept the key: run `routeai ssh-setup {node} <user@host>` in your terminal"),
    (("administratively prohibited",),
     "SSH works but the server refused the forward to 127.0.0.1:{port} (permitopen or AllowTcpForwarding)"),
    (("open failed: connect failed", "channel 2: open failed", "open failed"),
     "SSH works but nothing listens on 127.0.0.1:{port} of the server: is Ollama running there? "
     "(`systemctl status ollama`)"),
    (("Could not resolve hostname", "Name or service not known", "nodename nor servname"),
     "the host name does not resolve: check the address"),
    (("Connection refused",),
     "nothing answers on the SSH port: is sshd running, and is the port right?"),
    (("Connection timed out", "Operation timed out", "timed out"),
     "the machine does not answer: wrong address, firewall, or it is off"),
    (("Address already in use", "cannot listen to port", "Could not request local forwarding"),
     "the local port was taken; retrying on another one"),
    (("UNPROTECTED PRIVATE KEY FILE", "bad permissions"),
     "the private key file is readable by other users: restrict its permissions"),
    (("Load key", "invalid format", "No such file"),
     "the SSH key file is missing or unreadable: run `routeai ssh-setup` again"),
]


def known_problem(stderr: str, node: str = "<node>", port: int = DEFAULT_REMOTE_PORT) -> str | None:
    for needles, hint in _HINTS:
        if any(n.lower() in stderr.lower() for n in needles):
            return hint.format(node=node, port=port)
    return None


def classify(stderr: str, node: str = "<node>", port: int = DEFAULT_REMOTE_PORT) -> str:
    """Turn ssh's stderr into one line that says which stage failed and what to do."""
    hint = known_problem(stderr, node, port)
    if hint:
        return hint
    last = [line for line in stderr.strip().splitlines() if line.strip()]
    return last[-1].strip() if last else "ssh exited without saying why"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# -- keep ssh from outliving RouteAI -------------------------------------------------------
# Windows: every tunnel joins a job object that kills its members when this process ends, even if it is
# killed. Elsewhere tunnels are closed at normal exit.

_job_handle = None


def _bind_to_this_process(proc: subprocess.Popen) -> None:
    global _job_handle
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job_handle is None:
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in
                            ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                             "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            _job_handle = job
        kernel32.AssignProcessToJobObject(wintypes.HANDLE(_job_handle), wintypes.HANDLE(int(proc._handle)))
    except Exception:  # best effort: atexit still closes tunnels on a normal exit
        pass


class Tunnel:
    """One `ssh -N -L` process, opened on first use and reopened when it drops."""

    def __init__(self, node: str, target: SshTarget, remote_port: int = DEFAULT_REMOTE_PORT,
                 key: Path | None = None):
        self.node, self.target, self.remote_port, self.key = node, target, remote_port, key
        self.local_port: int | None = None
        self.proc: subprocess.Popen | None = None
        self.stderr = collections.deque(maxlen=40)
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"ssh://{self.target}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def recent_errors(self) -> str:
        return "\n".join(self.stderr)

    def url(self) -> str:
        self.ensure()
        return f"http://127.0.0.1:{self.local_port}"

    def ensure(self) -> None:
        with self._lock:
            if self.alive() and self.local_port and _port_open(self.local_port):
                return
            self._stop()
            if self.key is not None and not self.key.exists():
                raise TunnelError(f"{self.label}: key {self.key} not found - run `routeai ssh-setup {self.node} "
                                  f"{self.target}` in your terminal")
            last = ""
            for _ in range(3):  # a port picked as free can be taken before ssh binds it
                last = self._start()
                if not last:
                    return
                if "local port was taken" not in last:
                    break
            raise TunnelError(f"{self.label}: {last}")

    def _start(self) -> str:
        self.local_port = _free_port()
        self.stderr.clear()
        cmd = ssh_command() + tunnel_args(self.target, self.local_port, self.remote_port, self.key)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # stdin must not be inherited: inside the MCP server it is the JSON-RPC pipe.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=flags)
        _bind_to_this_process(self.proc)
        proc = self.proc
        self._reader = threading.Thread(target=self._drain, args=(proc,), daemon=True)
        self._reader.start()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                time.sleep(0.1)  # let the reader collect the last lines
                return classify(self.recent_errors(), self.node, self.remote_port)
            if _port_open(self.local_port):
                return ""
            time.sleep(0.1)
        self._stop()
        return f"no tunnel after {START_TIMEOUT_S:.0f}s ({classify(self.recent_errors(), self.node, self.remote_port)})"

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                self.stderr.append(raw.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):  # pipe closed by _stop
            pass

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        reader = getattr(self, "_reader", None)
        if reader is not None:
            reader.join(timeout=2)  # the pipe reaches EOF once ssh is gone
        if proc.stderr is not None:
            proc.stderr.close()

    def close(self) -> None:
        with self._lock:
            self._stop()


_tunnels: dict[tuple, Tunnel] = {}
_tunnels_lock = threading.Lock()


def tunnel_for(node_name: str, ssh: str, remote_port: int = DEFAULT_REMOTE_PORT, key: str | None = None) -> Tunnel:
    """One tunnel per destination, shared across configuration reloads."""
    target = parse_target(ssh)
    key_file = Path(key).expanduser() if key else None
    ident = (str(target), remote_port, str(key_file))
    with _tunnels_lock:
        if ident not in _tunnels:
            _tunnels[ident] = Tunnel(node_name, target, remote_port, key_file)
        return _tunnels[ident]


def close_all() -> None:
    with _tunnels_lock:
        tunnels = list(_tunnels.values())
        _tunnels.clear()
    for t in tunnels:
        t.close()


atexit.register(close_all)


class SshOllamaClient(OllamaClient):
    """The Ollama client, with every request going through the node's tunnel."""

    def __init__(self, tunnel: Tunnel, headers: dict[str, str] | None = None, timeout: float = 900.0):
        super().__init__(tunnel.label, headers, timeout)
        self.tunnel = tunnel

    def _base(self) -> str:
        try:
            return self.tunnel.url()
        except TunnelError as exc:
            raise OllamaError(str(exc)) from None

    def _where(self) -> str:
        return self.tunnel.label

    def _transport_error(self, path: str, exc: Exception) -> str:
        # The tunnel is up but the request failed: usually Ollama is not running on the server, or the server
        # refused the forward. ssh says which on its stderr.
        hint = known_problem(self.tunnel.recent_errors(), self.tunnel.node, self.tunnel.remote_port)
        if hint:
            return f"{self.tunnel.label}{path}: {hint}"
        return (f"{self.tunnel.label}{path}: SSH is connected but Ollama does not answer on "
                f"127.0.0.1:{self.tunnel.remote_port} of the server (is it running? `systemctl status ollama`) "
                f"- {exc}")


# -- first-time setup, in the user's terminal ---------------------------------------------------

def check_reachable(target: SshTarget, timeout: float = 5.0) -> list[tuple[str, bool, str]]:
    """DNS, then the TCP port, before anything involves ssh. Aliases are resolved by ssh itself."""
    if target.is_alias and "." not in target.host:
        return [("name", True, f"{target.host} (resolved by your ssh config)")]
    stages = []
    port = target.port or 22
    try:
        infos = socket.getaddrinfo(target.host, port, type=socket.SOCK_STREAM)
        stages.append(("DNS", True, ", ".join(sorted({i[4][0] for i in infos}))))
    except OSError as exc:
        return [("DNS", False, f"{target.host} does not resolve ({exc})")]
    try:
        with socket.create_connection((target.host, port), timeout=timeout):
            stages.append((f"TCP {port}", True, "the SSH port answers"))
    except OSError as exc:
        stages.append((f"TCP {port}", False, f"no answer on port {port} ({exc})"))
    return stages


def public_key_path(private: Path) -> Path:
    return private.with_name(private.name + ".pub")  # not with_suffix: node names may contain dots


def restrict_private_key(path: Path) -> None:
    """OpenSSH ignores a private key that other accounts can read. On Windows the file inherits the folder's
    ACL, which can include extra groups: replace it with the current user and SYSTEM only."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    who = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip('"')
    if not sid.startswith("S-1-"):
        raise TunnelError(f"could not read the current user's SID to protect {path}")
    result = subprocess.run([str(system32 / "icacls.exe"), str(path), "/inheritance:r",
                             "/grant:r", f"*{sid}:F", "*S-1-5-18:F"],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise TunnelError(f"could not restrict the permissions of {path}: {(result.stderr or result.stdout).strip()}")


def generate_key(node_name: str) -> tuple[Path, bool]:
    """A dedicated ed25519 key for this node, readable by the current user only. Returns (path, created)."""
    path = key_path(node_name)
    created = False
    if not (path.exists() and public_key_path(path).exists()):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            raise TunnelError("'ssh-keygen' was not found: install the OpenSSH client")
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(path.parent, 0o700)
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"routeai-{node_name}", "-f", str(path)],
                       check=True, stdin=subprocess.DEVNULL)
        created = True
    restrict_private_key(path)
    return path, created


def install_key_interactively(target: SshTarget, public_key: str, remote_port: int, restricted: bool) -> bool:
    """Runs ssh attached to the user's terminal: ssh asks to confirm the host fingerprint and for the password
    (or uses a key the user already has). Nothing typed there passes through RouteAI."""
    known_hosts().parent.mkdir(parents=True, exist_ok=True)
    cmd = ssh_command() + ["-o", _known_hosts_option(), "-o", "StrictHostKeyChecking=ask",
                           "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}"]
    if target.port:
        cmd += ["-p", str(target.port)]
    cmd += ["--", target.destination, install_script(authorized_key_line(public_key, remote_port, restricted))]
    result = subprocess.run(cmd, stdout=subprocess.PIPE)  # stdin and stderr stay on the terminal
    return result.returncode == 0 and INSTALLED_MARK in result.stdout.decode("utf-8", "replace")


def forget_host(target: SshTarget) -> bool:
    keygen = shutil.which("ssh-keygen")
    if not keygen or not known_hosts().exists():
        return False
    host = f"[{target.host}]:{target.port}" if target.port and target.port != 22 else target.host
    result = subprocess.run([keygen, "-R", host, "-f", str(known_hosts())], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0
