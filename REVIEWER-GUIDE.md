# Reviewing RouteAI

A 20-minute test plan for anyone who agreed to try this plugin and say publicly whether it is any good.
Everything below is meant to be run without me in the room. If something does not work as written, that is a bug
in this guide as much as in the code — please report it.

**What RouteAI is:** a Claude Code plugin. Claude keeps the hard parts of a job and hands the small, verifiable
ones — unit tests, scripts, docstrings, build files, bulk per-file edits — to AIs you already have: the Ollama
models on your own machines and the free daily tiers of providers like Gemini, Groq or OpenRouter. It routes by
machine power and by cost, checks every result, reports how many Claude tokens each task saved, and a self-grading
benchmark learns which model to trust for which kind of task.

**What I would like judged:** whether the savings are real and honestly reported, whether the routing decisions
make sense on *your* hardware, and whether the first ten minutes are confusing.

---

## 0. What you need

Python 3.11+ and Claude Code. Then **one** of these three setups — all three are supported, pick the cheapest for
you:

| Setup | What you need | What to expect |
|---|---|---|
| **A — two machines** (the design target) | Ollama on a GPU box and on a second computer, with `OLLAMA_HOST=0.0.0.0` on the remote one | Routing by power: heavy work on the GPU, tests/scripts/build on the slower box, batches in parallel |
| **B — one machine** | Ollama on your laptop | Everything works; routing is trivial with one node. Judge the savings and the quality gates, not the routing |
| **C — no Ollama at all** | A free API key, e.g. Gemini or Groq | `/routeai:add-ai gemini gem1`, key in an environment variable. Fast even on a thin laptop |

On a CPU-only laptop use small models (3B–7B); a 7B answers a test-writing task in roughly 30–60 s. On a 12 GB
GPU, `qwen2.5-coder:14b` runs at about 50 tok/s and answers in 10–20 s.

## 1. Install (two commands)

In Claude Code:

```
/plugin marketplace add bdbais/routeai
/plugin install routeai@bais
```

Restart the session — plugins load at session start. `/routeai:` should now autocomplete to eight commands
(`setup`, `add-ai`, `nodes`, `delegate`, `queue`, `usage`, `bench`, `status`).

No pip install, no SDK, no Docker: the server is pure standard library and talks to Claude Code over stdio.

## 2. Setup (one minute)

```
/routeai:setup
```

or, if you have more than one machine:

```
/routeai:setup gpu=http://192.168.1.13:11434 local=http://localhost:11434
```

It probes each Ollama server, reads which models are actually installed, picks one per category **per machine**,
lists recommended models that are missing with their size and hardware needs — and pulls them **only if you say
yes** — then writes `~/.routeai/fleet.toml`, which you can also edit by hand. With no configuration at all it
uses your local Ollama.

On setup **C** (no Ollama), do this instead, after putting the key in an environment variable:

```
setx GEMINI_API_KEY "..."     # Windows, then restart the session · macOS/Linux: export it in your shell rc
/routeai:add-ai gemini gem1
```

The key is never written to `fleet.toml` and never echoed in the chat — only the variable's name is stored.

## 3. Seven things to try

**1. One real task.** Point Claude at a module of yours of about 100 lines:

> *use the fleet to write pytest tests for `src/whatever.py`*

Look for: a self-contained brief (Claude should not paste your whole file into the chat), the result written to
disk, Claude reviewing it, and a closing line such as *"saved ~1,200 Claude tokens"*. On a GPU box this takes
10–20 s; on a CPU laptop 40–90 s.

**2. A batch.** *"use the fleet to write a Markdown reference for every module in `src/`"* — one brief, many
files, run in parallel across your machines. This is where the savings get big (about 4,800 tokens for two
modules on my fleet). Check that the per-file results are genuinely different from each other.

**3. A task that should NOT be delegated.** Ask for something cross-cutting or security-sensitive (*"use the
fleet to refactor authentication across the app"*). Claude should refuse to delegate it and do it itself. If it
delegates anyway, that is a finding I want to hear about.

**4. `/routeai:status`.** Node health, which model and node each category routes to and why, tokens saved so far,
and whether a benchmark is due. Judge whether the routing matches what you know about your machines.

**5. `/routeai:bench`.** The self-grading benchmark: hidden unit tests, mutation testing, script I/O, TOML and
AST graders, a VRAM-fit probe. It takes a few minutes and ends with concrete recommendations (*"drop model X from
category Y: it scored 0%"*). Judge whether the advice is sensible — this is the part I am least sure generalizes
to other hardware.

**6. `/routeai:usage`.** Tokens processed per node and in total, cost in USD for paid providers, remaining free
quota, and Claude tokens saved for this project or across all of them.

**7. The queue.** `/routeai:queue` parks work that the fleet finishes **without Claude** — for when you hit your
usage limit or go to bed. Queue two tasks, close Claude Code entirely, run `python run.py queue work` from the
plugin folder, and watch them complete; the results are on disk when you come back.

## 4. Known limits — please attack these

- **Tiny tasks cost more than they save.** A five-line docstring comes out negative and the report says so out
  loud. Delegation pays off on substantial inputs and outputs, and on batches. Tell me if the numbers ever look
  flattering rather than honest.
- **Small models fail in specific ways**: truncated output (never written to disk — the task fails instead),
  echoing the prompt back (detected and rejected), invented imports. The guard rails are there; try to get past
  them.
- **Tested end to end on Windows 11** with two Ollama machines. The unit tests run on Windows, macOS and Linux in
  CI, but the full flow on macOS and Linux has never been exercised by a human. This is where I most expect
  breakage.
- **The learned statistics start empty**, so the first handful of tasks route on configuration alone, not on
  measurements. The benchmark is what fills that in.
- **Claude Code does not expose usage-limit state to plugins**, so RouteAI cannot detect "tokens finished" by
  itself — the queue is the deliberate answer to that.

## 5. What leaves your machine

- Local Ollama models see your files; that traffic stays on your LAN.
- A remote provider receives project files **only** if you set `send_files = true` on its node. Without it, any
  task carrying files skips that provider.
- API keys live in environment variables: never in the config, never in a log, never in the chat, and requests do
  not follow redirects, so a key cannot be forwarded elsewhere.
- Ollama `:cloud` models, which run on ollama.com, are ignored unless you explicitly allow them.
- Writes are confined to the project directory, with `.git`, `.claude` and secret-looking files protected.

## 6. How to report

Open an issue at <https://github.com/bdbais/routeai/issues> — there is a "Reviewer feedback" template. Whatever
form you use, four things make a report actionable:

1. OS, Python version, and your hardware (GPU and VRAM, or CPU-only).
2. The output of `/routeai:status` (or `python run.py status`).
3. What you asked, what you expected, what happened.
4. The savings line, if the argument is about savings.

And the short verdict I actually care about:

- Did it save you anything real, or is the accounting optimistic?
- Did the routing match your hardware?
- What made you doubt it?
- What would you need before using it on real work?

## 7. Uninstalling

```
/plugin uninstall routeai@bais
```

Then delete `~/.routeai/` (config, stats, task log, benchmark reports, queue). Nothing else is written anywhere
on your system: no service is installed, no port is opened.

---

MIT licensed · site: <https://routeai.bais.info> · author: Bais ([bais.info](https://bais.info))
