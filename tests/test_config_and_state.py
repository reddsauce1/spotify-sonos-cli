"""Tests for DEFAULTS/config overrides, config validation, and the
search-result cache -- its expiry and its behaviour under concurrency.

The expiry matters because every distinct session_id the web UI sends created
a permanent dict entry: a public URL plus a few months of party guests is an
unbounded dict in a process that never restarts on its own.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

import pytest

from paths import PROJECT_ROOT as HERE




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

    VALID = {"client_id": "a", "client_secret": "b",
             "ui_password": "hunter2", "cli_token": "t" * 64}

    def test_valid_config_imports(self):
        out = self._import_with_config(dict(self.VALID))
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

    def test_truly_optional_keys_may_be_absent(self):
        """anthropic_api_key and sonos_room are still optional by design --
        one disables /chat, the other has a default."""
        out = self._import_with_config(dict(self.VALID))
        assert "IMPORTED" in out.stdout, out.stderr

    def test_wrong_type_on_optional_key_is_rejected(self):
        out = self._import_with_config({**self.VALID, "ui_password": 1234})
        assert out.returncode != 0
        assert "ui_password" in out.stderr


class TestItRefusesToRunOpenByAccident:
    """This reverses an earlier decision. ui_password and cli_token used to be
    optional, and an empty password only logged a warning -- so forgetting a
    key and choosing to run unauthenticated produced the same result, on
    something published to the internet through a Cloudflare tunnel. Only one
    of those is a decision, so now it has to be written down."""

    # staticmethod(), or the borrowed helper picks up `self` as its config.
    _import_with_config = staticmethod(TestConfigValidation._import_with_config)

    @pytest.mark.parametrize("missing", ["ui_password", "cli_token"])
    def test_a_missing_credential_stops_the_boot(self, missing):
        cfg = dict(TestConfigValidation.VALID)
        del cfg[missing]
        out = self._import_with_config(cfg)
        assert out.returncode != 0, out.stdout
        assert missing in out.stderr

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_an_empty_credential_stops_the_boot(self, empty):
        out = self._import_with_config({**TestConfigValidation.VALID, "ui_password": empty})
        assert out.returncode != 0, out.stdout

    def test_the_error_says_how_to_opt_out(self):
        """A refusal that does not say what to do next just gets worked around
        by deleting the check."""
        cfg = dict(TestConfigValidation.VALID)
        del cfg["ui_password"]
        out = self._import_with_config(cfg)
        assert "allow_open_access" in out.stderr

    def test_open_access_is_allowed_when_asked_for(self):
        out = self._import_with_config(
            {"client_id": "a", "client_secret": "b", "allow_open_access": True})
        assert "IMPORTED" in out.stdout, out.stderr

    def test_the_flag_alone_does_not_excuse_real_credentials(self):
        """allow_open_access covers authentication, not Spotify."""
        out = self._import_with_config({"allow_open_access": True})
        assert out.returncode != 0
        assert "client_id" in out.stderr


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


# The same cache as TestSearchResultExpiry above, under concurrency.
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


# The suite lives in tests/ and reads real files out of the repo root, so how
# it finds them is itself worth a test.


class TestTheSuiteIsLocationIndependent:
    def test_project_root_is_the_repo_not_the_tests_folder(self):
        import paths
        assert os.path.isfile(os.path.join(paths.PROJECT_ROOT, "server.py"))
        assert not paths.PROJECT_ROOT.rstrip("/").endswith("tests")

    @pytest.mark.parametrize("name", ["INDEX_HTML", "SERVER_PY", "README_MD",
                                      "QUEUEEDIT_JS"])
    def test_every_declared_path_exists(self, name):
        import paths
        assert os.path.isfile(getattr(paths, name)), name

    def test_no_test_reads_a_file_by_a_cwd_relative_path(self):
        """Those only worked while the suite sat in the repo root and pytest
        was invoked from there; from inside tests/ they raise
        FileNotFoundError at collection time."""
        import glob
        import re
        offenders = []
        for path in glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")):
            for line in open(path):
                if re.search(r'open\(\s*[\'"](?!/)[\w./-]+[\'"]', line):
                    offenders.append(f"{os.path.basename(path)}: {line.strip()}")
        assert not offenders, "cwd-relative reads: " + "; ".join(offenders)

    def test_pytest_config_puts_the_repo_root_on_the_path(self):
        """Without this the bare `pytest` command cannot import server at all.
        It is easy to believe it is unnecessary, because `python -m pytest`
        adds the working directory itself and so passes either way."""
        import paths
        ini = open(os.path.join(paths.PROJECT_ROOT, "pytest.ini")).read()
        assert re.search(r"^pythonpath\s*=\s*\.", ini, re.M)
