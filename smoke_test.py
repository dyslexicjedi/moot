#!/usr/bin/env python3
"""
Smoke test — makes real API calls to verify the full stack works end-to-end.
Requires API keys in .env (at minimum OPENROUTER_API_KEY).

Run:
    python smoke_test.py
    python smoke_test.py "Is a hot dog a sandwich?"   # custom topic
"""

import asyncio
import sys
import time

from config import AGENT_CONFIGS, CHAIRMAN_CONFIG
from council import DISCUSSION_DONE, run_discussion

SMOKE_TOPIC = "Is a hot dog a sandwich? Settle this once and for all."


async def main(topic: str) -> None:
    print(f"Smoke test topic: {topic!r}")
    print("Making real API calls — this will take 30-90 seconds and cost API credits.\n")

    start = time.monotonic()
    messages: list[tuple[str, str]] = []
    agents_seen: set[str] = set()
    errors: list[str] = []

    try:
        async for agent, text in run_discussion(topic):
            if text == DISCUSSION_DONE:
                break
            if agent is None:
                continue
            agents_seen.add(agent.name)
            preview = text[:100].replace("\n", " ")
            print(f"  [{agent.name}] {preview}...")
            messages.append((agent.name, text))
    except Exception as exc:
        errors.append(f"run_discussion raised: {exc}")

    elapsed = time.monotonic() - start

    # ── Assertions ────────────────────────────────────────────────────────────

    if not messages:
        errors.append("No messages produced")

    if CHAIRMAN_CONFIG.name not in agents_seen:
        errors.append(f"Chairman ({CHAIRMAN_CONFIG.name}) never spoke")

    missing_agents = {cfg.name for cfg in AGENT_CONFIGS} - agents_seen
    if missing_agents:
        errors.append(f"Agents never spoke: {missing_agents}")

    api_errors = [
        f"  [{name}]: {text}"
        for name, text in messages
        if text.startswith(f"[{name} encountered an error:")
    ]
    if api_errors:
        errors.append("API error responses:\n" + "\n".join(api_errors))

    empty_responses = [(name, text) for name, text in messages if not text.strip()]
    if empty_responses:
        errors.append(f"Empty responses from: {[n for n, _ in empty_responses]}")

    blaaattt_messages = [t for _, t in messages if "BLAAATTT" in t]
    if not blaaattt_messages:
        errors.append("Chairman never called BLAAATTT — discussion may not have concluded properly")

    # ── Report ────────────────────────────────────────────────────────────────

    print()
    if errors:
        print(f"FAILED ({elapsed:.1f}s)")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)
    else:
        print(
            f"PASSED ({elapsed:.1f}s) — "
            f"{len(messages)} messages from {sorted(agents_seen)}"
        )
        sys.exit(0)


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else SMOKE_TOPIC
    asyncio.run(main(topic))
