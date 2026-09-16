---
name: nodes
description: Add, remove, enable or disable a node of the RouteAI fleet - a local Ollama machine or an API provider. Use when the user gets a new PC or GPU, rents a server, takes a machine offline for a while, stops using a provider, or asks which AIs the fleet is using. To add a provider with an API key, prefer /routeai:add-ai.
argument-hint: "add name=url | remove name | disable name | enable name | list"
---

Arguments: `$ARGUMENTS`. Examples: `add gpu2=http://192.168.1.20:11434`, `remove laptop`, `disable gpu`, `list`.

1. Work out the action and the machine from the arguments; ask only for what is missing (a short name, and for
   `add` the URL, usually `http://<host>:11434`).
2. Call `fleet_nodes`. Only that machine's block in `fleet.toml` changes: the other machines keep their settings
   and comments, and the previous file is kept as a backup.
3. Report the result:
   - **add**: reachable or not (if not: on that machine Ollama must run with `OLLAMA_HOST=0.0.0.0` and port 11434
     must be open on the LAN), the models chosen per category, and the recommended models that are missing. Ask
     before pulling any of them (they are several GB), then call `fleet_pull` and poll `fleet_job`. For a machine
     outside the LAN, remind the user to keep it behind a VPN or an authenticating proxy and to use `api_key_env`.
     Offer `/routeai:bench` so the new machine gets measured; until then it runs on prior estimates.
   - **remove / disable**: the learned statistics are kept, so adding it back later starts from what it already
     knows. Prefer `disable` for a machine that is only temporarily off; a machine that is simply unreachable is
     skipped automatically anyway.
4. Finish with where each category routes now (from the `nodes` list in the answer, or `fleet_status`).
