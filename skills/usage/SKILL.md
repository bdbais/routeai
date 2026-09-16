---
name: usage
description: Report how much the fleet was used and what it saved - tokens processed by each node (local machines and API providers) with a fleet total, cost in USD, remaining daily quotas, and Claude tokens saved, per project or across all of them. Use when the user asks how many tokens Ollama or a provider has done, what it cost, which node did the work, or whether the fleet is worth it.
argument-hint: "[all] [days N]"
---

Arguments: `$ARGUMENTS` - `all` for every project instead of this one, `days N` to limit the period.

1. Call `fleet_usage` (`scope: "all"` and/or `days` when asked). It reads the delegation log, so it covers every
   session, not only this one.
2. Present, as a short table and a total:
   - **Tokens per node**: local machines and remote providers alike - tokens read (prompts) and written
     (answers), the total, how many tasks, how much model time, which models did the work, and for a paid
     provider the cost in USD.
   - **Fleet total** of tokens and of cost, plus today's quota use per node (`today_per_node`): say when a
     free tier is close to its daily limit.
   - **Claude tokens saved**, split into input (files the workers read) and output (answers they wrote), with the
     number of tasks, the failures, and how many tasks cost more than they saved.
   - The period covered, and the session figure when the user is comparing.
3. Be honest about the numbers. If savings are small or negative, say which kind of task does not pay off (tiny
   files, one-line changes) and what does: tests and documentation for real modules, and batches over many files.
   If one machine does all the work, say so - it may be worth adding or enabling another.
4. Useful follow-ups when they fit: `/routeai:bench` when the benchmark is due, `/routeai:status` for
   the current routing, `/routeai:nodes` to add or disable a machine.
