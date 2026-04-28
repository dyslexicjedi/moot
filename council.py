"""
Core discussion orchestration. Runs the multi-agent council meeting and
yields (agent_config, message_text) tuples so the caller (Discord bot or
any other interface) can handle delivery however it wants.
"""

import asyncio
import re
import time
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from config import AgentConfig, AGENT_CONFIGS, CHAIRMAN_CONFIG, GUPPY_CONFIG, MAX_ROUNDS, MAX_TOKENS, CHAIRMAN_MAX_TOKENS

_ARTICLE_TRUNCATE = 12_000  # chars — keeps Bob's context reasonable


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
    image_data: Optional[List[Dict]] = None,
) -> str:
    try:
        if image_data and config.supports_vision:
            content_parts = []
            for img in image_data:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img['mime_type']};base64,{img['data']}",
                        "detail": "high",
                    },
                })
            for m in messages:
                if m["role"] == "user":
                    text = m["content"] if isinstance(m["content"], str) else str(m["content"])
                    content_parts.append({"type": "text", "text": text})
            api_messages: List[Dict] = []
            for m in messages:
                if m["role"] == "system":
                    api_messages.append({"role": "system", "content": m["content"]})
                elif m["role"] in ("user", "assistant"):
                    pass
            api_messages.append({"role": "user", "content": content_parts})
            resp = await client.chat.completions.create(
                model=config.model,
                messages=api_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        else:
            resp = await client.chat.completions.create(
                model=config.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        content = resp.choices[0].message.content
        if not content:
            return f"[{config.name} returned an empty response]"
        return content.strip()
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
    image_data: Optional[List[Dict]] = None,
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

    return await _chat(client, agent, messages, MAX_TOKENS, image_data=image_data)


async def _chairman_open(
    client: AsyncOpenAI,
    topic: str,
    agent_names: List[str],
    context_note: str = "",
    image_data: Optional[List[Dict]] = None,
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
    return await _chat(client, CHAIRMAN_CONFIG, messages, CHAIRMAN_MAX_TOKENS, temperature=0.4, image_data=image_data)


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


async def summarize_article(raw_text: str) -> str:
    """Guppy reads raw article text and returns a briefing for the moot."""
    client = _make_client(GUPPY_CONFIG)
    truncated = raw_text[:_ARTICLE_TRUNCATE]
    if len(raw_text) > _ARTICLE_TRUNCATE:
        truncated += "\n\n[... article truncated ...]"
    messages = [
        {"role": "system", "content": GUPPY_CONFIG.system_prompt},
        {"role": "user", "content": (
            "Intelligence report for the replicants. Read this and hand out a clean "
            "brief: key points, main argument, anything worth arguing about. "
            "Under 300 words — Johansson's waiting on this.\n\n"
            f"INTELLIGENCE:\n{truncated}"
        )},
    ]
    return await _chat(client, GUPPY_CONFIG, messages, 450, temperature=0.3)


async def run_discussion(
    topic: str,
    context_note: str = "",
    image_data: Optional[List[Dict]] = None,
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
    opening = await _chairman_open(chairman_client, topic, agent_names, context_note, image_data)
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
                client, agent, topic, history, round_num, MAX_ROUNDS, context_note, image_data
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


async def guppy_brief_topic(topic: str) -> str:
    """Guppy gives a quick intel brief on a plain-text topic (no article to read)."""
    client = _make_client(GUPPY_CONFIG)
    messages = [
        {"role": "system", "content": GUPPY_CONFIG.system_prompt},
        {"role": "user", "content": (
            "Intel brief requested by the moot chair. "
            "Give the replicants a tactical overview of this topic: "
            "key factions, main tensions, bottom line. Under 250 words.\n\n"
            f"TOPIC: {topic}"
        )},
    ]
    return await _chat(client, GUPPY_CONFIG, messages, 400, temperature=0.35)


async def guppy_debrief(topic: str, history: List[Dict]) -> str:
    """Guppy delivers an after-action report on a completed moot."""
    formatted = "\n".join(f"[{e['speaker']}]: {e['text']}" for e in history)
    client = _make_client(GUPPY_CONFIG)
    messages = [
        {"role": "system", "content": GUPPY_CONFIG.system_prompt},
        {"role": "user", "content": (
            "Post-moot debrief. Read the transcript and give the replicants a "
            "tight after-action report: what was decided, what was left open, "
            "and the one thing that actually mattered. Under 250 words.\n\n"
            f"TOPIC: {topic}\n\n"
            f"TRANSCRIPT:\n{formatted[:8000]}"
        )},
    ]
    return await _chat(client, GUPPY_CONFIG, messages, 400, temperature=0.3)


async def guppy_brief_health(configs: List[AgentConfig], results: List[Dict]) -> str:
    """Ask Guppy to narrate health check results in character."""
    lines = ["System diagnostic. All replicants stand by.", "", "STATUS REPORT:"]
    for cfg, result in zip(configs, results):
        status = result["status"].upper()
        if result["latency_ms"] is not None:
            detail = f"{result['latency_ms']}ms — {cfg.model}"
            if result["status"] == "warn":
                detail += " (slow)"
        else:
            detail = result["detail"] or "unknown error"
        lines.append(f"- {cfg.name}: {status} {detail}")
    lines += ["", "Deliver your assessment to the replicants."]

    client = _make_client(GUPPY_CONFIG)
    messages = [
        {"role": "system", "content": GUPPY_CONFIG.system_prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]
    return await _chat(client, GUPPY_CONFIG, messages, max_tokens=300, temperature=0.5)


async def check_agent_health(config: AgentConfig, timeout: float = 5.0) -> Dict:
    """Ping one agent endpoint and return {"status", "latency_ms", "detail"}."""
    client = _make_client(config)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Respond with only the word OK."},
    ]
    start = time.monotonic()
    try:
        await asyncio.wait_for(
            client.chat.completions.create(
                model=config.model,
                messages=messages,
                max_tokens=5,
                temperature=0.0,
            ),
            timeout=timeout,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        status = "warn" if latency_ms > 2000 else "ok"
        return {"status": status, "latency_ms": latency_ms, "detail": ""}
    except asyncio.TimeoutError:
        return {"status": "error", "latency_ms": None, "detail": f"Timeout after {timeout:.0f}s"}
    except Exception as exc:
        return {"status": "error", "latency_ms": None, "detail": str(exc)[:80]}


async def export_discussion_text(history: List[Dict]) -> str:
    """Export discussion history as plain text for vector storage."""
    lines = []
    for entry in history:
        lines.append(f"[{entry['speaker']}]: {entry['text']}")
    return "\n\n".join(lines)


def split_by_speaker(history: List[Dict]) -> List[Tuple[str, Dict]]:
    """Return list of (chunk_text, entry) tuples for per-speaker indexing."""
    chunks = []
    for entry in history:
        chunk_text = f"[{entry['speaker']}]: {entry['text']}"
        chunks.append((chunk_text, entry))
    return chunks
