"""
Discord bot — listens for messages in the designated channel and runs the
council discussion, posting each agent's reply via its own webhook so it
appears as a distinct user in Discord.
"""

import asyncio
import logging
import os
import sys
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
    DISCORD_USER_ID,
    INTER_MESSAGE_DELAY,
    WEBHOOK_URLS,
    AgentConfig,
)
from council import DISCUSSION_DONE, run_discussion

load_dotenv()
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
) -> None:
    global _active_discussion
    _active_discussion = True

    try:
        async with aiohttp.ClientSession() as session:
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

    if _active_discussion:
        await message.reply(
            "A moot is already in progress. "
            "Use `!stop` to cancel it, or wait for the BLAAATTT.",
            delete_after=10,
        )
        return

    # Build topic from message content + any attachments
    topic = message.content.strip()
    context_note = ""

    if message.attachments:
        image_urls = [a.url for a in message.attachments if _is_image(a.filename)]
        if image_urls:
            context_note = "The user also shared an image: " + ", ".join(image_urls)
            if not topic:
                topic = "Discuss the attached image."

    if not topic:
        return  # Empty message with no attachments — ignore

    await message.add_reaction("⏳")
    asyncio.create_task(
        run_council_discussion(message.channel, topic, message.author.id, context_note)
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

    await ctx.message.add_reaction("⏳")
    asyncio.create_task(
        run_council_discussion(ctx.channel, topic, ctx.author.id)
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
