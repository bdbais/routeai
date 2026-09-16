"""Stands in for `ssh -N -L 127.0.0.1:LOCAL:127.0.0.1:REMOTE ... -- destination` in the tests.

FAKE_SSH_MODE:
  ok        listen on LOCAL and answer like Ollama
  denied    fail authentication, like a server that does not accept the key
  hostkey   fail on a changed host key
  noollama  authenticate and listen, but the forward is refused on the server side
FAKE_SSH_LOG, if set, receives the argv as one JSON line per run.
"""

import json
import os
import socket
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\n")

mode = os.environ.get("FAKE_SSH_MODE", "ok")
if mode == "denied":
    print("fede@example.test: Permission denied (publickey).", file=sys.stderr, flush=True)
    sys.exit(255)
if mode == "hostkey":
    print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
          "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
          "Host key verification failed.", file=sys.stderr, flush=True)
    sys.exit(255)

forward = args[args.index("-L") + 1]
local_port = int(forward.split(":")[1])

if mode == "noollama":
    server = socket.socket()
    server.bind(("127.0.0.1", local_port))
    server.listen()
    while True:
        conn, _ = server.accept()
        print("channel 2: open failed: connect failed: Connection refused", file=sys.stderr, flush=True)
        conn.close()


class Ollama(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = {"/api/version": {"version": "fake-9.9"},
                "/api/tags": {"models": [{"name": "qwen2.5-coder:7b", "capabilities": ["completion"],
                                          "details": {"parameter_size": "7.6B"}}]},
                "/api/ps": {"models": []}}.get(self.path)
        data = json.dumps(body or {}).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn() before listening, which can stall for tens of seconds
        # on CI runners without reverse DNS (macOS): bind like a plain TCP server instead.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


Server(("127.0.0.1", local_port), Ollama).serve_forever()
time.sleep(3600)
