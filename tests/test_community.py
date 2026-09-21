"""send-stats: what leaves the machine, the GitHub device flow, and talking to the site."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from routeai import community as com  # noqa: E402
from routeai.bench.suite import SUITE_VERSION  # noqa: E402
from routeai.config import parse_config  # noqa: E402

CFG = parse_config({"nodes": [
    {"name": "desktop-gpu", "url": "http://192.168.1.13:11434"},
    {"name": "laptop", "url": "http://localhost:11434"},
    {"name": "gemini", "type": "openai", "url": "https://example.invalid/v1", "api_key_env": "GEMINI_API_KEY"},
]})


def report(**over):
    base = {
        "generated_at": "2026-09-21 10:00:00",
        "suite_version": SUITE_VERSION,
        "table": [
            {"node": "desktop-gpu", "model": "Qwen2.5-Coder:14b", "category": "tests", "score": 1.0, "gen_tps": 49.4,
             "seconds": 11.0, "gpu": 1.0, "errors": 0, "runs": 2, "quant": "Q4_K_M", "params": "14.8B",
             "num_ctx": 8192},
            {"node": "laptop", "model": "qwen2.5-coder:3b", "category": "docs", "score": 0.9, "gen_tps": 14.5,
             "seconds": 15.0, "gpu": 0.0, "errors": 0, "runs": 1, "quant": "Q4_K_M", "params": "3.1B",
             "num_ctx": 8192},
            {"node": "gemini", "model": "gemini-2.5-flash", "category": "docs", "score": 1.0, "gen_tps": 120.0,
             "seconds": 2.0, "gpu": None, "errors": 0, "runs": 1, "quant": "unknown", "params": "",
             "num_ctx": 32768},
        ],
    }
    base.update(over)
    return base


class PayloadTests(unittest.TestCase):
    def test_only_the_allowed_fields_travel(self):
        payload = com.build_payload(report(), CFG, {"desktop-gpu": 12}, suite_versions=(SUITE_VERSION,))
        text = json.dumps(payload)
        for secret in ("desktop-gpu", "laptop", "192.168.1.13", "localhost", "11434", "seconds"):
            self.assertNotIn(secret, text, f"{secret} must not be sent")
        self.assertEqual(sorted(payload["rows"][0]),
                         ["category", "gen_tps", "gpu_ratio", "hw", "model", "num_ctx", "params", "quant", "runs",
                          "score"])
        by_hw = {r["hw"]: r for r in payload["rows"]}
        self.assertEqual(sorted(by_hw), ["cpu", "gpu12"])
        self.assertEqual(by_hw["gpu12"]["model"], "qwen2.5-coder:14b")  # normalised to lower case
        self.assertEqual(payload["bench_date"], "2026-09-21")

    def test_paid_providers_are_not_our_hardware(self):
        payload = com.build_payload(report(), CFG, {"desktop-gpu": 12}, suite_versions=(SUITE_VERSION,))
        self.assertNotIn("gemini-2.5-flash", json.dumps(payload))

    def test_failed_and_unknown_results_are_left_out(self):
        rows = report()["table"]
        rows[0]["errors"] = 1
        rows[1]["category"] = "nonsense"
        with self.assertRaises(com.CommunityError):
            com.build_payload(report(table=rows), CFG, {"desktop-gpu": 12}, suite_versions=(SUITE_VERSION,))

    def test_an_old_report_is_refused(self):
        with self.assertRaises(com.CommunityError):
            com.build_payload(report(suite_version=None), CFG, {}, suite_versions=(SUITE_VERSION,))
        with self.assertRaises(com.CommunityError):
            com.build_payload(report(suite_version="1999.01"), CFG, {}, suite_versions=(SUITE_VERSION,))

    def test_a_gpu_machine_without_a_declared_size_stops_the_send(self):
        with self.assertRaises(com.CommunityError) as caught:
            com.build_payload(report(), CFG, {}, suite_versions=(SUITE_VERSION,))
        self.assertIn("--vram desktop-gpu", str(caught.exception))
        self.assertEqual(com.nodes_needing_vram(report()), ["desktop-gpu"])

    def test_hardware_buckets_round_down(self):
        self.assertEqual(com.hardware_bucket([0.0, None], None), "cpu")
        self.assertEqual(com.hardware_bucket([1.0], 13), "gpu12")
        self.assertEqual(com.hardware_bucket([0.5], 24), "gpu24")
        self.assertEqual(com.hardware_bucket([0.5], 2), "gpu4")


class FakeSite(BaseHTTPRequestHandler):
    """Stands in for routeai.bais.info and for GitHub's device flow."""

    calls: list = []
    polls = 0

    def log_message(self, *_):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self._read()
        FakeSite.calls.append((self.path, body, self.headers.get("Authorization")))
        if self.path == "/login/device/code":
            return self._send(200, {"device_code": "dev", "user_code": "ABCD-1234", "interval": 0,
                                    "verification_uri": "https://github.com/login/device", "expires_in": 60})
        if self.path == "/login/oauth/access_token":
            FakeSite.polls += 1
            if FakeSite.polls == 1:
                return self._send(200, {"error": "authorization_pending"})
            if FakeSite.polls == 2:
                return self._send(200, {"error": "slow_down", "interval": 0})
            return self._send(200, {"access_token": "gho_fake"})
        if self.path == "/api/register":
            return self._send(200, {"certified": bool(body.get("github_token"))})
        if self.path == "/api/stats":
            if self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(200, {"accepted": len(body["rows"]), "outliers": 0, "certified": True})
            return self._send(401, {"error": "missing install token"})
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/stolen")
            self.end_headers()
            return
        self._send(404, {"error": "no"})

    def do_DELETE(self):
        FakeSite.calls.append((self.path, None, self.headers.get("Authorization")))
        self._send(200, {"deleted": 3})


class SiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeSite.calls, FakeSite.polls = [], 0
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("ROUTEAI_HOME", "ROUTEAI_COMMUNITY_URL", "ROUTEAI_GITHUB_URL")}
        os.environ["ROUTEAI_HOME"] = self.tmp.name
        os.environ["ROUTEAI_COMMUNITY_URL"] = com.SITE = self.base
        os.environ["ROUTEAI_GITHUB_URL"] = com.GITHUB = self.base

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_device_flow_waits_for_the_user_and_never_stores_the_token(self):
        token = com.sign_in_with_github(printer=lambda *_: None, sleep=lambda *_: None)
        self.assertEqual(token, "gho_fake")
        self.assertEqual(FakeSite.polls, 3)  # pending, slow_down, then the token
        answer = com.register(token)
        self.assertTrue(answer["certified"])
        state = com.load_state()
        self.assertTrue(state["install_token"])
        self.assertTrue(state["certified"])
        self.assertNotIn("gho_fake", com.state_path().read_text(encoding="utf-8"))

    def test_anonymous_registration_then_send_and_delete(self):
        com.register(None)
        state = com.load_state()
        self.assertFalse(state["certified"])
        payload = com.build_payload(report(), CFG, {"desktop-gpu": 12}, suite_versions=(SUITE_VERSION,))
        answer = com.send(payload, state)
        self.assertEqual(answer["accepted"], len(payload["rows"]))
        self.assertEqual(com.delete(state)["deleted"], 3)
        sent = [c for c in FakeSite.calls if c[0] == "/api/stats"][0]
        self.assertEqual(sent[2], f"Bearer {state['install_token']}")

    def test_a_refusal_from_the_site_is_reported_as_it_is(self):
        with self.assertRaises(com.CommunityError) as caught:
            com._call("POST", f"{self.base}/api/nope", {})
        self.assertIn("HTTP 404", str(caught.exception))

    def test_a_redirect_is_never_followed(self):
        with self.assertRaises(com.CommunityError):
            com._call("POST", f"{self.base}/moved", {})

    def test_the_user_declining_on_github_is_not_an_error_without_explanation(self):
        FakeSite.polls = 99
        original = FakeSite.do_POST

        def deny(handler):
            if handler.path == "/login/oauth/access_token":
                length = int(handler.headers.get("Content-Length") or 0)
                handler.rfile.read(length)
                return handler._send(200, {"error": "access_denied"})
            return original(handler)

        FakeSite.do_POST = deny
        try:
            with self.assertRaises(com.CommunityError) as caught:
                com.sign_in_with_github(printer=lambda *_: None, sleep=lambda *_: None)
            self.assertIn("declined", str(caught.exception))
        finally:
            FakeSite.do_POST = original


if __name__ == "__main__":
    unittest.main()
