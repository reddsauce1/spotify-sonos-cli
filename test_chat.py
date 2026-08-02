"""Tests that /chat reports upstream failures instead of Claude's optimism.

Claude's friendly message ("Paused the music!") is composed before any Sonos
call happens. Branches that dropped the helper's return value therefore told
the user the action succeeded even when Sonos was unreachable -- the response
carried a 502 while the body said everything was fine, and the UI renders the
body.

Also covers the limits on /chat, which is the only endpoint that costs money:
it bills the Anthropic key on every call, and the message reaches the model
as input tokens verbatim.
"""
import re
from unittest.mock import patch

import pytest
import requests


ACTIONS_WITH_NO_ARGS = ["pause", "resume", "skip", "previous", "clear"]


def _claude(action, **extra):
    return {"action": action, "message": f"✨ did {action}!", **extra}


class TestUpstreamFailureSurfaces:
    @pytest.mark.parametrize("action", ACTIONS_WITH_NO_ARGS)
    def test_sonos_down_is_reported(self, dj, server_mod, action):
        with patch.object(server_mod, "call_claude", return_value=_claude(action)):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                result = dj.chat(message="do the thing")

        assert "error" in result, f"{action} swallowed the failure"
        assert result["message"].startswith("❌"), result["message"]
        assert "did" not in result["message"], "optimistic message survived"

    def test_volume_reports_failure(self, dj, server_mod):
        with patch.object(server_mod, "call_claude",
                          return_value=_claude("volume", level=40)):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                result = dj.chat(message="turn it down")
        assert result["message"].startswith("❌")

    def test_nowplaying_reports_failure(self, dj, server_mod):
        with patch.object(server_mod, "call_claude", return_value=_claude("nowplaying")):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                result = dj.chat(message="what's on")
        assert result["message"].startswith("❌")

    def test_showqueue_failure_is_not_reported_as_empty(self, dj, server_mod):
        """_do_getqueue returns {"queue": [], "error": ...} on failure, so the
        empty-queue branch would otherwise claim the queue is empty."""
        with patch.object(server_mod, "call_claude", return_value=_claude("showqueue")):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                result = dj.chat(message="show the queue")

        assert "empty" not in result["message"].lower(), result["message"]
        assert result["message"].startswith("❌")

    def test_status_code_and_body_agree(self, dj, server_mod):
        """A 502 with a cheerful body is worse than either alone."""
        with patch.object(server_mod, "call_claude", return_value=_claude("pause")):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                result = dj.chat(message="pause")

        assert server_mod.cherrypy.response.status == 502
        assert "error" in result


class TestSuccessPathUnchanged:
    @pytest.mark.parametrize("action,stub", [
        ("pause", {"status": "paused"}),
        ("resume", {"status": "playing"}),
        ("skip", {"status": "skipped"}),
    ])
    def test_friendly_message_is_kept(self, dj, server_mod, action, stub):
        with patch.object(server_mod, "call_claude", return_value=_claude(action)):
            with patch.object(dj, f"_do_{action}", return_value=stub):
                result = dj.chat(message="go")

        assert result["message"] == f"✨ did {action}!"
        assert "error" not in result

    def test_play_still_names_the_track(self, dj, server_mod):
        item = {"name": "So What", "artist": "Miles Davis"}
        with patch.object(server_mod, "call_claude",
                          return_value=_claude("play", num=1)):
            with patch.object(dj, "_do_play", return_value={"status": "playing", "item": item}):
                result = dj.chat(message="play 1")

        assert "So What" in result["message"]
        assert "error" not in result

    def test_queue_listing_still_works(self, dj, server_mod):
        queue = [{"title": "A"}, {"title": "B"}]
        with patch.object(server_mod, "call_claude", return_value=_claude("showqueue")):
            with patch.object(dj, "_do_getqueue", return_value={"queue": queue, "limit": 50}):
                result = dj.chat(message="queue?")

        assert "2 tracks" in result["message"]
        assert result["queue"] == queue


class TestEveryActionCapturesItsOutcome:
    def test_no_branch_discards_the_helper_result(self):
        """A branch calling self._do_*() without assigning it cannot be
        checked for errors -- that is the bug this file exists for."""
        import os
        import re

        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'server.py')) as f:
            source = f.read()

        chat_body = source.split("def chat(", 1)[1].split("\n    @cherrypy.expose", 1)[0]
        offenders = [
            line.strip() for line in chat_body.splitlines()
            if re.match(r'\s*self\._do_\w+\(', line)
        ]
        assert not offenders, f"result discarded: {offenders}"


# /chat is the only endpoint that costs money -- it bills the Anthropic key on
# every call, and the message becomes input tokens verbatim.
class TestChatSpendLimits:
    """It bills the Anthropic key on every call, and the message becomes
    input tokens verbatim."""

    @pytest.fixture(autouse=True)
    def _clear(self, server_mod):
        server_mod._chat_calls.clear()
        yield
        server_mod._chat_calls.clear()

    def test_a_long_message_is_refused_before_it_reaches_claude(self, dj, server_mod):
        with patch.object(server_mod, "call_claude") as claude:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.chat(message="A" * (server_mod.MAX_CHAT_MESSAGE_CHARS + 1))
        assert exc.value.status == 400
        claude.assert_not_called()   # a rejected message must not be paid for

    def test_a_normal_message_is_allowed(self, dj, server_mod):
        with patch.object(server_mod, "call_claude", return_value=None):
            result = dj.chat(message="play something chill")
        assert "error" in result and "not configured" in result["error"]

    def test_a_message_at_exactly_the_limit_is_allowed(self, dj, server_mod):
        with patch.object(server_mod, "call_claude", return_value=None) as claude:
            dj.chat(message="A" * server_mod.MAX_CHAT_MESSAGE_CHARS)
        claude.assert_called_once()

    def test_the_rate_limit_bites(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 3)
        with patch.object(server_mod, "call_claude", return_value=None):
            for _ in range(3):
                dj.chat(message="hi", session_id="s1")
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.chat(message="hi", session_id="s1")
        assert exc.value.status == 429

    def test_the_429_says_how_long_to_wait(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 1)
        with patch.object(server_mod, "call_claude", return_value=None):
            dj.chat(message="hi", session_id="s2")
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.chat(message="hi", session_id="s2")
        assert re.search(r"try again in \d+s", str(exc.value))

    def test_the_429_sets_retry_after(self, dj, server_mod, monkeypatch):
        """The message is for the person; the header is for anything scripted."""
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 1)
        with patch.object(server_mod, "call_claude", return_value=None):
            dj.chat(message="hi", session_id="s3")
            with pytest.raises(server_mod.cherrypy.HTTPError):
                dj.chat(message="hi", session_id="s3")
        assert int(server_mod.cherrypy.response.headers["Retry-After"]) >= 1

    def test_a_rate_limited_call_never_reaches_claude(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 1)
        with patch.object(server_mod, "call_claude", return_value=None) as claude:
            dj.chat(message="hi", session_id="s4")
            with pytest.raises(server_mod.cherrypy.HTTPError):
                dj.chat(message="hi", session_id="s4")
        assert claude.call_count == 1

    def test_one_session_cannot_exhaust_another(self, dj, server_mod, monkeypatch):
        """Otherwise one guest hammering the page locks out the whole party."""
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 2)
        with patch.object(server_mod, "call_claude", return_value=None):
            for _ in range(2):
                dj.chat(message="hi", session_id="noisy")
            dj.chat(message="hi", session_id="quiet")  # must not raise

    def test_the_window_slides_rather_than_resetting_on_the_minute(self, dj, server_mod,
                                                                   monkeypatch):
        """A fixed window would let a caller take a double allowance by
        straddling the boundary."""
        monkeypatch.setattr(server_mod, "CHAT_CALLS_PER_MINUTE", 2)
        clock = [1000.0]
        monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock[0])
        with patch.object(server_mod, "call_claude", return_value=None):
            dj.chat(message="hi", session_id="s5")
            clock[0] += 59
            dj.chat(message="hi", session_id="s5")
            with pytest.raises(server_mod.cherrypy.HTTPError):
                dj.chat(message="hi", session_id="s5")

            clock[0] += 2          # the first call ages out, the second has not
            dj.chat(message="hi", session_id="s5")

    def test_the_call_log_is_bounded(self, dj, server_mod, monkeypatch):
        """A public URL means every distinct session_id the UI invents becomes
        a key, exactly as the search-result cache once did."""
        monkeypatch.setattr(server_mod, "MAX_CHAT_SESSIONS", 10)
        with patch.object(server_mod, "call_claude", return_value=None):
            for i in range(50):
                dj.chat(message="hi", session_id=f"s{i}")
        assert len(server_mod._chat_calls) <= 10


# ==================== a typo should not be a 500 ==========================
