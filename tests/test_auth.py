"""Tests for authentication.

The rest of the suite runs with ui_password="" which short-circuits
_is_authenticated() to True, so none of it exercises the auth path. These
tests set a password explicitly and drive the helpers directly.

The parameter validators moved to test_input_validation.py; they were only
here because auth was the first feature that needed them.
"""
import types

import pytest


class _Cookie:
    """Minimal stand-in for the cookie objects CherryPy puts on the request."""

    def __init__(self, value):
        self.value = value


def _request(headers=None, cookies=None, path="/nowplaying"):
    return types.SimpleNamespace(
        headers=headers or {},
        cookie={k: _Cookie(v) for k, v in (cookies or {}).items()},
        path_info=path,
        handler=lambda: "handler ran",
    )


@pytest.fixture
def auth_on(server_mod, monkeypatch):
    """Enable auth with a known password and CLI token."""
    monkeypatch.setattr(server_mod, "UI_PASSWORD", "hunter2")
    monkeypatch.setattr(server_mod, "CLI_TOKEN", "c" * 64)
    monkeypatch.setattr(server_mod, "_sessions", set())
    return server_mod


# ======================== _is_authenticated ========================


class TestIsAuthenticated:
    def test_disabled_when_no_password_set(self, server_mod, monkeypatch):
        """An unset ui_password means the server runs open, as documented."""
        monkeypatch.setattr(server_mod, "UI_PASSWORD", "")
        monkeypatch.setattr(server_mod.cherrypy, "request", _request())
        assert server_mod._is_authenticated() is True

    def test_no_credentials_denied(self, auth_on, monkeypatch):
        monkeypatch.setattr(auth_on.cherrypy, "request", _request())
        assert auth_on._is_authenticated() is False

    def test_valid_cli_token_accepted(self, auth_on, monkeypatch):
        req = _request(headers={"X-DJ-Token": "c" * 64})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is True

    def test_wrong_cli_token_denied(self, auth_on, monkeypatch):
        req = _request(headers={"X-DJ-Token": "d" * 64})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is False

    def test_truncated_cli_token_denied(self, auth_on, monkeypatch):
        """A prefix of the real token must not pass."""
        req = _request(headers={"X-DJ-Token": "c" * 32})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is False

    def test_token_header_ignored_when_no_cli_token_configured(self, server_mod, monkeypatch):
        """With cli_token unset, any supplied header must not authenticate."""
        monkeypatch.setattr(server_mod, "UI_PASSWORD", "hunter2")
        monkeypatch.setattr(server_mod, "CLI_TOKEN", "")
        monkeypatch.setattr(server_mod, "_sessions", set())
        req = _request(headers={"X-DJ-Token": ""})
        monkeypatch.setattr(server_mod.cherrypy, "request", req)
        assert server_mod._is_authenticated() is False

    def test_valid_session_cookie_accepted(self, auth_on, monkeypatch):
        auth_on._sessions.add("sessiontoken")
        req = _request(cookies={"dj_auth": "sessiontoken"})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is True

    def test_forged_session_cookie_denied(self, auth_on, monkeypatch):
        req = _request(cookies={"dj_auth": "not-a-real-token"})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is False

    def test_password_as_cookie_denied(self, auth_on, monkeypatch):
        """Regression: the cookie used to be the password itself."""
        req = _request(cookies={"dj_auth": "hunter2"})
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        assert auth_on._is_authenticated() is False


# ======================== _check_auth ========================


class TestCheckAuth:
    @pytest.mark.parametrize("path", ["", "/index", "/ui", "/login"])
    def test_public_paths_pass_without_credentials(self, auth_on, monkeypatch, path):
        req = _request(path=path)
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        auth_on._check_auth()
        # Handler left intact means the request proceeds.
        assert req.handler is not None

    def test_protected_path_gets_401_and_handler_suppressed(self, auth_on, monkeypatch):
        req = _request(path="/clearqueue")
        resp = types.SimpleNamespace(status=200, headers={}, body=None)
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        monkeypatch.setattr(auth_on.cherrypy, "response", resp)

        auth_on._check_auth()

        assert resp.status == 401
        assert resp.headers["Content-Type"] == "application/json"
        assert b"Authentication required" in resp.body
        # Without this the real handler would still run and act on Sonos.
        assert req.handler is None

    def test_unknown_path_is_protected_by_default(self, auth_on, monkeypatch):
        """A new endpoint must be denied unless added to PUBLIC_PATHS."""
        req = _request(path="/some_future_endpoint")
        resp = types.SimpleNamespace(status=200, headers={}, body=None)
        monkeypatch.setattr(auth_on.cherrypy, "request", req)
        monkeypatch.setattr(auth_on.cherrypy, "response", resp)

        auth_on._check_auth()

        assert resp.status == 401


# ======================== input validation ========================


class TestGetResultItem:
    def test_returns_selected_item(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "A", "uri": "spotify:track:aaa"},
             {"num": 2, "name": "B", "uri": "spotify:track:bbb"}], "global"
        )
        assert dj._get_result_item(2)["name"] == "B"

    def test_no_results_is_400(self, dj, server_mod):
        server_mod.set_results([], "global")
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj._get_result_item(1)
        assert exc.value.status == 400

    @pytest.mark.parametrize("num", [0, 3, -1, "abc"])
    def test_bad_selection_is_400(self, dj, server_mod, num):
        server_mod.set_results(
            [{"num": 1, "name": "A", "uri": "spotify:track:aaa"},
             {"num": 2, "name": "B", "uri": "spotify:track:bbb"}], "global"
        )
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj._get_result_item(num)
        assert exc.value.status == 400
