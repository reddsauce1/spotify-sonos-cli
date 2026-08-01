"""Tests that /chat reports upstream failures instead of Claude's optimism.

Claude's friendly message ("Paused the music!") is composed before any Sonos
call happens. Branches that dropped the helper's return value therefore told
the user the action succeeded even when Sonos was unreachable -- the response
carried a 502 while the body said everything was fine, and the UI renders the
body.
"""
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
