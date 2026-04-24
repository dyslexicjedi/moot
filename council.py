"""
Core discussion orchestration. Runs the multi-agent council meeting and
yields (agent_config, message_text) tuples so the caller (Discord bot or
any other interface) can handle delivery however it wants.
"""

import asyncio
import json
import re
from typing import AsyncGenerator, List, Dict, Optional, Tuple

from openai import AsyncOpenAI

from config import AgentConfig, AgentConfig, AGENT_CONFIGS, CHAIRMAN_CONFIG, MAX_ROUNDS, MAX_TOKENS, CHAIRMAN_MAX_TOKENS


# Sentinel: signals the caller that the discussion is fully done
DISCUSSION_DONE = "__DONE__"


def _make_client(config: AgentConfig) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)


async def _chat(
    client: AsyncOpenAI,
    config: AgentConfig,
    messages: List[Dict],
    max_tokens: int,
    temperature: float = 0.75,
) -> str:
    try:
        resp = await client.chat.completions.create(
            model=config.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        return f"[{config.name} encountered an error: {exc}]"


async def _agent_respond(
    client: AsyncOpenAI,
    agent: AgentConfig,
    topic: str,
    history: List[Dict],
    round_num: int,
    max_rounds: int,
    context_note: str = "",
) -> str:
    system = agent.system_prompt
    user_parts = [
        f"TOPIC: {topic}",
    ]
    if context_note:
        user_parts.append(f"CONTEXT: {context_note}")
    user_parts.append(
        f"This is round {round_num} of {max_rounds}. "
        "Share your perspective, engage with what others have said, "
        "and help move the discussion toward a conclusion."
    )

    messages: List[Dict] = [{"role": "system", "content": system}]

    # Inject prior discussion as assistant/user turns so the model sees it
    for entry in history:
        messages.append({
            "role": "user",
            "content": f"[{entry['speaker']}]: {entry['text']}"
        })

    messages.append({"role": "user", "content": "\n".join(user_parts)})

    return await _chat(client, agent, messages, MAX_TOKENS)


async def _chairman_open(
    client: AsyncOpenAI,
    topic: str,
    agent_names: List[str],
    context_note: str = "",
) -> str:
    names = ", ".join(agent_names)
    prompt = (
        f"Open the moot on this topic:\n\n\"{topic}\"\n\n"
        f"Replicants present: {names}\n"
        + (f"Additional context: {context_note}\n" if context_note else "")
        + "Everyone has gathered at the virtual pub. Introduce the topic clearly "
        + "and invite the first round of perspectives."
    )
    messages = [
        {"role": "system", "content": CHAIRMAN_CONFIG.system_prompt},
        {"role": "user",   "content": prompt},
    ]
    return await _chat(client, CHAIRMAN_CONFIG, messages, CHAIRMAN_MAX_TOKENS, temperature=0.4)


async def _chairman_evaluate(
    client: AsyncOpenAI,
    topic: str,
    history: List[Dict],
    round_num: int,
    max_rounds: int,
) -> Tuple[bool, str]:
    """
    Returns (should_continue: bool, chairman_message: str).
    If should_continue is False, chairman_message is the final summary.
    """
    formatted = "\n".join(
        f"[{e['speaker']}]: {e['text']}" for e in history
    )
    is_last = round_num >= max_rounds

    prompt = (
        f"TOPIC: {topic}\n\n"
        f"DISCUSSION SO FAR:\n{formatted}\n\n"
        f"This was round {round_num} of {max_rounds}.\n"
    )
    if is_last:
        prompt += (
            "This is the final round. Provide a concluding summary: "
            "what consensus was reached, what tensions remain, and the key takeaway. "
            "Your response MUST begin with the literal word CONCLUDE: — no other text before it."
        )
    else:
        prompt += (
            "Assess the discussion. Has meaningful consensus been reached, or "
            "is more discussion valuable?\n"
            "Your response MUST begin with exactly one of these two prefixes — "
            "no synthesis, preamble, or other text before it:\n"
            "  CONTINUE: — if you want another round; then state the specific question "
            "or angle for that round.\n"
            "  CONCLUDE: — if the discussion has reached a solid conclusion; then "
            "summarize the consensus and key takeaway.\n"
            "The very first characters of your response must be CONTINUE: or CONCLUDE:."
        )

    messages = [
        {"role": "system", "content": CHAIRMAN_CONFIG.system_prompt},
        {"role": "user",   "content": prompt},
    ]
    text = await _chat(client, CHAIRMAN_CONFIG, messages, CHAIRMAN_MAX_TOKENS, temperature=0.3)

    # Strip leading markdown/whitespace before checking the directive prefix.
    # Bob sometimes wraps the prefix in bold (**CONTINUE:**) or adds a blank line first.
    cleaned = re.sub(r'^[\s*_`#]+', '', text)
    if re.match(r'CONTINUE:', cleaned, re.IGNORECASE):
        body = re.sub(r'^CONTINUE:\s*', '', cleaned, flags=re.IGNORECASE).strip()
        return True, body
    else:
        body = re.sub(r'^[\s*_`#]*CONCLUDE:\s*', '', text, flags=re.IGNORECASE).strip()
        return False, body


async def run_discussion(
    topic: str,
    context_note: str = "",
) -> AsyncGenerator[Tuple[AgentConfig, str], None]:
    """
    Async generator that yields (AgentConfig, message_text) for every
    message produced during the discussion, in chronological order.

    Yield the special sentinel tuple (None, DISCUSSION_DONE) when finished.
    """
    chairman_client = _make_client(CHAIRMAN_CONFIG)
    agent_clients = [(_make_client(cfg), cfg) for cfg in AGENT_CONFIGS]
    history: List[Dict] = []

    # ── Opening ──────────────────────────────────────────────────────────────
    agent_names = [cfg.name for _, cfg in agent_clients]
    opening = await _chairman_open(chairman_client, topic, agent_names, context_note)
    history.append({"speaker": CHAIRMAN_CONFIG.name, "text": opening})
    yield (CHAIRMAN_CONFIG, opening)

    for round_num in range(1, MAX_ROUNDS + 1):
        # Round header from chairman
        round_header = f"**Moot — Round {round_num} of {MAX_ROUNDS}** — Replicants, the floor is yours."
        yield (CHAIRMAN_CONFIG, round_header)

        # Each agent responds in turn
        for client, agent in agent_clients:
            await asyncio.sleep(0)  # yield control so Discord can send queued messages
            response = await _agent_respond(
                client, agent, topic, history, round_num, MAX_ROUNDS, context_note
            )
            history.append({"speaker": agent.name, "text": response})
            yield (agent, response)

        # Chairman evaluates
        should_continue, eval_text = await _chairman_evaluate(
            chairman_client, topic, history, round_num, MAX_ROUNDS
        )
        history.append({"speaker": CHAIRMAN_CONFIG.name, "text": eval_text})

        if not should_continue:
            yield (CHAIRMAN_CONFIG, f"**BLAAATTT**\n\n{eval_text}")
            break
        else:
            yield (CHAIRMAN_CONFIG, eval_text)

    yield (None, DISCUSSION_DONE)
