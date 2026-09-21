# Changelog

## 0.4.0 — 2026-09-21

- **`send-stats`**: share your benchmark results on <https://routeai.bais.info/community/>, by hand and only if
  you want to. The command prints the exact payload, asks, and `--delete` takes it down again. The payload is
  built from a fixed list of fields, so machine names, addresses, paths and prompts cannot leak into it.
- Results are published in two separate lists: **certified** (the install is signed in with GitHub through the
  device flow, so a block costs something) and **not certified** (anonymous). Both are visibly self-reported,
  with the number of contributors next to every line.
- The site gained a Community page in all 13 languages, served by a small Cloudflare Worker over D1: medians per
  model, quantisation, hardware bucket and category. No JavaScript, same strict policy as the rest of the site.
- Moderation: speeds far out of scale are flagged and excluded from the medians, three flagged submissions in a
  month earn an automatic 7-day block, and `scripts/stats-admin.py` bans or unbans a submitter by hand. IP
  addresses are never stored, only counted for a day.
- The benchmark report now records quantisation, parameter size, context and the suite version, so results from
  different people can be compared at all.

## 0.3.0 — 2026-09-16

- **Remote machines over SSH.** A node can be `ssh = "user@host"` instead of a URL: Ollama stays on `127.0.0.1`
  of the server and RouteAI reaches it through a tunnel it opens, keeps alive and reopens, with the system OpenSSH
  client and no new dependency. No open port, no proxy, no token.
- `python run.py ssh-setup NAME user@host` runs in your terminal: ssh asks for the password once, RouteAI creates a
  dedicated ed25519 key and installs it restricted (`permitopen="127.0.0.1:11434"`, no shell), pins the server's
  fingerprint, verifies the tunnel with the key alone and adds the node. `--use-ssh-config` reuses the keys and
  aliases you already have; nodes can also be added as `ssh://alias` from Claude.
- `ssh-check` diagnoses a node stage by stage (DNS, SSH port, authentication, Ollama) and every failure says what
  to do; `ssh-forget` drops a host key after a legitimate reinstall.
- On Windows tunnels belong to a job object, so they die with RouteAI even when it is killed; the private key gets
  an explicit ACL, because OpenSSH ignores keys other accounts can read.
- Fix: the usage report test assumed Windows path case folding and failed on Linux and macOS.

## 0.2.0 — 2026-09-16

- **RouteAI**: the plugin is no longer only about Ollama. Any OpenAI-compatible provider can join the fleet
  (Gemini, Groq, OpenRouter, DeepSeek, Mistral, OpenAI, vLLM, LM Studio): one adapter, model ids read from the
  provider itself, nothing hardcoded.
- `/routeai:add-ai <provider> <name>` adds one: the API key stays in an environment variable (never in
  `fleet.toml`, never in the chat), project files reach a provider only with `send_files = true`, and each node
  carries its price per million tokens and a daily cap (requests, tokens or USD).
- Cost-aware routing: free nodes first (your machines, free tiers), paid ones only when needed, and a node whose
  daily limit is reached is dropped until the counter resets.
- `/routeai:usage` reports tokens, cost and remaining quota per node; the benchmark leaves paid providers alone
  unless you opt in.
- Renamed from Ollama Fleet: package `routeai`, plugin `routeai@bais`, data in `~/.routeai`, launcher
  `bin/routeai`, environment variables `ROUTEAI_*`.

## 0.1.0 — 2026-09-15

First public release.

- Claude Code plugin with a dependency-free Python MCP server (stdio): `fleet_status`, `fleet_delegate`,
  `fleet_delegate_batch`, `fleet_job`, `fleet_feedback`, `fleet_bench`, `fleet_pull`.
- Priority routing by machine power: complex/code to the fast GPU node, tests/scripts/build/docs to lighter
  machines, overflow when the preferred tier is busy; learned quality and speed decide among equals.
- The server reads project files itself and can write results straight to disk, so Claude neither pastes
  inputs nor reads long outputs.
- Self-learning benchmark: 10 automatically graded tasks (hidden unit tests, mutation testing, script I/O,
  TOML/AST/JSON checks), VRAM-fit and parallel-throughput probes, recommendations for `fleet.toml`.
- Feedback loop: Claude rates delegated results (good / fixed / rejected) and routing adapts.
- Skills: `delegate` (when and how to delegate), `/routeai:status`, `/routeai:bench`.
- Guided setup: `/routeai:setup` and the `fleet_setup` tool probe your Ollama servers, pick models per
  category, suggest missing models (size, hardware, measured results) and pull them only with your approval.
- Cross-platform launcher (`bin/routeai` for macOS/Linux, `bin/routeai.cmd` for Windows) that finds a
  Python 3.11+ by itself; `ROUTEAI_PYTHON` forces one; a clear error on older Pythons.
- `/routeai:nodes` and the `fleet_nodes` tool add, remove, enable or disable one machine with a
  surgical edit of `fleet.toml`: the other machines keep their settings and comments, and a backup is kept.
- `/routeai:usage` and the `fleet_usage` tool report how much Ollama was used: tokens processed per machine
  and per model with a fleet total, plus the Claude tokens saved, for the current project (or all of them, over
  any period) and the tasks that cost more than they saved. Every delegation is logged with its project.
- `/routeai:queue` and the `fleet_queue` tool: Claude queues self-contained tasks and the fleet finishes
  them on its own - inside the running server or a standalone `python run.py queue work` - so the machines
  keep going while Claude is paused by a usage limit or a closed session. Claiming is an atomic rename, so
  several workers never run the same task twice.
- Token savings per task: every result (and every finished batch) carries `tokens_saved` — this task, the session
  and all-time — and Claude reports it to the user; `status` shows the running total.
- Fixed: benchmark grading timed out on Windows when run from the MCP server (child processes inherited the
  JSON-RPC stdin pipe); fixed stdout draining on exit; MCP server declared in plugin.json, not a root `.mcp.json`.
- Hardening from a pre-release security review: answers cut off at `max_output_tokens` (or with an unclosed
  code block) are never written; `stats.json` is shared safely between sessions (lock file, a failed read never
  overwrites learned data); one failing task can no longer stop a batch, a benchmark or the server; malformed
  MCP input is answered with JSON-RPC errors; writes into `.git/`, `.claude/`, `node_modules/` and similar are
  refused; globs skip dotfiles and secret-looking files; UNC/network paths are refused before being touched;
  redirects are never followed (a node's bearer token cannot leak); cloud detection is case-insensitive and
  covers aliases of ollama.com models; the benchmark grader caps output, kills the whole process tree and gives
  model code a throwaway home; long single tasks return their full answer through `fleet_job`; batch polls
  return failures only and are counted against the savings.
- Safety: workspace-confined file access, `:cloud` models excluded by default, no proxy for LAN traffic,
  bearer-token support for remote nodes.
