"""Tests for application logging.

The Claude billing failure ran for an unknown length of time behind a generic
"Sorry, I had trouble understanding that", because the only diagnostic was a
print() that reported KeyError('content'). These tests pin the properties that
make the log usable: no duplicate records, no secrets, and an actual entry on
each of the failure paths that used to be silent.
"""
import logging
import os
import re

import pytest
import requests
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOauthError

from paths import SERVER_PY


class TestLoggerConfiguration:
    def test_has_its_own_handler(self, server_mod):
        assert server_mod.log.handlers, "logger would emit nothing"

    def test_does_not_propagate_to_root(self, server_mod):
        """cherrypy.error and cherrypy.access both have their own handlers AND
        propagate=True. If this logger propagated as well, anything configured
        on root would print our records a second time."""
        assert server_mod.log.propagate is False

    def test_does_not_call_basicconfig(self):
        """basicConfig() adds a root handler, and cherrypy's loggers propagate
        to root while also holding their own handlers -- so every access-log
        line would appear twice in the same file.

        Checked against the source rather than logging.getLogger().handlers,
        because pytest's caplog attaches its own root handler; asserting on
        that would test pytest, not this server.
        """
        with open(SERVER_PY) as f:
            lines = f.readlines()
        # Match a call, not the comment that explains why we avoid it.
        calls = [
            f"{i}: {l.strip()}" for i, l in enumerate(lines, 1)
            if re.search(r'^\s*(logging\.)?basicConfig\s*\(', l)
        ]
        assert not calls, calls

    def test_format_carries_timestamp_and_level(self, server_mod):
        fmt = server_mod.log.handlers[0].formatter._fmt
        assert "%(asctime)s" in fmt
        assert "%(levelname)" in fmt

    def test_no_print_calls_remain_in_server(self):
        with open(SERVER_PY) as f:
            source = f.read()
        # Ignore the word inside strings/comments; look for real calls.
        assert not re.search(r'^\s*print\(', source, re.MULTILINE)


class TestFailurePathsAreLogged:
    def test_claude_api_error(self, server_mod, caplog):
        import anthropic
        import httpx
        from unittest.mock import patch

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request)
        with caplog.at_level(logging.INFO, logger="dj"):
            with patch.object(server_mod, "claude") as mock_claude:
                mock_claude.messages.create.side_effect = anthropic.APIStatusError(
                    "credit balance too low", response=response, body=None
                )
                server_mod.call_claude("play jazz")

        # getMessage() applies the %-args; r.message is only populated once a
        # formatter has run, so it is not safe to read here.
        assert any("credit balance" in r.getMessage() for r in caplog.records), caplog.text
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_sonos_failure(self, dj, server_mod, caplog):
        from unittest.mock import patch

        with caplog.at_level(logging.INFO, logger="dj"):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                dj._sonos_request("pause")

        assert any(r.levelno >= logging.WARNING for r in caplog.records), caplog.text
        assert "pause" in caplog.text

    def test_spotify_failure(self, server_mod, caplog):
        @server_mod._handles_spotify_errors
        def boom():
            raise SpotifyException(429, -1, "slow down")

        with caplog.at_level(logging.INFO, logger="dj"):
            with pytest.raises(server_mod.cherrypy.HTTPError):
                boom()

        assert any(r.levelno >= logging.WARNING for r in caplog.records), caplog.text

    def test_spotify_oauth_failure(self, server_mod, caplog):
        @server_mod._handles_spotify_errors
        def boom():
            raise SpotifyOauthError("refresh failed")

        with caplog.at_level(logging.INFO, logger="dj"):
            with pytest.raises(server_mod.cherrypy.HTTPError):
                boom()

        assert any(r.levelno >= logging.ERROR for r in caplog.records), caplog.text


class TestSecretsNeverLogged:
    def test_failed_login_does_not_record_the_attempt(self, server_mod, caplog, monkeypatch):
        """A near-miss typo of the real password must not land in a file that
        is not treated as secret."""
        monkeypatch.setattr(server_mod, "UI_PASSWORD", "hunter2")
        dj = server_mod.DJServer()

        with caplog.at_level(logging.INFO, logger="dj"):
            with pytest.raises(server_mod.cherrypy.HTTPRedirect):
                dj.login(password="hunter3")

        assert "hunter3" not in caplog.text
        assert "hunter2" not in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_successful_login_does_not_record_the_token(self, server_mod, caplog, monkeypatch):
        monkeypatch.setattr(server_mod, "UI_PASSWORD", "hunter2")
        monkeypatch.setattr(server_mod, "_sessions", set())
        dj = server_mod.DJServer()

        with caplog.at_level(logging.INFO, logger="dj"):
            with pytest.raises(server_mod.cherrypy.HTTPRedirect):
                dj.login(password="hunter2")

        issued = next(iter(server_mod._sessions))
        assert issued not in caplog.text, "session token leaked into the log"
        assert "hunter2" not in caplog.text

    def test_source_never_logs_credential_variables(self):
        """Guard against a future log line interpolating a secret."""
        with open(SERVER_PY) as f:
            lines = f.readlines()

        secrets_named = ("UI_PASSWORD", "CLI_TOKEN", "ANTHROPIC_API_KEY", "client_secret")
        for i, line in enumerate(lines, 1):
            if not re.search(r'\blog\.(debug|info|warning|error|critical)\(', line):
                continue
            for name in secrets_named:
                # Presence as a truthiness check is fine; interpolation is not.
                if re.search(rf'%[sr].*\b{name}\b|\b{name}\b\s*\)', line):
                    pytest.fail(f"server.py:{i} may log {name}: {line.strip()}")


class TestLoginCannotLeakViaUrl:
    """The access log records the full request line, so a password in a query
    string ends up in cleartext in the log file (and browser history, and the
    tunnel's logs). Rejecting GET removes that path entirely."""

    def test_login_rejects_get(self, server_mod):
        import inspect
        # cherrypy.tools.allow attaches its config to the handler.
        conf = getattr(server_mod.DJServer.login, '_cp_config', {})
        methods = conf.get('tools.allow.methods')
        assert methods == ['POST'], (
            f"login must be POST-only, got {methods!r}"
        )

    def test_login_form_posts(self):
        import os
        with open(SERVER_PY) as f:
            source = f.read()
        assert 'method="POST" action="/login"' in source
