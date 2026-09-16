---
name: bench
description: Run the routeai self-learning benchmark on every node and model, then review its recommendations.
disable-model-invocation: true
argument-hint: "[quick|full] [explore] [deep]"
---

Arguments: `$ARGUMENTS` — `quick` (default) or `full`; `explore` also tries installed models that are not
configured; `deep` also measures parallel throughput per node.

1. Call `fleet_bench` with those options. Tell the user it runs in the background and takes several minutes.
2. Poll `fleet_job` every 30-60 seconds and relay short progress updates, until the state is `done`.
3. Present from the report: best model per category, a compact score/speed table, the recommendations,
   the learning status and when to run the next benchmark. Give the report path.
4. Governance: turn the recommendations into a concrete proposed edit of `fleet.toml` (show the diff),
   and apply it **only after the user approves**. Routing already uses the learned scores without edits.
