"""
Discord bot — listens for messages in the designated channel and runs the
council discussion, posting each agent's reply via its own webhook so it
appears as a distinct user in Discord.
"""

import asyncio
import logging
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
    INTER_MESSAGE_DELAY,
    WEBHOOK_URLS,
    AgentConfig,
)
from council import DISCUSSION_DONE, run_discussion, summarize_article

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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MootBot/1.0)"}
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

# ─── Discord setup ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Track active discussions so we don't run two at once in the same channel
_active_discussion: bool = False


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
) -> None:
    global _active_discussion
    _active_discussion = True

    try:
        async with aiohttp.ClientSession() as session:
            if url:
                await send_agent_message(
                    session, channel, CHAIRMAN_CONFIG,
                    "*Stand by — pulling up the article...*"
                )
                try:
                    raw_text = await _fetch_article_text(session, url)
                    summary = await summarize_article(raw_text)
                    await send_agent_message(session, channel, CHAIRMAN_CONFIG, summary)
                    await asyncio.sleep(INTER_MESSAGE_DELAY)
                    context_note = f"Bob has pre-read the article and briefed the replicants:\n\n{summary}"
                except Exception as exc:
                    log.error("Article fetch/summarize failed: %s", exc)
                    await channel.send(f"⚠️ Couldn't read the article: `{exc}` — proceeding without it.")

            async for agent, text in run_discussion(topic, context_note):
                if text == DISCUSSION_DONE:
                    break

                await send_agent_message(session, channel, agent, text)
                await asyncio.sleep(INTER_MESSAGE_DELAY)

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
    log.info("We are Legion. We are Bob.")
    log.info("Logged in as %s (id=%d)", bot.user, bot.user.id)
    log.info("Watching channel id=%d", DISCORD_CHANNEL_ID)
    webhooks_configured = sum(1 for v in WEBHOOK_URLS.values() if v)
    log.info("Webhooks configured: %d / %d", webhooks_configured, len(WEBHOOK_URLS))
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

    if message.attachments:
        image_urls = [a.url for a in message.attachments if _is_image(a.filename)]
        if image_urls:
            context_note = "The user also shared an image: " + ", ".join(image_urls)
            if not topic:
                topic = "Discuss the attached image."

    if not topic and not url:
        return  # Empty message with no content — ignore

    await message.add_reaction("⏳")
    asyncio.create_task(
        run_council_discussion(message.channel, topic, message.author.id, context_note, url=url)
    )


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
async def force_discuss(ctx: commands.Context, *, topic: str) -> None:
    """Manually trigger a moot: !moot <topic>  (alias: !discuss)"""
    global _active_discussion
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("This command only works in the moot channel.")
        return
    if _active_discussion:
        await ctx.send("A moot is already running. Use `!stop` first.")
        return

    url, topic = _extract_url(topic)
    await ctx.message.add_reaction("⏳")
    asyncio.create_task(
        run_council_discussion(ctx.channel, topic, ctx.author.id, url=url)
    )


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


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    if not DISCORD_CHANNEL_ID:
        log.error("DISCORD_CHANNEL_ID is not set.")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)
