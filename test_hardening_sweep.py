"""Tests for the findings of the August 2026 review of everything added
after docs/hardening-2026-08.md.

server.py roughly doubled after that pass -- scheduler, stations, queue
editing, seek, shuffle -- and none of it had been reviewed. Nothing serious
turned up: auth covers every endpoint, no lock is held across a network call,
both JSON files write atomically, and nothing reached the Sonos path. What did
turn up was a handful of ways to get a 500 out of a typo, one unbounded dict,
and one endpoint that spends money without a limit.
"""
import re
import threading
from unittest.mock import patch

import pytest


# ==================== /chat is the only endpoint that costs money ==========


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


class TestUnexpectedParametersAre404:
    """CherryPy reads the handler's signature with inspect.getfullargspec,
    which does not follow the __wrapped__ chain functools.wraps sets. Every
    handler wrapped in _handles_spotify_errors therefore looked like
    (*args, **kwargs): an unknown parameter was accepted, then blew up inside
    the call as a 500 with a stack trace."""

    DECORATED = ["my", "like", "recommend", "album_tracks",
                 "create_playlist", "add_to_playlist"]

    @pytest.mark.parametrize("name", DECORATED)
    def test_the_signature_survives_the_decorator(self, server_mod, name):
        import inspect
        spec = inspect.getfullargspec(getattr(server_mod.DJServer, name))
        assert spec.varkw is None, f"{name} still accepts arbitrary kwargs"
        assert spec.args[0] == "self"

    @pytest.mark.parametrize("name", DECORATED)
    def test_the_declared_parameters_are_the_real_ones(self, server_mod, name):
        """A signature that survives but is wrong would reject valid calls."""
        import inspect
        handler = getattr(server_mod.DJServer, name)
        assert inspect.getfullargspec(handler).args == \
            inspect.getfullargspec(handler.__wrapped__).args

    def test_an_undecorated_handler_is_unaffected(self, server_mod):
        import inspect
        spec = inspect.getfullargspec(server_mod.DJServer.nowplaying)
        assert spec.varkw is None


# ==================== input bounds =======================================


class TestValidateText:
    def test_it_strips(self, server_mod):
        assert server_mod._validate_text("  hi  ", "name", 10) == "hi"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_is_rejected(self, server_mod, bad):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_text(bad, "name", 10)
        assert exc.value.status == 400

    def test_over_length_is_rejected_not_truncated(self, server_mod):
        """A silently shortened playlist name is a playlist nobody asked for."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_text("A" * 11, "name", 10)
        assert exc.value.status == 400

    def test_exactly_the_limit_is_allowed(self, server_mod):
        assert server_mod._validate_text("A" * 10, "name", 10) == "A" * 10

    @pytest.mark.parametrize("ch", [chr(0), chr(13), chr(10), chr(9), chr(27), chr(0x7f)])
    def test_control_characters_are_rejected(self, server_mod, ch):
        """A NUL in a search query reached Spotify and came back a 502,
        blaming the upstream for the caller's input."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_text("a" + ch + "b", "q", 50)
        assert exc.value.status == 400

    def test_ordinary_punctuation_and_accents_survive(self, server_mod):
        for text in ["Rock 'n' Roll", "Björk", "1/2 of 3", "café — 90%", "日本語"]:
            assert server_mod._validate_text(text, "q", 50) == text


class TestSearchQueryIsValidated:
    def test_a_control_character_never_reaches_spotify(self, dj, server_mod):
        with patch.object(server_mod, "sp") as spotify:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_search(q="a" + chr(0) + "b")
        assert exc.value.status == 400
        spotify.search.assert_not_called()

    def test_a_normal_query_still_works(self, dj, server_mod):
        with patch.object(server_mod, "sp") as spotify:
            spotify.search.return_value = {"tracks": {"items": []}}
            dj._do_search(q="daft punk")
        assert spotify.search.call_args.kwargs["q"] == "daft punk"


class TestPlaylistNameIsBounded:
    def test_a_long_name_is_a_400_not_a_502(self, dj, server_mod):
        """Unbounded, it reached Spotify and returned an upstream error for
        what is really a bad request."""
        with patch.object(server_mod, "sp") as spotify:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.create_playlist(name="A" * 500)
        assert exc.value.status == 400
        spotify.user_playlist_create.assert_not_called()

    def test_a_missing_name_is_still_a_400(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.create_playlist()
        assert exc.value.status == 400

    def test_a_normal_name_still_works(self, dj, server_mod):
        with patch.object(server_mod, "sp") as spotify:
            spotify.current_user.return_value = {"id": "u1"}
            spotify.user_playlist_create.return_value = {
                "name": "Party", "uri": "spotify:playlist:p1", "id": "p1"}
            assert dj.create_playlist(name="Party")["name"] == "Party"
        assert spotify.user_playlist_create.call_args[0][1] == "Party"


# ==================== the last unguarded shared dict =====================


class TestSearchResultsAreThreadSafe:
    """CherryPy serves on a thread pool, and the expiry sweep is not safe under
    that: it iterates the dict looking for stale entries, then picks the oldest
    key and deletes it. A concurrent insert mid-iteration raises "dictionary
    changed size during iteration"; two threads choosing the same oldest key
    raise KeyError on the second delete."""

    @pytest.fixture
    def interleaved(self):
        """Shorten the thread switch interval so the race is hit reliably.

        At the default 5ms the window is small enough that a few hundred
        iterations pass even with no lock at all -- an earlier version of this
        test did, and would have let the lock be deleted silently.
        """
        import sys
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-9)
        yield
        sys.setswitchinterval(previous)

    def test_there_is_a_lock(self, server_mod):
        assert isinstance(server_mod._results_lock, type(threading.Lock()))

    @pytest.mark.parametrize("accessor", ["set_results", "get_results"])
    def test_both_accessors_take_it(self, server_mod, accessor):
        import inspect
        body = inspect.getsource(getattr(server_mod, accessor))
        assert "with _results_lock:" in body

    def test_concurrent_writes_do_not_raise(self, server_mod, monkeypatch, interleaved):
        """The eviction path is the dangerous one, so the cap is set low enough
        that every write triggers it."""
        monkeypatch.setattr(server_mod, "MAX_SEARCH_SESSIONS", 4)
        monkeypatch.setattr(server_mod, "search_results", {})
        errors = []

        def hammer(base):
            try:
                for i in range(3000):
                    server_mod.set_results([{"num": 1, "uri": "spotify:track:x"}],
                                           f"{base}-{i}")
                    server_mod.get_results(f"{base}-{i}")
            except Exception as exc:      # noqa: BLE001 -- recording, not hiding
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"raced: {errors[:3]}"
        assert len(server_mod.search_results) <= 4

    def test_the_cap_still_holds_afterwards(self, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_SEARCH_SESSIONS", 3)
        monkeypatch.setattr(server_mod, "search_results", {})
        for i in range(20):
            server_mod.set_results([], f"s{i}")
        assert len(server_mod.search_results) <= 3


# ==================== request body =======================================


class TestRequestBodyIsBounded:
    def test_a_limit_is_configured(self, server_mod):
        """CherryPy defaults to 100MB; a 38MB body was buffered and parsed."""
        assert server_mod.MAX_REQUEST_BODY_BYTES <= 1024 * 1024

    def test_the_limit_still_fits_the_largest_real_routine(self, server_mod):
        """A routine with the maximum number of steps must not be refused."""
        import json
        biggest = {
            "time": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "label": "A" * 80,
            "steps": [{"offset": i, "action": "play",
                       "uri": "spotify:playlist:" + "a" * 22, "volume": 100}
                      for i in range(server_mod.MAX_STEPS)],
        }
        assert len(json.dumps(biggest).encode()) < server_mod.MAX_REQUEST_BODY_BYTES
