"""
Unit tests for council.py — all LLM calls are mocked, no API keys required.

Key design: patch `council._chat` (the single internal function that wraps
AsyncOpenAI) so every test controls exactly what the "LLM" returns.
Temperature is a reliable discriminator:
  0.4  → chairman opening (_chairman_open)
  0.3  → chairman evaluation (_chairman_evaluate)
  0.75 → agent response (_agent_respond) or summarize_article default
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config import AGENT_CONFIGS, CHAIRMAN_CONFIG
from council import (
    DISCUSSION_DONE,
    _chairman_evaluate,
    check_agent_health,
    export_discussion_text,
    guppy_brief_health,
    run_discussion,
    split_by_speaker,
    summarize_article,
)


# ─── Pure-function tests (no mocking needed) ──────────────────────────────────


async def test_export_discussion_text():
    history = [
        {"speaker": "Bob", "text": "Hello"},
        {"speaker": "Riker", "text": "World"},
    ]
    result = await export_discussion_text(history)
    assert "[Bob]: Hello" in result
    assert "[Riker]: World" in result
    assert result.index("[Bob]") < result.index("[Riker]")


def test_split_by_speaker():
    history = [
        {"speaker": "Bob", "text": "Hello"},
        {"speaker": "Riker", "text": "World"},
    ]
    chunks = split_by_speaker(history)
    assert len(chunks) == 2
    assert "[Bob]: Hello" in chunks[0][0]
    assert chunks[0][1]["speaker"] == "Bob"
    assert "[Riker]: World" in chunks[1][0]


# ─── _chairman_evaluate parsing ───────────────────────────────────────────────


async def test_chairman_evaluate_continue():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "CONTINUE: Let's dig deeper into the implications."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=1, max_rounds=3
        )
    assert should_continue is True
    assert "Let's dig deeper" in body
    assert "CONTINUE:" not in body


async def test_chairman_evaluate_conclude():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "CONCLUDE: The group has reached consensus."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=1, max_rounds=3
        )
    assert should_continue is False
    assert "consensus" in body
    assert "CONCLUDE:" not in body


async def test_chairman_evaluate_bold_continue():
    """Bob sometimes wraps the directive in bold markdown."""
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "**CONTINUE:** Push harder on the engineering constraints."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=1, max_rounds=3
        )
    assert should_continue is True
    assert "Push harder" in body


async def test_chairman_evaluate_bold_conclude():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "**CONCLUDE:** That's the final word."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=2, max_rounds=3
        )
    assert should_continue is False
    assert "final word" in body


async def test_chairman_evaluate_leading_whitespace():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "\nCONTINUE: One more round needed."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=1, max_rounds=3
        )
    assert should_continue is True
    assert "One more round" in body


async def test_chairman_evaluate_case_insensitive():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "conclude: wrapping up now."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=1, max_rounds=3
        )
    assert should_continue is False


async def test_chairman_evaluate_last_round_uses_conclude_prompt():
    """On the final round the prompt changes; verify CONCLUDE still parses."""
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "CONCLUDE: Final summary goes here."
        should_continue, body = await _chairman_evaluate(
            AsyncMock(), "topic", [], round_num=3, max_rounds=3
        )
    assert should_continue is False
    assert "Final summary" in body


# ─── run_discussion orchestration ─────────────────────────────────────────────


async def _collect(gen):
    results = []
    async for item in gen:
        results.append(item)
    return results


def _make_fake_chat(eval_responses):
    """
    Returns a fake _chat coroutine.
    eval_responses: list of strings to return on successive evaluation calls
                    (temperature=0.3).
    """
    eval_iter = iter(eval_responses)

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        if config.name == CHAIRMAN_CONFIG.name:
            if temperature == 0.4:
                return "Replicants, the moot is open."
            if temperature == 0.3:
                return next(eval_iter)
        return f"{config.name} has spoken."

    return fake_chat


async def test_discussion_concludes_after_one_round():
    fake = _make_fake_chat(["CONCLUDE: We've reached consensus."])
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("hot dog sandwich"))

    assert results[-1] == (None, DISCUSSION_DONE)
    round_headers = [t for _, t in results if isinstance(t, str) and "Moot — Round" in t]
    assert len(round_headers) == 1


async def test_discussion_all_agents_respond_each_round():
    fake = _make_fake_chat(["CONCLUDE: Done."])
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("topic"))

    spoken = {a.name for a, _ in results if a is not None}
    for cfg in AGENT_CONFIGS:
        assert cfg.name in spoken, f"{cfg.name} never spoke"
    assert CHAIRMAN_CONFIG.name in spoken


async def test_discussion_done_sentinel_is_last():
    fake = _make_fake_chat(["CONCLUDE: Done."])
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("sentinel check"))

    assert results[-1] == (None, DISCUSSION_DONE)


async def test_discussion_continues_multiple_rounds():
    """CONTINUE×2 then CONCLUDE → 3 round headers."""
    eval_calls = []

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        if config.name == CHAIRMAN_CONFIG.name:
            if temperature == 0.4:
                return "Opening the moot."
            if temperature == 0.3:
                eval_calls.append(1)
                return "CONCLUDE: Done." if len(eval_calls) >= 3 else "CONTINUE: Keep going."
        return f"{config.name} responds."

    with patch("council._chat", side_effect=fake_chat):
        results = await _collect(run_discussion("multi-round"))

    round_headers = [t for _, t in results if isinstance(t, str) and "Moot — Round" in t]
    assert len(round_headers) == 3
    assert len(eval_calls) == 3
    assert results[-1] == (None, DISCUSSION_DONE)


async def test_discussion_respects_max_rounds():
    """If the chairman keeps saying CONTINUE, the loop still caps at MAX_ROUNDS."""
    from config import MAX_ROUNDS

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        if config.name == CHAIRMAN_CONFIG.name:
            if temperature == 0.4:
                return "Opening."
            if temperature == 0.3:
                return "CONTINUE: keep going"  # always continue
        return "response"

    with patch("council._chat", side_effect=fake_chat):
        results = await _collect(run_discussion("capped topic"))

    round_headers = [t for _, t in results if isinstance(t, str) and "Moot — Round" in t]
    assert len(round_headers) == MAX_ROUNDS
    assert results[-1] == (None, DISCUSSION_DONE)


async def test_discussion_with_context_note():
    fake = _make_fake_chat(["CONCLUDE: Done."])
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("topic", context_note="extra context here"))

    assert results[-1] == (None, DISCUSSION_DONE)


async def test_discussion_with_image_data():
    """Image data passed through without breaking the generator."""
    fake = _make_fake_chat(["CONCLUDE: Done."])
    fake_image = [{"mime_type": "image/png", "data": "abc123"}]
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("what is this image", image_data=fake_image))

    assert results[-1] == (None, DISCUSSION_DONE)


async def test_discussion_blaaattt_in_conclude_message():
    """The BLAAATTT gavel should appear in the final chairman message."""
    fake = _make_fake_chat(["CONCLUDE: The moot is adjourned."])
    with patch("council._chat", side_effect=fake):
        results = await _collect(run_discussion("topic"))

    bob_messages = [t for a, t in results if a is not None and a.name == CHAIRMAN_CONFIG.name]
    assert any("BLAAATTT" in t for t in bob_messages)


# ─── check_agent_health ───────────────────────────────────────────────────────


async def test_health_ok():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=AsyncMock())
    with patch("council._make_client", return_value=mock_client):
        result = await check_agent_health(CHAIRMAN_CONFIG, timeout=5.0)
    assert result["status"] in ("ok", "warn")
    assert isinstance(result["latency_ms"], int)


async def test_health_error_on_exception():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=ConnectionRefusedError("Connection refused on port 8003")
    )
    with patch("council._make_client", return_value=mock_client):
        result = await check_agent_health(CHAIRMAN_CONFIG, timeout=5.0)
    assert result["status"] == "error"
    assert result["latency_ms"] is None
    assert "Connection refused" in result["detail"]


async def test_health_error_on_timeout():
    with patch("council._make_client"), patch(
        "asyncio.wait_for", side_effect=asyncio.TimeoutError
    ):
        result = await check_agent_health(CHAIRMAN_CONFIG, timeout=5.0)
    assert result["status"] == "error"
    assert result["latency_ms"] is None
    assert "Timeout" in result["detail"]


async def test_health_warn_on_slow_response():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=AsyncMock())

    async def instant_wait_for(coro, timeout):
        return await coro

    with patch("council._make_client", return_value=mock_client), \
         patch("council.asyncio.wait_for", side_effect=instant_wait_for), \
         patch("council.time.monotonic", side_effect=[0.0, 2.5]):
        result = await check_agent_health(CHAIRMAN_CONFIG, timeout=5.0)
    assert result["status"] == "warn"
    assert result["latency_ms"] == 2500


# ─── guppy_brief_health ───────────────────────────────────────────────────────


async def test_guppy_brief_health_returns_narration():
    configs = [CHAIRMAN_CONFIG] + AGENT_CONFIGS
    results = [
        {"status": "ok", "latency_ms": 120, "detail": ""},
        {"status": "error", "latency_ms": None, "detail": "Connection refused"},
        {"status": "ok", "latency_ms": 200, "detail": ""},
        {"status": "warn", "latency_ms": 2500, "detail": ""},
        {"status": "ok", "latency_ms": 180, "detail": ""},
    ]
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "Bottom line: one replicant is down. Someone fix it."
        narration = await guppy_brief_health(configs, results)
    assert narration == "Bottom line: one replicant is down. Someone fix it."
    called_config = mock.call_args[0][1]
    assert called_config.name == "Guppy"


async def test_guppy_brief_health_includes_status_data():
    configs = [CHAIRMAN_CONFIG]
    results = [{"status": "error", "latency_ms": None, "detail": "Connection refused"}]
    captured = {}

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        captured["messages"] = messages
        return "Brief."

    with patch("council._chat", side_effect=fake_chat):
        await guppy_brief_health(configs, results)

    prompt = " ".join(m["content"] for m in captured["messages"])
    assert "Bob" in prompt
    assert "ERROR" in prompt


# ─── summarize_article ────────────────────────────────────────────────────────


async def test_summarize_article_returns_briefing():
    with patch("council._chat", new_callable=AsyncMock) as mock:
        mock.return_value = "Bottom line: this article is about widgets."
        result = await summarize_article("Long article text about widgets " * 50)
    assert "widgets" in result


async def test_summarize_article_truncates_long_input():
    """Input over 12 000 chars should get a truncation marker in the LLM prompt."""
    captured = {}

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        captured["messages"] = messages
        return "Brief."

    with patch("council._chat", side_effect=fake_chat):
        await summarize_article("x" * 20_000)

    all_content = " ".join(
        m["content"] for m in captured["messages"] if isinstance(m.get("content"), str)
    )
    assert "[... article truncated ...]" in all_content


async def test_summarize_article_short_input_not_truncated():
    """Short articles should not have the truncation marker."""
    captured = {}

    async def fake_chat(client, config, messages, max_tokens, temperature=0.75, image_data=None):
        captured["messages"] = messages
        return "Brief."

    with patch("council._chat", side_effect=fake_chat):
        await summarize_article("Short article." * 10)

    all_content = " ".join(
        m["content"] for m in captured["messages"] if isinstance(m.get("content"), str)
    )
    assert "[... article truncated ...]" not in all_content
