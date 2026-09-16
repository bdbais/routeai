---
name: add-ai
description: Add another AI to the fleet - a remote provider with an API key (Gemini, Groq, OpenRouter, DeepSeek, Mistral, OpenAI, or any OpenAI-compatible endpoint) or another local Ollama machine. Use when the user wants to plug in a new AI, use a free daily quota, or spread work across more models.
argument-hint: "provider name  (e.g. gemini gem1)"
---

Arguments: `$ARGUMENTS`, usually `<provider> <name>`: `gemini gem1`, `groq fast`, `openrouter or1`,
`deepseek ds`, `mistral mi`, `openai gpt`, `custom mybox`. A plain machine URL means a local Ollama node instead.

1. **Never take the key in chat.** The key must live in an environment variable; `fleet_nodes` only stores its
   *name*. If the user has not set one, tell them how (`setx GEMINI_API_KEY ...` on Windows, `export ...` in the
   shell profile on macOS/Linux, then a new session) and stop until it exists. If they paste a key in the chat,
   say it is now in the transcript and should be rotated.
2. **Ask before their code leaves the machine.** `send_files` decides whether project files may be sent to that
   provider. Default no. Ask explicitly, and suggest starting with `docs` and `general` only.
3. **Free tier or paid?** Set `cost_input` / `cost_output` (USD per million tokens, 0 for a free tier) and a
   limit: `daily_requests` or `daily_tokens` for free tiers, `daily_cost_usd` for paid ones. The router prefers
   free nodes and stops using a node when its limit is reached, falling back to the local machines.
4. **Call `fleet_nodes`** with `action: "add"`, the provider preset, the name, `api_key_env`, `send_files`, the
   limits and the models. The answer lists the provider's available models: pick a few and set `models` per
   category (a fast cheap one for `docs`/`general`, a strong one for `code`/`complex`).
5. **Check and measure.** Report whether the provider answered, then offer a quick real task to try it, and
   `/routeai:bench --include-remote` when the user accepts that measuring a provider spends quota or money.
6. Afterwards, `/routeai:usage` shows tokens, cost and remaining quota per node.
