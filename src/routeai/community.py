"""Share benchmark results with routeai.bais.info, by hand, and only what cannot identify you.

What travels: model tag, quantisation, parameter size, context, a hardware bucket (CPU, or GPU by VRAM),
category, benchmark score, tokens per second, GPU offload ratio, number of runs, suite and plugin versions.

What never travels, by construction: this file builds the payload field by field from a fixed list, so node
names, addresses, file paths, prompts, instructions, project names and task logs cannot leak into it even if
they are added to the benchmark report later.

Identity: nothing of the sort by default. The first send registers a random install token (kept in
~/.routeai/community.json) so a later send replaces this machine's results instead of piling up, and so
`--delete` can remove them. Signing in with GitHub (device flow, in the user's own terminal) marks the
installation as *certified*: the site keeps certified and uncertified statistics apart, and a ban can be made
to cost something. The GitHub token is used once to read the account id and is never stored.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import FleetConfig, fleet_home

SITE = os.environ.get("ROUTEAI_COMMUNITY_URL", "https://routeai.bais.info").rstrip("/")
GITHUB = os.environ.get("ROUTEAI_GITHUB_URL", "https://github.com").rstrip("/")
CLIENT_ID = os.environ.get("ROUTEAI_GITHUB_CLIENT_ID", "Iv23liNkFF34YSkSnUzn")

CATEGORIES = ("complex", "code", "tests", "scripts", "build", "docs", "general")
VRAM_BUCKETS = (4, 6, 8, 10, 11, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128)
TIMEOUT_S = 30.0


class CommunityError(RuntimeError):
    pass


def state_path() -> Path:
    return fleet_home() / "community.json"


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> Path:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:  # the install token is a credential: keep it to this account
        from .sshtunnel import restrict_private_key

        restrict_private_key(path)
    except Exception:
        pass
    return path


def forget_state() -> None:
    state_path().unlink(missing_ok=True)


# -- what gets sent ---------------------------------------------------------------------

def hardware_bucket(gpu_ratios: list[float | None], vram_gb: int | None) -> str:
    """'cpu' when nothing ran in VRAM, otherwise the GPU size rounded down to a bucket."""
    if not any(r for r in gpu_ratios if r):
        return "cpu"
    if not vram_gb:
        raise CommunityError("VRAM unknown")
    bucket = max((b for b in VRAM_BUCKETS if b <= vram_gb), default=VRAM_BUCKETS[0])
    return f"gpu{bucket}"


def nodes_needing_vram(report: dict) -> list[str]:
    by_node: dict[str, list] = {}
    for row in report.get("table", []):
        by_node.setdefault(row["node"], []).append(row.get("gpu"))
    return sorted(n for n, ratios in by_node.items() if any(r for r in ratios if r))


def build_payload(report: dict, cfg: FleetConfig, hardware: dict[str, int], *,
                  suite_versions: tuple[str, ...] = ()) -> dict:
    """One row per model/quantisation/hardware/category, from the benchmark report and nothing else."""
    suite = report.get("suite_version")
    if not suite or (suite_versions and suite not in suite_versions):
        raise CommunityError("this benchmark report predates community statistics: run /routeai:bench again")
    remote = {n.name for n in cfg.nodes if n.is_remote}
    rows, skipped = [], []
    for row in report.get("table", []):
        node = row.get("node", "")
        if node in remote:  # a paid provider's speed is the provider's, not your machine's
            continue
        if row.get("category") not in CATEGORIES or row.get("errors"):
            continue
        try:
            hw = hardware_bucket([row.get("gpu")], hardware.get(node))
        except CommunityError:
            skipped.append(node)
            continue
        rows.append({
            "model": str(row["model"]).strip().lower()[:100],
            "quant": str(row.get("quant") or "unknown")[:24],
            "params": str(row.get("params") or "")[:12],
            "num_ctx": int(row.get("num_ctx") or 0),
            "hw": hw,
            "category": row["category"],
            "score": round(float(row["score"]), 3),
            "gen_tps": round(float(row["gen_tps"]), 1),
            "gpu_ratio": None if row.get("gpu") is None else round(float(row["gpu"]), 2),
            "runs": int(row.get("runs") or 1),
        })
    if skipped:
        raise CommunityError(f"VRAM not declared for: {', '.join(sorted(set(skipped)))} "
                             f"(use --vram {skipped[0]}=12, in GB)")
    if not rows:
        raise CommunityError("nothing to share: run /routeai:bench first")
    return {
        "suite": suite,
        "routeai": __version__,
        "os": platform.system().lower()[:16],
        "bench_date": str(report.get("generated_at", ""))[:10],
        "rows": rows,
    }


def summarise(payload: dict) -> str:
    models = sorted({f"{r['model']} ({r['quant']}) on {r['hw']}" for r in payload["rows"]})
    return (f"{len(payload['rows'])} results, {len(models)} model/hardware combinations, "
            f"benchmark suite {payload['suite']}:\n  " + "\n  ".join(models))


# -- talking to the site ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow a redirect carrying our token
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _call(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": f"routeai/{__version__}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            message = json.loads(detail).get("error") or detail
        except ValueError:
            message = detail
        raise CommunityError(f"{url}: HTTP {exc.code} - {message}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CommunityError(f"{url}: {exc}") from None
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise CommunityError(f"{url}: the answer was not JSON") from None


# -- GitHub device flow (runs in the user's terminal, never through the chat) --------------

def start_device_flow() -> dict:
    return _call("POST", f"{GITHUB}/login/device/code",
                 {"client_id": CLIENT_ID})


def poll_device_flow(device: dict, printer=print, sleep=time.sleep) -> str:
    """Wait for the user to type the code on GitHub. Returns a short-lived user token."""
    interval = max(1, int(device.get("interval", 5)))
    deadline = time.time() + min(int(device.get("expires_in", 900)), 900)
    while time.time() < deadline:
        sleep(interval)
        answer = _call("POST", f"{GITHUB}/login/oauth/access_token",
                       {"client_id": CLIENT_ID, "device_code": device["device_code"],
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        error = answer.get("error")
        if not error and answer.get("access_token"):
            return answer["access_token"]
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = max(interval + 5, int(answer.get("interval", interval + 5)))
            continue
        if error == "access_denied":
            raise CommunityError("you declined the authorisation on GitHub")
        if error == "expired_token":
            raise CommunityError("the code expired: run the command again")
        raise CommunityError(f"GitHub refused the sign-in: {error}")
    raise CommunityError("timed out waiting for the GitHub authorisation")


def sign_in_with_github(printer=print, sleep=time.sleep) -> str:
    device = start_device_flow()
    printer(f"     open {device.get('verification_uri')} and type the code: {device.get('user_code')}")
    printer("     (RouteAI never sees your GitHub password; the token it receives is used once and discarded)")
    return poll_device_flow(device, printer, sleep)


# -- registration, sending, deleting -----------------------------------------------------

def register(github_token: str | None = None, state: dict | None = None) -> dict:
    state = dict(state or load_state())
    token = state.get("install_token") or secrets.token_urlsafe(32)
    answer = _call("POST", f"{SITE}/api/register",
                   {"github_token": github_token} if github_token else {}, token=token)
    state.update(install_token=token, certified=bool(answer.get("certified")),
                 registered_at=int(time.time()))
    save_state(state)
    return answer


def send(payload: dict, state: dict) -> dict:
    return _call("POST", f"{SITE}/api/stats", payload, token=state["install_token"])


def delete(state: dict) -> dict:
    return _call("DELETE", f"{SITE}/api/stats", None, token=state["install_token"])
