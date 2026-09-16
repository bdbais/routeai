---
name: delegate
description: Offload small, well-scoped, verifiable coding sub-tasks (unit tests, scripts, docstrings and docs, boilerplate, build/CI files, log triage, bulk per-file edits) to the AIs the user already has - their Ollama machines and any API providers they added - through the RouteAI MCP tools, to save Claude tokens. Use when a plan contains such sub-tasks, when the same edit must be applied to many files, or when the user asks to use the fleet, RouteAI or their local AI.
---

# Delegating to the RouteAI fleet

You are the lead engineer. The fleet is a team of junior workers running on the user's hardware:
free to use, parallel, but weaker than you and blind to everything you do not hand them.
**Governance stays with you**: you decide what to delegate, you write the brief, you verify the result,
and you own what lands in the repository.

## Decide: delegate or keep?

Delegate when all three hold:
1. The brief fits in a few sentences with a clear interface (signature, file, format).
2. The result is cheap to verify: run the tests, a linter, a parser, or a quick read of a small diff.
3. The input is a few files the server can read itself (worker context is ~8k tokens).

Tiny jobs do not pay: writing the brief and reading the result can cost more than a 20-line change done
yourself. Savings come from substantial inputs or outputs (a module to test, a long doc, a big config) and from
batches, where one brief covers many files. If `tokens_saved` comes back negative, stop delegating that kind of task.

| Good fits | Keep for yourself |
|---|---|
| Unit tests for a given module | Architecture and design decisions |
| CLI/automation scripts, small utilities | Cross-cutting refactors, repo-wide changes |
| Docstrings, READMEs, changelogs, comments | Security-sensitive code (auth, crypto, input validation at trust boundaries) |
| Boilerplate: DTOs, config files, CI workflows, Dockerfiles | Debugging subtle issues that need the whole picture |
| Build/CI log triage (structured JSON answer) | Anything you could not verify afterwards |
| The same mechanical edit on many files (batch) | Tasks needing secrets — never pass `.env` or credentials |

## Route: pick the category

- `complex` — tricky algorithmic code → fast GPU node first.
- `code` — ordinary functions/classes → GPU node first.
- `tests`, `scripts`, `build`, `docs` → light machines first, overflow to the GPU node when busy.
- `general` — extraction, classification, summaries → any node.

## Local machines and API providers

A node is either a machine running Ollama or an API provider the user added (Gemini, Groq, OpenRouter, …).
`fleet_status` says which is which (`type`, `free`, `sends_files_offsite`, `today`, `quota_blocked`).

- **Files leave the machine only where allowed.** A provider without `send_files` is skipped for any task that
  includes files. If that is why a task had nowhere to go, say so and offer `/routeai:add-ai` to change it, or
  run the task on a local node.
- **Free before paid.** Routing already prefers free capacity; when a paid provider did the work, say what it
  cost (`fleet_usage` reports cost per node) and warn when a free tier is close to its daily limit.
- **Sensitive work stays home.** For anything under NDA or with credentials in the files, force a local node
  (`node: "<name>"`) or do it yourself.
- **Never handle keys.** API keys belong in environment variables; the plugin stores only the variable name. If
  the user pastes a key in the chat, tell them it is in the transcript now and should be rotated.

## Brief: write it like a ticket for a junior

- Goal and exact interface (function signature, file name, CLI usage, output format).
- Constraints: language version, allowed libraries, framework and conventions already used in the repo.
- Acceptance criteria: what must be true for you to accept it (e.g. "tests must import from `app.parser`").
- Put inputs in `files` (paths or globs — the server reads them; never paste file contents) and shared
  interfaces/conventions in `context` or `shared_files`.
- Use `output_path` so the result goes straight to disk and only a preview comes back.
- For structured answers use `json_schema`.

## Run

1. Call `fleet_status` once per session: which nodes are up, where each category routes, whether a benchmark is due.
2. One task: `fleet_delegate`. It returns within `wait_seconds`, or a `job_id` to poll with `fleet_job`.
3. Many files: `fleet_delegate_batch` with `output_pattern` (e.g. `tests/test_{stem}.py`), then poll `fleet_job`
   every 20-60 seconds while you do other work. Tasks spread across all machines in parallel.

## Verify, then give feedback (always)

- Run what can be run (tests, build, linter). Read the diff; do not trust previews alone.
- Small defects: fix them yourself. Bad result: re-delegate once with a sharper brief, or do it yourself.
- Then call `fleet_feedback(task_id, verdict)` — `good` (used as is), `fixed` (needed corrections) or
  `rejected` (unusable). This is how routing learns which machine and model to trust for each category.

## Report the savings (always)

Every finished task or batch returns `tokens_saved` (from `fleet_delegate`, or `fleet_job` for batches and long
tasks). As soon as it finishes, tell the user in one line, in their language, how many Claude tokens it saved and
the session total, e.g. *"Fleet: ~2.4k Claude tokens saved on this task (session: 9.1k, all-time: 31k)."*
It is an estimate: tokens of the files the worker read plus the tokens it generated, minus the brief you wrote
and what came back to you. Report negative values as they are.

## When Claude is running out of tokens

If the user is close to their Claude usage limit, or wants the machines to keep working while they are away,
queue the delegable work with `fleet_queue` (see `/routeai:queue`): the fleet finishes it without you and
you review the results in the next session.

## Housekeeping

- If `fleet_status` reports `bench.due`, tell the user and suggest `/routeai:bench`; do not start long
  benchmarks on your own.
- If a node is down or a model is missing, report it with the one-line fix instead of silently doing the work yourself.
- When you summarize your work, repeat the session total of Claude tokens saved by the fleet.
