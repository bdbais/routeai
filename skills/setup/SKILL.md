---
name: setup
description: Configure RouteAI — find the user's Ollama machines, write ~/.routeai/fleet.toml, recommend models to install and start a first benchmark (use /routeai:add-ai for API providers). Use on first use, when fleet_status reports setup_needed, or when the user adds, removes or changes a machine.
argument-hint: "[name=url ...]"
---

Arguments: `$ARGUMENTS` — optional machines as `name=url` pairs (e.g. `gpu=http://192.168.1.13:11434`).

1. **Machines.** If no arguments were given, ask the user which computers run Ollama (a short name and the URL;
   the local one is `local=http://localhost:11434`). Remind them that on other computers of the LAN Ollama must
   listen on the network (`OLLAMA_HOST=0.0.0.0`, port 11434 open). For a machine outside the LAN - a remote or
   rented Linux server - use SSH instead (Ollama has no authentication): `name=ssh://alias` if they already log in
   with a key, otherwise have them run `run.py ssh-setup <name> <user@host>` in their own terminal (see the `nodes`
   skill). Never ask for a password in the chat.
2. **Probe and write.** Call `fleet_setup` with the nodes. If a configuration already exists, show the user and
   only pass `overwrite: true` after they agree (the old file is kept as a backup).
3. **Report**, per machine: reachable or not (with the fix), installed chat models, the model chosen for each
   category, and `suggested_pulls` (model, size, hardware it needs, what it measured).
4. **Models to install.** Downloads are several GB on the user's machines: ask which suggested models to pull and
   on which machine, then call `fleet_pull` for each approved one and poll `fleet_job` until done. Never pull
   without explicit approval.
5. **Benchmark.** Offer `/routeai:bench` (use `explore` after new pulls) so the fleet measures what each
   machine is good and fast at. Routing learns from it immediately.
