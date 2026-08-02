"""Tests for album results and expanding them in place.

One query runs both an album and a track search, so Spotify's field filters
(album:, artist:, year:) keep working with no type selector in the way.
"""
import os
from unittest.mock import patch

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, 'static', 'index.html')


@pytest.fixture(scope="module")
def markup():
    with open(INDEX) as f:
        return f.read()


ALBUM_SEARCH = {"albums": {"items": [{
    "name": "Rumours", "uri": "spotify:album:abc", "total_tracks": 11,
    "release_date": "1977-02-04",
    "artists": [{"name": "Fleetwood Mac"}],
    "images": [{"url": "http://img/x.jpg"}],
}]}}

ALBUM_DETAIL = {
    "name": "Rumours", "uri": "spotify:album:abc",
    "release_date": "1977-02-04",
    "artists": [{"name": "Fleetwood Mac"}],
    "images": [{"url": "http://img/x.jpg"}],
    "tracks": {"items": [
        {"name": "Second Hand News", "uri": "spotify:track:1",
         "artists": [{"name": "Fleetwood Mac"}], "duration_ms": 173000},
        {"name": "Dreams", "uri": "spotify:track:2",
         "artists": [{"name": "Fleetwood Mac"}], "duration_ms": 257000},
    ]},
}


class TestAlbumSearch:
    def test_album_results_are_returned(self, dj, server_mod):
        """They used to come back empty: _do_search only built output for
        type == 'track', so every other type silently returned nothing."""
        with patch.object(server_mod, "sp") as sp:
            sp.search.return_value = ALBUM_SEARCH
            result = dj._do_search(q="Rumours", type="album")
        assert len(result["results"]) == 1

    def test_album_result_carries_what_the_row_needs(self, dj, server_mod):
        with patch.object(server_mod, "sp") as sp:
            sp.search.return_value = ALBUM_SEARCH
            item = dj._do_search(q="Rumours", type="album")["results"][0]
        assert item["name"] == "Rumours"
        assert item["artist"] == "Fleetwood Mac"
        assert item["tracks"] == 11
        assert item["year"] == "1977"
        assert item["uri"] == "spotify:album:abc"
        assert item["artwork"] == "http://img/x.jpg"

    def test_album_without_artwork_does_not_crash(self, dj, server_mod):
        album = {"albums": {"items": [{
            "name": "X", "uri": "spotify:album:z", "total_tracks": 1,
            "artists": [{"name": "Y"}], "release_date": "", "images": [],
        }]}}
        with patch.object(server_mod, "sp") as sp:
            sp.search.return_value = album
            assert dj._do_search(q="x", type="album")["results"][0]["artwork"] is None

    def test_playlist_search_skips_null_entries(self, dj, server_mod):
        """Spotify returns nulls in playlist results; indexing them crashes."""
        payload = {"playlists": {"items": [
            None,
            {"name": "P", "uri": "spotify:playlist:p",
             "owner": {"display_name": "me"}, "tracks": {"total": 3}},
        ]}}
        with patch.object(server_mod, "sp") as sp:
            sp.search.return_value = payload
            result = dj._do_search(q="p", type="playlist")
        assert len(result["results"]) == 1
        assert result["results"][0]["name"] == "P"

    def test_track_search_is_unchanged(self, dj, server_mod):
        payload = {"tracks": {"items": [{
            "name": "The Chain", "uri": "spotify:track:c",
            "artists": [{"name": "Fleetwood Mac"}],
            "album": {"name": "Rumours"},
        }]}}
        with patch.object(server_mod, "sp") as sp:
            sp.search.return_value = payload
            item = dj._do_search(q="x", type="track")["results"][0]
        assert item["album"] == "Rumours"


class TestAlbumExpansion:
    def test_lists_tracks_for_an_explicit_album(self, dj, server_mod):
        with patch.object(server_mod, "sp") as sp:
            sp.album.return_value = ALBUM_DETAIL
            result = dj.album_tracks(uri="spotify:album:abc")
        sp.album.assert_called_once_with("abc")
        assert [t["name"] for t in result["tracks"]] == ["Second Hand News", "Dreams"]
        assert result["album"] == "Rumours"

    def test_tracks_carry_playable_uris(self, dj, server_mod):
        with patch.object(server_mod, "sp") as sp:
            sp.album.return_value = ALBUM_DETAIL
            tracks = dj.album_tracks(uri="spotify:album:abc")["tracks"]
        assert all(t["uri"].startswith("spotify:track:") for t in tracks)

    def test_a_bad_uri_is_rejected(self, dj, server_mod):
        """This reaches the Spotify API path, so it is validated like any
        other uri."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.album_tracks(uri="../../Bedroom/pause")
        assert exc.value.status == 400

    def test_expanding_does_not_clobber_the_numbered_results(self, dj, server_mod):
        """Expanding an album in the browser must not change what
        'play number 3' means for a CLI session running alongside."""
        server_mod.set_results([{"num": 1, "name": "kept", "uri": "spotify:track:k"}])
        with patch.object(server_mod, "sp") as sp:
            sp.album.return_value = ALBUM_DETAIL
            dj.album_tracks(uri="spotify:album:abc")
        assert server_mod.get_results()[0]["name"] == "kept"

    def test_nowplaying_mode_still_works(self, dj, server_mod):
        assert "error" in dj.album_tracks(based_on="something-else")


class TestUi:
    def test_one_query_runs_both_searches(self, markup):
        body = markup.split("function doSearch(", 1)[1].split("\n  }", 1)[0]
        assert "type=album" in body
        assert "type=track" in body

    def test_no_type_selector_was_added(self, markup):
        """The field filters keep working; a selector would be in the way."""
        assert 'id="search-type"' not in markup

    def test_album_rows_expand(self, markup):
        assert "function toggleAlbum(" in markup
        assert 'class="alb-tracks"' in markup

    def test_expanded_tracks_are_cached(self, markup):
        """Collapsing and reopening should not hit Spotify again.

        Asserts the cache is *consulted*, not merely populated: filling a
        cache nobody reads leaves the refetch in place while the name still
        appears in the source."""
        body = markup.split("function toggleAlbum(", 1)[1].split("\n  }", 1)[0]
        assert "ALBUM_CACHE[uri]" in body.split("fetch(", 1)[0], \
            "the cache is not checked before fetching"
        assert "ALBUM_CACHE[uri] =" in body, "nothing is ever stored"

    def test_album_row_can_play_or_queue_the_whole_album(self, markup):
        row = markup.split("function renderAlbumRow(", 1)[1].split("\n  }", 1)[0]
        assert "playUri(" in row
        assert "queueUri(" in row

    def test_album_fields_are_escaped(self, markup):
        """Only lines that build markup matter. Deriving a DOM id with
        .replace(/[^a-zA-Z0-9]/g, '') is safe by construction -- everything
        dangerous is stripped -- and never reaches innerHTML."""
        row = markup.split("function renderAlbumRow(", 1)[1].split("\n  }", 1)[0]
        for line in row.splitlines():
            builds_markup = "'<" in line or "'>" in line or '">' in line
            if not builds_markup or "escapeHtml" in line:
                continue
            for field in ("a.name", "a.artist", "a.uri", "a.artwork", "a.year", "a.tracks"):
                if field in line:
                    pytest.fail(f"unescaped {field}: {line.strip()}")

    def test_the_dom_id_derivation_strips_everything_unsafe(self, markup):
        row = markup.split("function renderAlbumRow(", 1)[1].split("\n  }", 1)[0]
        assert "a.uri.replace(/[^a-zA-Z0-9]/g, '')" in row
