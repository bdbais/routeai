---
name: queue
description: Queue work for the fleet (local machines and API providers) to finish on its own while Claude is paused - use when the user is close to their Claude usage limit, wants the machines to keep working while away or overnight, or asks to park work for later; also to list, review or clear queued work.
argument-hint: "[list | clear done|pending|all]"
---

The fleet can keep working when you cannot: queued tasks are executed by this session's server process and by any
`python run.py queue work` worker, one at a time, and the results wait on disk. Slow but steady.

1. **Queue the work.** For each task call `fleet_queue` with `action: "add"` and the same care as a direct
   delegation: a self-contained brief, the file paths, and an `output_path` so the result lands on disk (a queued
   task nobody reads is wasted work). Add a short `note` saying why it was queued. Only queue what can be verified
   later; never queue a change you would not be able to check.
2. **Tell the user what keeps it alive.** Work continues while this Claude Code session's process is running.
   To survive closing it, they can start a worker in a terminal: `python run.py queue work` (it takes tasks from
   any project). Results land in `~/.routeai/queue/done`.
3. **Review afterwards.** `fleet_queue` with `action: "list"` shows queued, running and finished tasks with their
   results. For each finished one: read what was written, run the tests, fix or redo what is wrong, then call
   `fleet_feedback` with the result's `task_id`. Clear the reviewed ones with `action: "clear"`.
4. **Usage limits.** Claude Code does not tell the plugin when your tokens run out, so ask the user (or act when
   they say they are close): queue the delegable work first - tests, documentation, scripts, boilerplate, bulk
   edits - and keep for later only what needs Claude's judgement.
