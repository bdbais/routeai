---
name: status
description: Show the state of the RouteAI fleet — local machines and API providers, models, routing per category, quotas and cost, token savings and benchmark advice.
disable-model-invocation: true
---

Call the `fleet_status` tool and present, compactly:

1. A table of nodes: name, kind (local machine or API provider), healthy, tier, loaded or configured models,
   missing models, and for a provider whether it may receive files, what it used today and whether a quota or
   budget is blocking it.
2. Where each category routes right now (model @ node, and why), and whether that node is free or paid.
3. Totals: delegated tasks, tokens processed by the fleet, estimated Claude tokens avoided.
4. Benchmark advice: due or not, the reasons, and how many model/category pairs are stable vs still learning.

For every problem (node down, model missing, context too small, cloud models ignored) give the one-line fix.
