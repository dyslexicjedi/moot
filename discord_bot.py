"""
Discord bot — listens for messages in the designated channel and runs the
council discussion, posting each agent's reply via its own webhook so it
appears as a distinct user in Discord.
"""

import asyncio
import base64
import logging
import os
import re
import sys
from html.parser import HTMLParser
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import (
    AGENT_CONFIGS,
    CHAIRMAN_CONFIG,
    DISCORD_CHANNEL_ID,
    DISCORD_TOKEN,
    GUPPY_CONFIG,
    INTER_MESSAGE_DELAY,
    WEBHOOK_URLS,
    AgentConfig,
)
from council import DISCUSSION_DONE, check_agent_health, guppy_brief_health, guppy_brief_topic, guppy_debrief, run_discussion, summarize_article, export_discussion_text, split_by_speaker

from vector_store import VectorStore

load_dotenv()

# Matches bare URLs and Discord's <url> embed-suppression format.
# Group 1 is always the clean URL without surrounding <>.
_URL_RE = re.compile(r'<?(https?://[^\s>]+)>?')


class _TextExtractor(HTMLParser):
    _SKIP = frozenset(('script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript'))
    _BLOCK = frozenset(('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'blockquote'))

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self._BLOCK:
            self._parts.append('\n')

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = ''.join(self._parts)
        return re.sub(r'[ \t]+', ' ', re.sub(r'\n{3,}', '\n\n', text)).strip()


def _extract_url(text: str) -> tuple[Optional[str], str]:
    """Return (url, remaining_topic) from a message that may contain a URL."""
    m = _URL_RE.search(text)
    if not m:
        return None, text
    url = m.group(1)
    remaining = (text[:m.start()] + text[m.end():]).strip()
    return url, remaining or "Discuss this article."


async def _fetch_article_text(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        html = await resp.text()
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("council-bot")


# ─── Image handling ──────────────────────────────────────────────────────────

async def _download_and_encode_image(
    session: aiohttp.ClientSession, url: str, filename: str,
) -> Optional[dict]:
    """Download an image from a URL and return a dict ready for API use."""
    try:
        async with session.get(url) as resp:
            raw = await resp.read()
    except Exception as exc:
        log.warning("Failed to download image %s: %s", filename, exc)
        return None

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp",
    }
    ext = filename.lower().rsplit(".", 1)[-1]
    mime_type = mime_map.get(ext, "image/png")

    encoded = base64.b64encode(raw).decode("ascii")
    return {"mime_type": mime_type, "data": encoded}

# ─── Discord setup ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Track active discussions so we don't run two at once in the same channel
_active_discussion: bool = False

# Last completed moot — used by !debrief
_last_moot: Optional[dict] = None

# Vector database for knowledge base
vector_db: Optional[VectorStore] = None


# ─── Webhook sending ──────────────────────────────────────────────────────────

async def _send_via_webhook(
    session: aiohttp.ClientSession,
    webhook_url: str,
    agent: AgentConfig,
    text: str,
) -> None:
    """Post a message through an agent's personal webhook."""
    embed = {
        "description": text,
        "color": agent.color,
    }
    payload = {
        "username": agent.name,
        "embeds": [embed],
    }
    if agent.avatar_url:
        payload["avatar_url"] = agent.avatar_url

    async with session.post(
        webhook_url,
        json=payload,
        params={"wait": "true"},
    ) as resp:
        if resp.status not in (200, 204):
            body = await resp.text()
            log.warning("Webhook %s returned %d: %s", agent.name, resp.status, body)


async def _send_via_bot(channel: discord.TextChannel, agent: AgentConfig, text: str) -> None:
    """Fallback: post as the bot itself with a color-coded embed."""
    embed = discord.Embed(description=text, color=agent.color)
    embed.set_author(name=agent.name)
    await channel.send(embed=embed)


async def send_agent_message(
    session: aiohttp.ClientSession,
    channel: discord.TextChannel,
    agent: AgentConfig,
    text: str,
) -> None:
    webhook_url = WEBHOOK_URLS.get(agent.name, "")
    try:
        if webhook_url:
            await _send_via_webhook(session, webhook_url, agent, text)
        else:
            await _send_via_bot(channel, agent, text)
    except Exception as exc:
        log.error("Error sending message for %s: %s", agent.name, exc)
        # Best-effort fallback
        try:
            await _send_via_bot(channel, agent, text)
        except Exception:
            pass


# ─── Discussion runner ────────────────────────────────────────────────────────

async def run_council_discussion(
    channel: discord.TextChannel,
    topic: str,
    trigger_user_id: int,
    context_note: str = "",
    url: Optional[str] = None,
    image_data: Optional[list[dict]] = None,
) -> None:
    global _active_discussion, _last_moot
    _active_discussion = True
    discussion_history = []

    try:
        async with aiohttp.ClientSession() as session:
            if url:
                await send_agent_message(
                    session, channel, CHAIRMAN_CONFIG,
                    "*Guppy, pull up that article and give me a brief.*"
                )
                try:
                    raw_text = await _fetch_article_text(session, url)
                    summary = await summarize_article(raw_text)
                    await send_agent_message(session, channel, GUPPY_CONFIG, summary)
                    await asyncio.sleep(INTER_MESSAGE_DELAY)
                    context_note = f"Bob tasked Guppy to review the article. Guppy's brief:\n\n{summary}"
                except Exception as exc:
                    log.error("Article fetch/summarize failed: %s", exc)
                    await channel.send(f"⚠️ Couldn't read the article: `{exc}` — proceeding without it.")

            async for agent, text in run_discussion(topic, context_note, image_data):
                if text == DISCUSSION_DONE:
                    break

                # Track history for archiving
                if agent:
                    discussion_history.append({"speaker": agent.name, "text": text})

                await send_agent_message(session, channel, agent, text)
                await asyncio.sleep(INTER_MESSAGE_DELAY)

        # Save last moot for !debrief
        if discussion_history:
            _last_moot = {"topic": topic, "history": discussion_history}

        # Auto-archive the moot
        if vector_db and discussion_history:
            try:
                from datetime import datetime
                moot_id = f"moot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                participants = list(set(entry["speaker"] for entry in discussion_history))
                
                await vector_db.archive_moot(
                    moot_id=moot_id,
                    topic=topic,
                    discussion_text="",
                    participants=participants,
                    history=discussion_history
                )
                log.info("Archived moot %s", moot_id)
            except Exception as e:
                log.error("Failed to archive moot: %s", e)

        # Ping the user when done
        await channel.send(
            f"<@{trigger_user_id}> The moot has concluded. See the discussion above."
        )
    except Exception as exc:
        log.error("Moot failed: %s", exc, exc_info=True)
        await channel.send(
            f"<@{trigger_user_id}> Ack! The moot hit a snag: `{exc}`"
        )
    finally:
        _active_discussion = False


# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    global vector_db
    log.info("We are Legion. We are Bob.")
    log.info("Logged in as %s (id=%d)", bot.user, bot.user.id)
    log.info("Watching channel id=%d", DISCORD_CHANNEL_ID)
    webhooks_configured = sum(1 for v in WEBHOOK_URLS.values() if v)
    log.info("Webhooks configured: %d / %d", webhooks_configured, len(WEBHOOK_URLS))
    
    persist_dir = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
    try:
        vector_db = VectorStore(persist_dir)
        stats = vector_db.get_stats()
        total_docs = sum(s["count"] for s in stats.values())
        log.info("Vector store initialized with %d total documents", total_docs)
    except Exception as e:
        log.error("Failed to initialize vector store: %s", e)
        vector_db = None
    
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send("*We are Legion. We are Bob.* — Replicants online and standing by.")


@bot.event
async def on_message(message: discord.Message) -> None:
    global _active_discussion

    # Ignore bots and messages outside the designated channel
    if message.author.bot:
        return
    if message.channel.id != DISCORD_CHANNEL_ID:
        return

    # Let commands (! prefix) through
    await bot.process_commands(message)

    # Ignore command messages themselves
    if message.content.startswith("!"):
        return

    # Only trigger a moot when the message contains !moot
    if "!moot" not in message.content:
        return

    if _active_discussion:
        await message.reply(
            "A moot is already in progress. "
            "Use `!stop` to cancel it, or wait for the BLAAATTT.",
            delete_after=10,
        )
        return

    # Build topic from message content + any attachments
    raw = message.content.replace("!moot", "").strip()
    context_note = ""
    url, topic = _extract_url(raw)
    image_data = None

    if message.attachments:
        image_files = [a for a in message.attachments if _is_image(a.filename)]
        if image_files:
            context_note = "The user also shared image(s)."
            if not topic:
                topic = "Discuss the attached image(s)."

    if not topic and not url:
        return  # Empty message with no content — ignore

    await message.add_reaction("⏳")

    async def _start_discussion():
        nonlocal image_data
        if image_files:
            async with aiohttp.ClientSession() as img_session:
                encoded = []
                for img_file in image_files:
                    result = await _download_and_encode_image(
                        img_session, img_file.url, img_file.filename,
                    )
                    if result:
                        encoded.append(result)
                image_data = encoded or None
        asyncio.create_task(
            run_council_discussion(
                message.channel, topic, message.author.id,
                context_note, url=url, image_data=image_data,
            )
        )

    asyncio.create_task(_start_discussion())


def _is_image(filename: str) -> bool:
    return filename.lower().rsplit(".", 1)[-1] in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="stop")
async def stop_discussion(ctx: commands.Context) -> None:
    """Cancel any in-progress moot."""
    global _active_discussion
    if _active_discussion:
        _active_discussion = False
        await ctx.send("Moot cancelled. BLAAATTT.")
    else:
        await ctx.send("Ack! No moot is currently running.")


@bot.command(name="discuss", aliases=["moot"])
async def force_discuss(ctx: commands.Context, *, topic: Optional[str] = None) -> None:
    """Manually trigger a moot: !moot <topic>  (alias: !discuss)"""
    global _active_discussion
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("This command only works in the moot channel.")
        return
    if _active_discussion:
        await ctx.send("A moot is already running. Use `!stop` first.")
        return

    raw = topic or ""
    url, topic = _extract_url(raw)
    if not topic:
        topic = "Discuss the attached image."

    await ctx.send("⏳")

    async def _start_discussion():
        image_data = None
        image_files = [a for a in ctx.message.attachments if _is_image(a.filename)]
        if image_files:
            async with aiohttp.ClientSession() as img_session:
                encoded = []
                for img_file in image_files:
                    result = await _download_and_encode_image(
                        img_session, img_file.url, img_file.filename,
                    )
                    if result:
                        encoded.append(result)
                image_data = encoded or None
        asyncio.create_task(
            run_council_discussion(
                ctx.channel, topic, ctx.author.id, url=url, image_data=image_data,
            )
        )

    asyncio.create_task(_start_discussion())


@bot.command(name="status")
async def status(ctx: commands.Context) -> None:
    """Guppy reports current moot status."""
    if _active_discussion:
        await ctx.send("*Guppy:* Moot in progress, Admiral. Standing by.")
    else:
        await ctx.send("*Guppy:* All systems nominal. No moot in progress.")


@bot.command(name="replicants", aliases=["agents"])
async def list_agents(ctx: commands.Context) -> None:
    """List all active replicants. (alias: !agents)"""
    lines = [f"**{CHAIRMAN_CONFIG.name}** (moot chair) → `{CHAIRMAN_CONFIG.base_url}`"]
    for cfg in AGENT_CONFIGS:
        hook = "✓ webhook" if WEBHOOK_URLS.get(cfg.name) else "✗ no webhook"
        lines.append(f"**{cfg.name}** → `{cfg.base_url}` ({hook})")
    await ctx.send("**Active replicants:**\n" + "\n".join(lines))


@bot.command(name="health")
async def health(ctx: commands.Context) -> None:
    """Check API connectivity for all agents: !health"""
    await ctx.send("*Running health checks...*")

    all_configs = [CHAIRMAN_CONFIG, GUPPY_CONFIG] + AGENT_CONFIGS
    results = await asyncio.gather(*[check_agent_health(cfg) for cfg in all_configs])

    lines = ["**Health Check:**"]
    for cfg, result in zip(all_configs, results):
        status = result["status"]
        latency_ms = result["latency_ms"]
        detail = result["detail"]

        if cfg == CHAIRMAN_CONFIG:
            role = " (chairman)"
        elif cfg == GUPPY_CONFIG:
            role = " (intel)"
        else:
            role = ""

        if status == "ok":
            icon = "✅"
            suffix = f"({latency_ms}ms)"
        elif status == "warn":
            icon = "⚠️"
            suffix = f"({latency_ms}ms) — slow response"
        else:
            icon = "❌"
            suffix = f"— {detail}"

        lines.append(f"• **{cfg.name}**{role} → {icon} {suffix} — `{cfg.model}`")

    await ctx.send("\n".join(lines))


@bot.command(name="guppy")
async def guppy_diagnostics(ctx: commands.Context) -> None:
    """Get Guppy's system diagnostic briefing: !guppy"""
    await ctx.send("*Guppy, run diagnostics.*")

    all_configs = [CHAIRMAN_CONFIG, GUPPY_CONFIG] + AGENT_CONFIGS
    results = await asyncio.gather(*[check_agent_health(cfg) for cfg in all_configs])

    narration = await guppy_brief_health(all_configs, results)

    async with aiohttp.ClientSession() as session:
        await send_agent_message(session, ctx.channel, GUPPY_CONFIG, narration)


@bot.command(name="lookup")
async def lookup(ctx: commands.Context, *, query: str) -> None:
    """Search the knowledge base: !lookup <question or topic>"""
    if not vector_db:
        await ctx.send("⚠️ Vector store not initialized. Check logs for errors.")
        return
    
    await ctx.send(f"*Bob is searching the archives for: **{query}**...*")
    
    try:
        results = await vector_db.lookup(query, top_k=5)
        if not results:
            await ctx.send("Bob found nothing relevant in the archives.")
            return
        
        summary = await vector_db.summarize_findings(query, results)
        async with aiohttp.ClientSession() as session:
            await send_agent_message(session, ctx.channel, CHAIRMAN_CONFIG, summary)
    except Exception as e:
        log.error("Lookup failed: %s", e)
        await ctx.send(f"⚠️ Search error: `{e}`")


@bot.command(name="index")
async def index(ctx: commands.Context, *, url_or_text: str) -> None:
    """Index a URL or text for future lookups: !index <url> or !index <text>"""
    if not vector_db:
        await ctx.send("⚠️ Vector store not initialized. Check logs for errors.")
        return
    
    await ctx.send("*Bob is adding this to the archives...*")
    
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            if url_or_text.startswith("http"):
                raw_text = await _fetch_article_text(session, url_or_text)
                source = url_or_text
            else:
                raw_text = url_or_text
                source = "manual-entry"
        
        from datetime import datetime
        doc_id = f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        chunk_count = await vector_db.add_document(
            doc_id=doc_id,
            source=source,
            content=raw_text,
            metadata={"indexed_by": ctx.author.name}
        )
        
        await ctx.send(f"*Bob:* Got it. {len(raw_text)} characters indexed ({chunk_count} chunks).")
    except Exception as e:
        log.error("Index failed: %s", e)
        await ctx.send(f"⚠️ Indexing error: `{e}`")


@bot.command(name="memory")
async def memory(ctx: commands.Context, *, text: str) -> None:
    """Save a personal note or fact: !memory <text>"""
    if not vector_db:
        await ctx.send("⚠️ Vector store not initialized. Check logs for errors.")
        return
    
    try:
        from datetime import datetime
        note_id = f"note_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        await vector_db.add_personal_note(
            note_id=note_id,
            content=text,
            tags=[ctx.author.name]
        )
        await ctx.send(f"*Bob:* Noted. I'll remember this for future lookups.")
    except Exception as e:
        log.error("Memory save failed: %s", e)
        await ctx.send(f"⚠️ Save error: `{e}`")


@bot.command(name="brief")
async def brief(ctx: commands.Context, *, target: Optional[str] = None) -> None:
    """Guppy delivers an intel brief on a URL or topic: !brief <url or topic>"""
    if not target:
        await ctx.send("*Guppy:* Brief on what, exactly? Give me a URL or a topic.")
        return

    await ctx.send("*Guppy, pull up that intel.*")

    try:
        url, topic = _extract_url(target)
        async with aiohttp.ClientSession() as session:
            if url:
                raw_text = await _fetch_article_text(session, url)
                brief_text = await summarize_article(raw_text)
            else:
                brief_text = await guppy_brief_topic(topic)
            await send_agent_message(session, ctx.channel, GUPPY_CONFIG, brief_text)
    except Exception as exc:
        log.error("Brief failed: %s", exc)
        await ctx.send(f"⚠️ Couldn't pull that intel: `{exc}`")


@bot.command(name="debrief")
async def debrief(ctx: commands.Context) -> None:
    """Guppy delivers an after-action report on the last moot: !debrief"""
    if not _last_moot:
        await ctx.send("*Guppy:* No moot on record. Run one first.")
        return

    await ctx.send("*Guppy, give me the after-action.*")

    try:
        report = await guppy_debrief(_last_moot["topic"], _last_moot["history"])
        async with aiohttp.ClientSession() as session:
            await send_agent_message(session, ctx.channel, GUPPY_CONFIG, report)
    except Exception as exc:
        log.error("Debrief failed: %s", exc)
        await ctx.send(f"⚠️ Debrief error: `{exc}`")


_ORDERS_TEXT = """\
**STANDING ORDERS** — Admiral Guppy, Intel Division

**Moot operations**
• `!moot <topic or url>` — Convene the council. Replicants debate, Johansson chairs.
• `!discuss <topic or url>` — Alias for above.
• `!stop` — Terminate active moot immediately.

**Intel** (my department)
• `!brief <url or topic>` — Intel brief without convening a moot.
• `!debrief` — After-action report on the last completed moot.
• `!guppy` — Full system diagnostic in my voice.

**Knowledge base**
• `!lookup <query>` — Johansson searches the archives.
• `!index <url or text>` — Add a source to the knowledge base.
• `!memory <text>` — Log a personal note for future reference.

**Administrative**
• `!health` — Raw API status on all replicants.
• `!status` — Active moot status.
• `!replicants` / `!agents` — List active agents and endpoints.
• `!stats` — Knowledge base document counts.
• `!orders` — This briefing.

Bottom line: you call the moot, I brief the intel, Johansson runs it.\
"""


@bot.command(name="orders")
async def orders(ctx: commands.Context) -> None:
    """Guppy delivers the command list in military-brief style: !orders"""
    async with aiohttp.ClientSession() as session:
        await send_agent_message(session, ctx.channel, GUPPY_CONFIG, _ORDERS_TEXT)


@bot.command(name="stats")
async def stats(ctx: commands.Context) -> None:
    """Show vector database statistics."""
    if not vector_db:
        await ctx.send("⚠️ Vector store not initialized.")
        return
    
    try:
        db_stats = vector_db.get_stats()
        lines = ["**Knowledge Base Stats:**"]
        for collection, stats in db_stats.items():
            lines.append(f"• {collection}: {stats['count']} documents")
        await ctx.send("\n".join(lines))
    except Exception as e:
        log.error("Stats failed: %s", e)
        await ctx.send(f"⚠️ Error: `{e}`")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    if not DISCORD_CHANNEL_ID:
        log.error("DISCORD_CHANNEL_ID is not set.")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)
