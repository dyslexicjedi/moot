# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Is This

Moot is a Discord bot that orchestrates multi-agent AI discussions. When a user posts a topic, URL, or image, a council of AI agents (named after characters from Dennis E. Taylor's *Bobiverse* series) debate the topic and post their discussion back to Discord. All discussions are archived in a local ChromaDB vector database for semantic search and retrieval.

## Setup and Running

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in Discord and LLM API keys
python3 setup_webhooks.py     # one-time: creates Discord webhooks per agent
python3 discord_bot.py        # start the bot
```

Local llama.cpp servers (optional): `./start_servers.sh` before starting the bot.

## Testing

**After every code change**, run the unit tests:

```bash
pytest
```

Tests live in `tests/test_council.py`. They mock all LLM calls — no API keys required, runs in seconds.

**Before every commit**, also run the smoke test:

```bash
python smoke_test.py
```

The smoke test makes real API calls (requires keys in `.env`, costs credits) and verifies all agents spoke, no API errors occurred, and the chairman called BLAAATTT.

If test deps aren't installed: `pip install -r requirements.txt`.

## Architecture

The app has four core modules:

**`config.py`** — All agent personalities, API endpoints, and Discord settings live here. `AgentConfig` is a dataclass holding name, system prompt, model, base_url, api_key, color, and vision capability. `CHAIRMAN_CONFIG` is Bob (Claude Sonnet 4.6 via OpenRouter). `GUPPY_CONFIG` is the intel-briefer. `AGENT_CONFIGS` is the list of four debating agents (Riker, Bill, Milo, Homer). Discord and discussion tuning constants (MAX_ROUNDS, MAX_TOKENS, INTER_MESSAGE_DELAY) are also here.

**`council.py`** — The discussion engine. `run_discussion()` is an async generator that yields `(agent_name, message_text)` tuples. It calls `_chairman_open()`, then round-robins through `_agent_respond()` for each agent, then `_chairman_evaluate()` to decide CONTINUE/CONCLUDE. Bob signals the end with "BLAAATTT". All LLM calls use `AsyncOpenAI` with the per-agent `base_url` and `api_key`, so any OpenAI-compatible provider works. Temperature is intentionally set low for the chairman (0.3–0.4) and higher for agents (0.75).

**`discord_bot.py`** — The Discord event loop. Handles `!moot` / `!discuss` commands, URL extraction, image downloading and base64-encoding for vision models, and knowledge base commands (`!lookup`, `!index`, `!memory`, `!stats`). After each discussion, it calls `vector_db.archive_moot()`. It posts each agent message via that agent's Discord webhook (falls back to a bot embed). `_active_discussion` is a bool flag that prevents concurrent moots.

**`vector_store.py`** — ChromaDB wrapper. Three collections: `moot_archive` (completed discussions), `external_docs` (indexed URLs/text), `personal_notes` (user `!memory` entries). Embedding model is `sentence-transformers/all-MiniLM-L6-v2`. `lookup()` does semantic search; `summarize_findings()` asks Bob to synthesize results.

### Data Flow

```
Discord message → discord_bot.py
  → (optional) fetch URL text / download+encode image
  → council.py run_discussion() [async generator]
      → _chairman_open() → LLM API
      → per-round: each agent _agent_respond() → LLM API
      → _chairman_evaluate() → CONTINUE / CONCLUDE
  → send_agent_message() via webhook
  → vector_store.archive_moot() → ChromaDB
```

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot auth |
| `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `DISCORD_USER_ID` | Target channel |
| `WEBHOOK_BOB`, `WEBHOOK_RIKER`, etc. | Per-agent Discord webhooks (set by setup_webhooks.py) |
| `OPENROUTER_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY` | Cloud LLM providers |
| `CHROMA_PERSIST_DIR` | Path for vector DB (default: `./chroma_db`) |

## Adding or Modifying Agents

To add an agent, create an `AgentConfig` in `config.py` and append it to `AGENT_CONFIGS`. The config drives everything — council.py and discord_bot.py iterate over the list dynamically. Run `setup_webhooks.py` again to create the new agent's webhook.

To swap a model, change `model` and `base_url` in the relevant `AgentConfig`. Any OpenAI-compatible endpoint works. Set `supports_vision=True` only for models that accept image content blocks.
