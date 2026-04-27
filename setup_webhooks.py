"""
One-time setup script: creates one Discord webhook per agent in the council
channel and writes the webhook URLs into your .env file.

Run once:
    python setup_webhooks.py

Requires DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, and DISCORD_CHANNEL_ID in .env.
The bot must have the Manage Webhooks permission in the target channel.
"""

import asyncio
import os
import re
import sys

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN       = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID  = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Agent names we need webhooks for
from config import AGENT_CONFIGS, CHAIRMAN_CONFIG, GUPPY_CONFIG  # noqa: E402
ALL_AGENTS = [CHAIRMAN_CONFIG, GUPPY_CONFIG] + list(AGENT_CONFIGS)


def _update_env_file(key: str, value: str, env_path: str = ".env") -> None:
    """Insert or replace a key=value line in the .env file."""
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(f"{key}={value}\n")
        return

    with open(env_path, "r") as f:
        lines = f.readlines()

    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    replaced = False
    new_lines = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        new_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)


async def main() -> None:
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not CHANNEL_ID:
        print("ERROR: DISCORD_CHANNEL_ID not set in .env")
        sys.exit(1)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            print(f"ERROR: Could not find channel {CHANNEL_ID}. "
                  "Make sure the bot is in the server and has access.")
            await client.close()
            return

        if not isinstance(channel, discord.TextChannel):
            print("ERROR: The channel must be a text channel.")
            await client.close()
            return

        # Fetch existing webhooks to avoid duplicates
        existing = {wh.name: wh for wh in await channel.webhooks()}

        for agent in ALL_AGENTS:
            env_key = f"WEBHOOK_{agent.name.upper()}"
            if agent.name in existing:
                wh = existing[agent.name]
                print(f"  {agent.name}: already exists → {wh.url}")
            else:
                wh = await channel.create_webhook(name=agent.name)
                print(f"  {agent.name}: created → {wh.url}")

            _update_env_file(env_key, wh.url)

        print("\n.env updated with webhook URLs. Restart discord_bot.py to use them.")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
