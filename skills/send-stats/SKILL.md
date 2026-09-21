---
name: send-stats
description: Share this machine's benchmark results on the public RouteAI Community page, or remove them. Use when the user wants to contribute their numbers, asks what would be sent, wants to certify their installation with GitHub, or wants their data deleted. The sending itself always happens in the user's own terminal.
disable-model-invocation: true
argument-hint: "[preview | send | delete | status]"
---

Arguments: `$ARGUMENTS`. The command is **manual and opt-in**: nothing is ever sent automatically, and you never
send on the user's behalf without showing them first.

1. **Show what would leave the machine.** In your Bash tool, run the plugin's CLI with `--dry-run`:

   ```
   <plugin folder>/bin/routeai send-stats --dry-run
   ```

   (`routeai` is on your PATH while the plugin is enabled; `which routeai` gives the folder.) It prints the exact
   JSON: model tag, quantisation, parameter size, context, a hardware bucket (`cpu` or `gpu12`), category, score,
   tokens per second, GPU ratio, number of runs, plus the plugin version and the operating system name. No machine
   names, addresses, file paths, prompts or project data — by construction, not by filtering.

2. **Explain the two lists before they send.** Results are published either as *certified* (the installation is
   signed in with a GitHub account, so abuse can be blocked) or *not certified* (anonymous). Both are public.

3. **Let the user run the send themselves**, in their own terminal, because signing in with GitHub shows a device
   code and the command asks for confirmation:

   ```
   python <plugin folder>/run.py send-stats
   ```

   Useful flags: `--anonymous` (no GitHub sign-in), `--vram gpu-node=12` when a machine's GPU size is unknown,
   `--yes` to skip the confirmation, `--delete` to remove this machine's results from the site, `--status` to see
   how the installation is registered.

4. **If the benchmark report is older than the current suite**, the command refuses: run `/routeai:bench` first,
   otherwise the numbers would not be comparable with anyone else's.

5. Afterwards, point them at <https://routeai.bais.info/community/> and remind them that `--delete` takes their
   results down at any time.

Never ask for a GitHub password or token in the chat, and never paste one into a file: the sign-in happens
between the user's terminal and GitHub, and the plugin only keeps a random install token.
