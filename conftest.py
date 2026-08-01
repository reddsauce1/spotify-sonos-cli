"""Conftest: patches module-level Spotify/config so server.py can be imported in tests."""
import sys
import json
from unittest.mock import patch, MagicMock

import pytest


# Fake config that server.py expects
_FAKE_CONFIG = {
    "client_id": "fake_client_id",
    "client_secret": "fake_client_secret",
    "sonos_room": "TestRoom",
    "anthropic_api_key": "fake_key",
    "ui_password": "",
}


@pytest.fixture(autouse=True)
def _patch_server_imports(monkeypatch):
    """Ensure server module can be imported without real credentials or network."""
    # If server is already imported, we just need the fixture for safety.
    # The real patching happens at module level below.
    pass


def _import_server():
    """Import server.py with all external dependencies mocked."""
    # Remove cached module if present so patches take effect
    sys.modules.pop("server", None)

    fake_open = MagicMock()
    fake_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: json.dumps(_FAKE_CONFIG))
    fake_open.return_value.__exit__ = MagicMock(return_value=False)

    with patch("builtins.open", fake_open):
        with patch("json.load", return_value=_FAKE_CONFIG):
            with patch("spotipy.Spotify"):
                with patch("spotipy.oauth2.SpotifyOAuth"):
                    import server
    return server


# Import once at conftest load time so all tests share the module
server_module = _import_server()


@pytest.fixture
def dj():
    """Return a fresh DJServer instance for testing."""
    return server_module.DJServer()


@pytest.fixture
def server_mod():
    """Return the server module for accessing module-level functions/constants."""
    return server_module
