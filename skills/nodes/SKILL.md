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
     before pulling any of them (they are several GB), then call `fleet_pull` and poll `fleet_job`.
     Offer `/routeai:bench` so the new machine gets measured; until then it runs on prior estimates.
   - **remove / disable**: the learned statistics are kept, so adding it back later starts from what it already
     knows. Prefer `disable` for a machine that is only temporarily off; a machine that is simply unreachable is
     skipped automatically anyway.
4. Finish with where each category routes now (from the `nodes` list in the answer, or `fleet_status`).

## Machines outside the LAN: SSH, not an open port

Ollama has no authentication, so a remote or rented Linux machine should not expose port 11434. RouteAI reaches
it through an SSH tunnel instead, with Ollama listening only on `127.0.0.1` over there.

- **The user already reaches it with a key** (an alias in `~/.ssh/config`, or their agent): add it with
  `fleet_nodes` and `url: "ssh://<alias>"` (or `ssh://user@host:port`). Nothing to install.
- **It needs a password the first time**: do not ask for the password, and never let it be typed in the chat. Tell
  the user to run, in their own terminal (find the plugin folder with `which routeai` in your Bash tool):

  ```
  python <plugin folder>/run.py ssh-setup <name> <user@host>
  ```

  ssh asks them to confirm the server fingerprint and for the password; RouteAI creates a dedicated key, installs
  it restricted so it can only open the tunnel to Ollama (no shell), checks the tunnel and adds the node. When
  they are done, call `fleet_status`.
- A node that stops working: suggest `run.py ssh-check <name>`, which tests DNS, the SSH port, authentication and
  Ollama one by one. After a legitimate reinstall of the server, `run.py ssh-forget <name>` drops the old host key.
