# RouteAI

<!-- mcp-name: io.github.bdbais/routeai -->

[![CI](https://github.com/bdbais/routeai/actions/workflows/ci.yml/badge.svg)](https://github.com/bdbais/routeai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Donate](https://img.shields.io/badge/donate-PayPal-0070ba.svg)](https://paypal.me/bellizia)

**A Claude Code plugin that routes small coding jobs to the AIs you already have — the Ollama machines on your network and the free tiers of providers like Gemini, Groq or OpenRouter — while Claude decides what to delegate and checks every result. Fewer Claude tokens, work in parallel, and free capacity used before paid.**

Website, in 13 languages: **https://routeai.bais.info**

```
Claude Code ──(MCP, stdio)──► RouteAI ──► desktop with GPU     complex code, functions
   decides, briefs,             router        laptop (CPU)         tests, scripts, build, docs
   reviews, gives feedback      learned        Gemini / Groq free   docs, extraction, overflow
                                stats + cost   any paid API         only when you allow it
```

## Why

Claude is the best engineer on the team, and the most expensive. Plenty of the work in a coding session
is well defined and easy to check: unit tests for a module, a CLI script, docstrings, a CI workflow, a
`pyproject.toml`, working out why a build failed, or the same mechanical edit on forty files. A 7–30B model
running at home does these for free, while Claude keeps the design, the hard parts and the final review.

## What it does

- **Priority routing by machine power.** Each task category prefers a tier. `complex` and `code` go to the
  fast GPU machine; `tests`, `scripts`, `build` and `docs` go to the slower machines. A task only moves to
  the other tier when the preferred machines are busy.
- **It learns as it goes.** Among suitable candidates the router ranks by measured quality² ÷ expected
  time. Expected time includes prompt processing, generation, model load or swap, and queue wait.
  Quality comes from the benchmark and from Claude's feedback on real work.
- **A benchmark that grades itself**, meant to be run from time to time until the fleet "knows" your
  hardware. There are 10 tasks, each checked automatically:
  - code is run against hidden unit tests;
  - generated tests are mutation-tested against 4 buggy variants;
  - scripts are run with sample input and their output compared;
  - config files are checked by parsing the TOML;
  - docstrings are checked with AST comparison;
  - log diagnoses are checked against a strict JSON schema.

  Probes also measure how much of each model sits in VRAM and how throughput scales with parallel
  requests. The result is a report with concrete suggestions for `fleet.toml`.
- **Claude never pastes files.** The server reads project files itself (paths or globs), can write results
  straight to disk, and returns only a preview.
- **Parallel batches.** One instruction runs over many files, spread across every machine.
- **Claude stays in charge.** The benchmark only suggests configuration changes; Claude proposes them and
  you approve them.
- **No dependencies.** It needs only Python 3.11+ and Ollama. No pip install and no SDK.

## Adding another AI

Beyond local machines, RouteAI talks to any OpenAI-compatible provider — Gemini, Groq, OpenRouter, DeepSeek,
Mistral, OpenAI, or your own vLLM/LM Studio endpoint:

```
/routeai:add-ai gemini gem1
```

Three rules keep it safe and cheap:

- **The key never touches the configuration or the chat.** You put it in an environment variable
  (`GEMINI_API_KEY`, `GROQ_API_KEY`, …) and the node stores only that variable's name.
- **Your files stay home unless you say otherwise.** A provider only receives project files when its node has
  `send_files = true`; without it, it is skipped for any task that includes files.
- **Free first, then paid, then nothing.** Set `[nodes.cost]` (USD per million tokens, 0 for a free tier) and a
  `[nodes.limits]` cap: daily requests or tokens for free tiers, `daily_cost_usd` for paid ones. The router
  prefers free nodes, stops using a node when its limit is reached, and falls back to your own machines.

`/routeai:usage` then shows tokens, cost and remaining quota per node.

## Requirements

- Claude Code
- Python 3.11 or newer. The launcher in `bin/` finds it by itself: `py -3` or `python` on Windows,
  `python3.14`…`python3.11`, `python3` or `python` on macOS/Linux. Set `ROUTEAI_PYTHON` to force one.
  (The system `python3` of macOS is 3.9: install a newer one, e.g. with Homebrew.)
- One or more machines running [Ollama](https://ollama.com). On machines other than the one running
  Claude Code, set `OLLAMA_HOST=0.0.0.0` so Ollama listens on the LAN.

Tested end to end on Windows 11 with two Ollama machines; the unit tests run on Windows, macOS and Linux in CI.

## Install

In Claude Code:

```
/plugin marketplace add bdbais/routeai
/plugin install routeai@bais
```

Trying it out to review it? [`REVIEWER-GUIDE.md`](REVIEWER-GUIDE.md) is a 20-minute test plan: what to run,
what to look for, the known limits, and what makes a bug report actionable.

Then, in a new session, describe your machines:

```
/routeai:setup gpu=http://192.168.1.13:11434 local=http://localhost:11434
```

Setup probes each Ollama server, picks the installed models per category (models are set **per machine and per
category**, because the right model depends on each machine's power), lists recommended models that are missing
with their size and hardware needs — and downloads them only if you say so — then writes
`~/.routeai/fleet.toml`. You can also edit that file by hand; see [`config/fleet.example.toml`](config/fleet.example.toml).
With no config at all, the plugin uses the local Ollama only.

Let the fleet learn your hardware, then check routing and savings:

```
/routeai:bench
/routeai:status
```

## Using it

Every finished task reports how many Claude tokens it saved (this task, session and all-time), and Claude tells
you in one line. It is an estimate: the tokens of the files the worker read plus the tokens it generated, minus
the brief Claude wrote and the result Claude read. Tiny tasks can come out negative — delegation pays off on
substantial inputs/outputs and on batches, where one brief covers many files. Measured on the test fleet:
pytest tests for a ~100-line module saved ~1,200 Claude tokens (15 s on the GPU box); Markdown references for
two modules, as a batch split across both machines, saved ~4,800.

You don't have to do anything special. The `delegate` skill teaches Claude when a sub-task is worth
delegating, how to write a self-contained brief, and how to verify the result. You can also ask directly:
*"use the fleet to write tests for every module in `src/parsers/`"*.

| Tool | Purpose |
|---|---|
| `fleet_status` | Shows node health, loaded and missing models, where each category routes, tokens saved, and whether a benchmark or setup is due |
| `fleet_setup` | Probes your Ollama servers, picks models per category, suggests models to pull, writes `fleet.toml` |
| `fleet_nodes` | Adds, removes, enables or disables one machine, keeping the other machines' settings |
| `fleet_usage` | Ollama tokens processed per machine and model, with totals, plus the Claude tokens saved |
| `fleet_queue` | Queues work the fleet finishes on its own while Claude is paused; lists and clears it |
| `fleet_delegate` | Runs one task: `instruction`, `category`, `files`, `context`, `output_path`, `json_schema` |
| `fleet_delegate_batch` | Runs the same instruction on many files in parallel, e.g. `output_pattern: "tests/test_{stem}.py"` |
| `fleet_job` | Reports progress and results of a batch, a long task or a benchmark |
| `fleet_feedback` | Records `good` / `fixed` / `rejected` for a result, which trains the router |
| `fleet_bench` | Runs the self-learning benchmark (`quick` or `full`, `explore`, `deep`) |
| `fleet_pull` | Downloads a model onto a node |

The same features are available from the command line (`bin/routeai` is also on the PATH of Claude's
Bash tool while the plugin is enabled):

```bash
python run.py init --node gpu=http://192.168.1.13:11434   # same as /routeai:setup
python run.py status                         # health + routing preview + savings + bench advice
python run.py nodes add gpu2=http://192.168.1.20:11434   # also: list, remove, enable, disable
python run.py usage --days 7                 # Ollama tokens per machine + savings, last week
python run.py queue work                     # keep working the queue while Claude is paused
python run.py bench --mode quick --deep      # graded benchmark + parallel probe
python run.py bench --mode full --explore    # also try installed models you haven't configured
```

Stats, the task log and reports are kept in `~/.routeai/`.

## How often to benchmark

`fleet_status` tells you when a run is due:

- **While learning** (a model/category pair has fewer than 5 samples, or its recent scores disagree):
  every 2–3 days, or after adding models.
- **Once stable:** monthly, or after a hardware change.

Every real task also updates speed statistics, and every `fleet_feedback` updates quality. The benchmark is
how new models and machines earn trust quickly.

## Tips from real hardware

- **Pin `num_ctx`.** Ollama's default context can push a model out of VRAM; on a 12 GB GPU,
  `qwen2.5-coder:14b` went from 7 to 49 tokens/s at `num_ctx = 8192`. Context is fixed per model on purpose,
  because changing it between requests forces a reload.
- **MoE models suit small GPUs.** `qwen3-coder:30b` still produces about 28 tokens/s when half of it is
  offloaded to the CPU.
- **CPU-only laptops still help.** Give them small coder models (3–7B) for scripts, tests and docs, and
  `max_parallel = 1` so your own work stays responsive.
- **Rented servers:** use `api_key_env` behind a VPN or an authenticating reverse proxy. Ollama has no
  authentication of its own.

## Security

- Provider keys are read from environment variables only: they are never written to `fleet.toml`, never
  logged, and requests never follow redirects, so a key cannot be forwarded elsewhere.
- A remote provider receives project files only if you set `send_files = true` for it.
- The server only reads and writes files inside the project, plus any `allowed_roots` you list (explicit paths;
  globs are relative to the project). Network (UNC) paths are refused before they are touched.
- Worker output is written before Claude reviews it, so it is never written into `.git/`, `.claude/`,
  `node_modules/`, virtualenvs or editor folders, where a file could run code or change tool settings.
- Globs skip dotfiles and secret-looking files (`.env`, `*.pem`, `*.key`, credentials…); list such a file
  explicitly only if you really want a local model to see it.
- An answer cut off at `max_output_tokens` is reported as a failure and never written over a file.
- Requests to nodes never follow redirects, so a node's bearer token cannot be forwarded elsewhere.
- `:cloud` models, which run on ollama.com, are never used unless `allow_cloud_models = true`.
- Traffic to nodes bypasses system HTTP proxies, so LAN requests are not sent through a corporate proxy.
- The benchmark runs code written by *your* local models in a temporary folder with a timeout and a
  minimal environment. That is enough for its small, fixed prompts, but it is not a security sandbox.
- Don't delegate secrets. The `delegate` skill tells Claude never to send `.env` or credential files.

## Development

Install or refresh the plugin from a local checkout on Windows, including validation, tests, the fleet config
and a server smoke test (run it again after every change; `-Uninstall` removes it):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

```bash
python -m unittest discover -s tests -v      # no Ollama needed (live tests are skipped)
ROUTEAI_LIVE=1 python -m unittest tests.test_live -v   # drives the real MCP server against your fleet
node site/build.mjs                          # showcase site; fails on any missing translation
claude --plugin-dir .                        # try the plugin from a checkout
```

The live test uses your `fleet.toml` but a temporary home and project, so your learned statistics stay untouched.

## Support

If RouteAI saves you tokens, you can [buy me a coffee on PayPal](https://paypal.me/bellizia) ☕ or star the repo.

Independent open-source project, not affiliated with Anthropic or Ollama. MIT licensed.
