"""Tests for call_claude, which drives /chat -- the endpoint that spends money.

The old implementation posted raw JSON and then indexed
result['content'][0]['text'] with no structure check, so an API error response
(which has no 'content' key) surfaced as KeyError: 'content' -- swallowed by a
bare `except Exception` into a generic apology. These tests pin the failure
modes to specific, distinguishable outcomes.
"""
import logging
import types
from unittest.mock import patch

import anthropic
import httpx
import pytest


def _response(text, stop_reason="end_turn"):
    """A stand-in for the SDK's Message object."""
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
    )


def _api_status_error(status_code, message):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError(message, response=response, body=None)


class TestHappyPath:
    def test_returns_parsed_command(self, server_mod):
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.return_value = _response(
                '{"action": "search", "query": "jazz", "message": "Finding jazz!"}'
            )
            result = server_mod.call_claude("play some jazz")
        assert result == {"action": "search", "query": "jazz", "message": "Finding jazz!"}

    def test_sends_the_schema_and_skips_thinking(self, server_mod):
        """The schema is what guarantees parseable output; thinking off keeps
        a request that blocks someone at a speaker fast."""
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.return_value = _response('{"action": "chat", "message": "hi"}')
            server_mod.call_claude("hello")

        kwargs = mock_claude.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5"
        assert kwargs["thinking"] == {"type": "disabled"}
        assert kwargs["output_config"]["format"]["schema"] is server_mod.DJ_COMMAND_SCHEMA
        assert kwargs["output_config"]["effort"] == "low"

    def test_no_api_key_returns_none(self, server_mod, monkeypatch):
        """Callers treat None as 'natural language unavailable'."""
        monkeypatch.setattr(server_mod, "ANTHROPIC_API_KEY", "")
        assert server_mod.call_claude("play jazz") is None


class TestSchema:
    def test_every_action_the_chat_handler_understands_is_allowed(self, server_mod):
        """If chat() grows a branch the schema doesn't list, Claude can never
        emit it and the feature is silently dead."""
        handled = {
            "search", "play", "queue", "next", "pause", "resume", "skip",
            "previous", "volume", "nowplaying", "showqueue", "clear", "chat",
        }
        allowed = set(server_mod.DJ_COMMAND_SCHEMA["properties"]["action"]["enum"])
        assert handled <= allowed, f"chat() handles actions the schema forbids: {handled - allowed}"

    def test_schema_is_strict(self, server_mod):
        """additionalProperties:false is required for structured outputs."""
        assert server_mod.DJ_COMMAND_SCHEMA["additionalProperties"] is False
        assert set(server_mod.DJ_COMMAND_SCHEMA["required"]) == {"action", "message"}


class TestFailureModes:
    def test_rate_limit_gives_a_distinct_message(self, server_mod):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request)
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.side_effect = anthropic.RateLimitError(
                "rate limited", response=response, body=None
            )
            result = server_mod.call_claude("play jazz")
        assert result["action"] == "chat"
        assert "try again" in result["message"].lower()

    def test_billing_error_is_logged_not_swallowed(self, server_mod, caplog):
        """Regression: the live account hit exactly this. The old code raised
        KeyError('content') and printed nothing useful about the real cause.

        Asserted via caplog rather than capsys: the log handler binds to
        sys.stdout at import time, so capsys (which swaps sys.stdout later)
        never sees these records.
        """
        with caplog.at_level(logging.INFO, logger="dj"):
            with patch.object(server_mod, "claude") as mock_claude:
                mock_claude.messages.create.side_effect = _api_status_error(
                    400, "Your credit balance is too low to access the Anthropic API."
                )
                result = server_mod.call_claude("play jazz")

        assert result["action"] == "chat"
        assert "400" in caplog.text
        assert "credit balance" in caplog.text

    def test_connection_error_tells_user_to_use_buttons(self, server_mod):
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.side_effect = anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )
            result = server_mod.call_claude("play jazz")
        assert result["action"] == "chat"
        assert "buttons" in result["message"]

    def test_refusal_is_handled_before_reading_content(self, server_mod):
        """A refusal can carry empty content; indexing it would raise."""
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.return_value = types.SimpleNamespace(
                content=[], stop_reason="refusal"
            )
            result = server_mod.call_claude("something disallowed")
        assert result["action"] == "chat"

    def test_truncated_response_does_not_raise(self, server_mod):
        """max_tokens truncation yields no text block."""
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.return_value = types.SimpleNamespace(
                content=[], stop_reason="max_tokens"
            )
            result = server_mod.call_claude("play jazz")
        assert result["action"] == "chat"


class TestNoRawHTTP:
    def test_call_claude_does_not_use_requests(self, server_mod):
        """The SDK owns retries and error typing -- a raw post would bypass both."""
        with patch.object(server_mod, "claude") as mock_claude:
            mock_claude.messages.create.return_value = _response('{"action": "chat", "message": "hi"}')
            with patch.object(server_mod.requests, "post") as mock_post:
                server_mod.call_claude("hello")
        mock_post.assert_not_called()
