# Moot

A multi-agent Discord bot where a council of AI replicants — named after characters from Dennis E. Taylor's *Bobiverse* series — discuss topics, debate ideas, and reach a consensus. Post a topic or drop a meme in a private Discord channel and watch Bob, Riker, Bill, Milo, and Homer hash it out. Bob chairs the moot and calls it closed with a **BLAAATTT**. Drop a link and Guppy runs the intelligence briefing.

## How it works

1. You post anything in the designated Discord channel — a question, a topic, an image, or a link
2. If you drop a link, Guppy runs the intelligence briefing. Otherwise, Bob opens the moot and introduces the topic
3. Each replicant takes a turn sharing their perspective (up to 3 rounds)
4. Bob evaluates after each round and decides whether to continue or conclude
5. Bob closes with **BLAAATTT** and a summary, then @ mentions you

Each replicant has a distinct personality:

| Replicant | Role | Style |
|-----------|------|-------|
| **Bob** | Chair | Pragmatic engineer, sarcastic, keeps the moot moving |
| **Guppy** | Intel | Admiral, terse, dry — runs the article briefing |
| **Riker** | Agent | Decisive, action-oriented, pushes for conclusions |
| **Bill** | Agent | Systems thinker, asks "what breaks first?" |
| **Milo** | Agent | Philosophical, surfaces what others gloss over |
| **Homer** | Agent | Explorer, connects distant ideas, thinks big |

## Requirements

- Python 3.11+
- A Discord bot with **Send Messages**, **Embed Links**, **Manage Webhooks**, and **Read Message History** permissions
- At least one of: local models via [llama.cpp](https://github.com/ggerganov/llama.cpp) or a cloud API key (OpenRouter, Together AI, Groq, OpenAI)

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env` with your Discord bot token, server/channel/user IDs, and any API keys.

### 3. Create webhooks

Gives each replicant their own Discord identity (name and color). Run once:

```bash
python3 setup_webhooks.py
```

### 4. Configure models

Open `config.py` and set `base_url`, `model`, and `api_key` for each replicant.

**Cloud (OpenRouter example):**
```python
AgentConfig(
    name="Riker",
    base_url=OPENROUTER_BASE,
    model="meta-llama/llama-3.3-70b-instruct",
    api_key=OPENROUTER_API_KEY,
    ...
)
```

**Local (llama.cpp example):**
```python
AgentConfig(
    name="Riker",
    base_url="http://localhost:8002/v1",
    model="local-model",
    api_key="not-needed",
    ...
)
```

Mix and match freely — each replicant is independent.

**Local model servers** (Framework AI Max 395+ suggested layout, 128 GB unified memory):

| Replicant | Port | Suggested size |
|-----------|------|----------------|
| Bob | 8001 | ~70B (~40 GB) |
| Riker | 8002 | ~32B (~20 GB) |
| Bill | 8003 | ~22B (~12 GB) |
| Milo | 8004 | ~14B (~8 GB) |
| Homer | 8005 | ~14B (~8 GB) |

Edit model paths in `start_servers.sh`, then:

```bash
./start_servers.sh
```

### 5. Run the bot

```bash
python3 discord_bot.py
```

You'll see *"We are Legion. We are Bob."* in your Discord channel when it's ready.

## Commands

| Command | Description |
|---------|-------------|
| `!moot <topic>` | Explicitly trigger a moot (alias: `!discuss`) |
| `!stop` | Cancel a moot in progress |
| `!status` | Guppy reports whether a moot is running |
| `!replicants` | List active replicants and their endpoints (alias: `!agents`) |
| `!lookup <query>` | Search past moots and indexed docs, Bob summarizes findings |
| `!index <url\|text>` | Index an article URL or raw text for future lookups |
| `!memory <fact>` | Save a personal note or fact to remember |
| `!stats` | Show knowledge base statistics |

Or just post anything in the channel — any message automatically starts a moot.

## Project structure

```
council-ai/
├── config.py           # Agent personalities, model endpoints, Discord settings
├── council.py          # Discussion orchestration (the moot engine)
├── discord_bot.py      # Discord bot and command handling
├── vector_store.py     # ChromaDB knowledge base with semantic search
├── setup_webhooks.py   # One-time webhook creation
├── start_servers.sh    # Launch local llama.cpp server instances
├── chroma_db/          # Persistent vector database (auto-created)
├── requirements.txt
└── .env.example
```

## Supported cloud providers

Any OpenAI-compatible API works. Constants for common providers are in `config.py`:

- **OpenRouter** — access to nearly every model
- **Together AI** — fast open-weight inference
- **Groq** — very fast, generous free tier
- **OpenAI** — GPT-4o, o1, etc.

## Vector Database (Knowledge Base)

All moot discussions are automatically archived in a local ChromaDB vector database. You can also index external articles and save personal notes.

### How it works

- **Automatic archiving**: Every completed moot is stored with full text and per-speaker chunks
- **Semantic search**: Uses `sentence-transformers/all-MiniLM-L6-v2` for embeddings (384 dimensions)
- **Local storage**: Everything stored in `chroma_db/` directory — no external services
- **Bob summarizes**: Search results are summarized by Bob for easy consumption

### What gets stored

| Collection | Content |
|------------|---------|
| `moot_archive` | All completed moots (full discussion + individual speaker chunks) |
| `external_docs` | Indexed articles from URLs or manual text entries |
| `personal_notes` | Notes saved via `!memory` command |

### Usage examples

```
!lookup quantum computing applications
# Bob searches archives and summarizes relevant discussions

!index https://arxiv.org/abs/quantum-paper
# Fetches and indexes the article

!memory Always backup before deploying on Friday
# Saves a personal reminder

!stats
# Shows document counts for each collection
```

### Upgrade instructions

If you're upgrading from an older version:

```bash
# Stop the bot
# Update dependencies
source .venv/bin/activate
pip install -r requirements.txt

# Add CHROMA_PERSIST_DIR to .env (optional, defaults to ./chroma_db)
cp .env.example .env

# Start the bot — vector store initializes automatically
python3 discord_bot.py
```

The embedding model (`all-MiniLM-L6-v2`) downloads automatically on first use (~90MB).
