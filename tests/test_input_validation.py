"""Tests for the validators every endpoint runs its parameters through.

These were scattered: the uri/int/volume ones sat in test_auth.py because
authentication happened to be the feature that first needed them, and the
text ones sat in a file named after the review that added them. Both are
about the same thing -- refusing bad input at the edge, before it reaches
Sonos or Spotify -- so they live together.

Two rules run through all of them. Anything interpolated into a Sonos path is
validated as the type it claims to be rather than passed through as text, and
a caller's mistake answers 400 rather than being blamed on the upstream as a
502.
"""
import re
from unittest.mock import patch

import pytest


class TestValidateUri:
    @pytest.mark.parametrize("uri", [
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "spotify:album:aaa",
        "spotify:playlist:ZZZ999",
    ])
    def test_accepts_spotify_uris(self, server_mod, uri):
        assert server_mod._validate_uri(uri) == uri

    @pytest.mark.parametrize("uri", [
        "../../Bedroom/pause",
        "../pause",
        "spotify:track:../../pause",
        "http://evil.example/x",
        "notaspotifyuri",
        "spotify:track:has spaces",
        "",
        None,
    ])
    def test_rejects_anything_else(self, server_mod, uri):
        """These would otherwise be interpolated into the Sonos request path."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_uri(uri)
        assert exc.value.status == 400


class TestValidateInt:
    def test_accepts_in_range(self, server_mod):
        assert server_mod._validate_int("5", "num", 1, 10) == 5

    @pytest.mark.parametrize("value", ["abc", "", None, "1.5", "0x10"])
    def test_rejects_non_numeric(self, server_mod, value):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_int(value, "num", 1, 10)
        assert exc.value.status == 400

    @pytest.mark.parametrize("value", [0, 11, -1, 9999])
    def test_rejects_out_of_range(self, server_mod, value):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_int(value, "num", 1, 10)
        assert exc.value.status == 400


class TestValidateVolumeChange:
    @pytest.mark.parametrize("given,expected", [
        ("+10", "+10"),
        ("10", "+10"),
        ("-10", "-10"),
        (0, "+0"),
    ])
    def test_normalises_sign(self, server_mod, given, expected):
        """Sonos expects an explicit sign on relative changes."""
        assert server_mod._validate_volume_change(given) == expected

    @pytest.mark.parametrize("value", ["abc", "../pause", 999, -999])
    def test_rejects_bad_values(self, server_mod, value):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_volume_change(value)
        assert exc.value.status == 400


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
