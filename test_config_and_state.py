"""Tests for DEFAULTS/config overrides, config validation, and search-result
expiry.

The expiry matters because every distinct session_id the web UI sends created
a permanent dict entry: a public URL plus a few months of party guests is an
unbounded dict in a process that never restarts on its own.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))


class TestDefaults:
    def test_every_default_is_reachable_through_setting(self, server_mod):
        for name in server_mod.DEFAULTS:
            assert server_mod._setting(name) is not None

    def test_config_value_overrides_the_default(self, server_mod, monkeypatch):
        monkeypatch.setitem(server_mod.config, "queue_display_limit", 7)
        assert server_mod._setting("queue_display_limit") == 7

    def test_absent_key_falls_back_to_default(self, server_mod, monkeypatch):
        monkeypatch.delitem(server_mod.config, "queue_display_limit", raising=False)
        assert server_mod._setting("queue_display_limit") == server_mod.DEFAULTS["queue_display_limit"]

    def test_unknown_setting_is_a_loud_keyerror(self, server_mod):
        """A typo in a constant name should fail at import, not silently
        return None and be used as a timeout."""
        with pytest.raises(KeyError):
            server_mod._setting("no_such_setting")

    @pytest.mark.parametrize("name", [
        "server_port", "claude_model", "claude_max_tokens", "search_limit",
        "queue_display_limit", "sonos_timeout", "cookie_max_age",
        "max_sessions", "search_result_ttl", "max_search_sessions",
    ])
    def test_documented_settings_are_present(self, server_mod, name):
        assert name in server_mod.DEFAULTS

    def test_no_magic_numbers_left_at_the_call_sites(self):
        """Each tunable should appear as a literal once, in DEFAULTS, and be
        referenced by name everywhere else."""
        with open(os.path.join(HERE, 'server.py')) as f:
            source = f.read()

        assert "max_tokens=512" not in source
        assert "max_tokens=CLAUDE_MAX_TOKENS" in source
        # 86400 * 7 may appear only in the DEFAULTS table.
        assert source.count("86400 * 7") == 1
        assert "['max-age'] = COOKIE_MAX_AGE" in source
        assert "'server.socket_port': 5006" not in source


class TestConfigValidation:
    """Run in a subprocess: the checks call SystemExit at import time."""

    @staticmethod
    def _import_with_config(cfg):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump(cfg, f)
            script = (
                "import sys, json, io, builtins\n"
                f"real_open = builtins.open\n"
                f"def fake_open(p, *a, **k):\n"
                f"    if str(p).endswith('config.json'): return real_open({path!r})\n"
                f"    return real_open(p, *a, **k)\n"
                "builtins.open = fake_open\n"
                "from unittest.mock import patch\n"
                "with patch('spotipy.Spotify'), patch('spotipy.oauth2.SpotifyOAuth'):\n"
                "    import server\n"
                "print('IMPORTED')\n"
            )
            return subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, cwd=HERE, timeout=60,
            )

    def test_valid_config_imports(self):
        out = self._import_with_config({"client_id": "a", "client_secret": "b"})
        assert "IMPORTED" in out.stdout, out.stderr

    @pytest.mark.parametrize("cfg", [
        {},
        {"client_id": "a"},
        {"client_id": "", "client_secret": "b"},
        {"client_id": "   ", "client_secret": "b"},
        {"client_id": "a", "client_secret": None},
    ])
    def test_missing_credentials_exit_nonzero(self, cfg):
        out = self._import_with_config(cfg)
        assert out.returncode != 0, out.stdout
        assert "config.json" in out.stderr

    def test_error_names_the_offending_key(self):
        out = self._import_with_config({"client_id": "a"})
        assert "client_secret" in out.stderr

    def test_optional_keys_may_be_absent(self):
        """anthropic_api_key, ui_password etc. are optional by design."""
        out = self._import_with_config({"client_id": "a", "client_secret": "b"})
        assert "IMPORTED" in out.stdout, out.stderr

    def test_wrong_type_on_optional_key_is_rejected(self):
        out = self._import_with_config(
            {"client_id": "a", "client_secret": "b", "ui_password": 1234}
        )
        assert out.returncode != 0
        assert "ui_password" in out.stderr


class TestSearchResultExpiry:
    @pytest.fixture(autouse=True)
    def _clean(self, server_mod):
        server_mod.search_results.clear()
        yield
        server_mod.search_results.clear()

    def test_roundtrip(self, server_mod):
        server_mod.set_results([{"num": 1}], "s1")
        assert server_mod.get_results("s1") == [{"num": 1}]

    def test_absent_session_is_empty(self, server_mod):
        assert server_mod.get_results("never-seen") == []

    def test_expired_entry_reads_as_empty_and_is_dropped(self, server_mod, monkeypatch):
        server_mod.set_results([{"num": 1}], "s1")
        # Jump past the TTL rather than sleeping.
        real = server_mod.time.monotonic
        monkeypatch.setattr(
            server_mod.time, "monotonic",
            lambda: real() + server_mod.SEARCH_RESULT_TTL + 1,
        )
        assert server_mod.get_results("s1") == []
        assert "s1" not in server_mod.search_results

    def test_fresh_entry_survives(self, server_mod):
        server_mod.set_results([{"num": 1}], "s1")
        assert server_mod.get_results("s1") == [{"num": 1}]
        assert "s1" in server_mod.search_results

    def test_stale_sessions_are_swept_on_write(self, server_mod, monkeypatch):
        server_mod.set_results([{"num": 1}], "old")
        real = server_mod.time.monotonic
        monkeypatch.setattr(
            server_mod.time, "monotonic",
            lambda: real() + server_mod.SEARCH_RESULT_TTL + 1,
        )
        server_mod.set_results([{"num": 2}], "new")
        assert "old" not in server_mod.search_results
        assert "new" in server_mod.search_results

    def test_total_sessions_are_capped(self, server_mod, monkeypatch):
        """Without a cap, a burst inside the TTL still grows without bound."""
        monkeypatch.setattr(server_mod, "MAX_SEARCH_SESSIONS", 5)
        for i in range(50):
            server_mod.set_results([{"num": i}], f"s{i}")
        assert len(server_mod.search_results) <= 5

    def test_cap_evicts_oldest_first(self, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_SEARCH_SESSIONS", 3)
        for i in range(6):
            server_mod.set_results([{"num": i}], f"s{i}")
        # The most recent writes are the ones that should still be there.
        assert "s5" in server_mod.search_results
        assert "s0" not in server_mod.search_results


class TestParseTrackId:
    """Sonos reports the track URI wrapped and percent-encoded, and also plays
    things that are not Spotify tracks at all (radio, line-in, TV)."""

    REAL = "x-sonos-spotify:spotify%3atrack%3a0z1IquwlPxxsQMaD7WIvCo?sid=12&flags=8232&sn=2"

    def test_extracts_id_from_a_real_sonos_uri(self, dj):
        assert dj._parse_track_id(self.REAL) == "0z1IquwlPxxsQMaD7WIvCo"

    def test_strips_the_query_string(self, dj):
        assert "?" not in (dj._parse_track_id(self.REAL) or "")

    def test_handles_an_already_decoded_uri(self, dj):
        assert dj._parse_track_id("spotify:track:abc123") == "abc123"

    @pytest.mark.parametrize("uri", [
        None,
        "",
        "x-rincon-mp3radio://stream.example/live",       # internet radio
        "x-sonos-htastream:RINCON_123:spdif",            # TV / line-in
        "x-sonos-spotify:spotify%3aalbum%3aabc?sid=12",  # album, not a track
    ])
    def test_non_track_sources_return_none(self, dj, uri):
        assert dj._parse_track_id(uri) is None

    @pytest.mark.parametrize("uri", [
        "x-file-cifs://nas/music/track:12345",
        "x-rincon-mp3radio://stream.example/track:abc",
    ])
    def test_non_spotify_source_containing_track_is_rejected(self, dj, uri):
        """A path from another source that happens to contain 'track:' must
        not be mistaken for a Spotify id -- it would be sent to sp.track()."""
        assert dj._parse_track_id(uri) is None

    @pytest.mark.parametrize("bad", [
        "x-sonos-spotify:spotify%3atrack%3a../../me?sid=12",
        "x-sonos-spotify:spotify%3atrack%3aabc%2f..%2fplaylists?sid=12",
        "x-sonos-spotify:spotify%3atrack%3a?sid=12",
        "x-sonos-spotify:spotify%3atrack%3aabc def?sid=12",
    ])
    def test_malformed_ids_are_rejected(self, dj, bad):
        """The result is passed straight to the Spotify API, so anything that
        is not a bare base62 id must not get through."""
        assert dj._parse_track_id(bad) is None

    def test_the_three_callers_use_the_helper(self):
        """like(), recommend() and album_tracks() each had their own copy."""
        with open(os.path.join(HERE, 'server.py')) as f:
            source = f.read()
        assert source.count("_parse_track_id(") == 4  # 1 definition + 3 callers
        assert source.count("split('track:')") == 1   # only inside the helper
