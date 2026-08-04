"""Tests for proxying album art.

Sonos serves artwork from http://<speaker-ip>:1400/getaa. Handing that URL
straight to the browser only works from a machine on the LAN viewing the UI
over plain HTTP. Through the Cloudflare tunnel the page is HTTPS, so the
browser refuses a plain-HTTP subresource as mixed content and the player shows
no art at all -- which is exactly how this was noticed.

The security-shaped part is that /albumart must not become a way to make the
server fetch arbitrary URLs, so the host is remembered rather than accepted
from the caller.
"""
from unittest.mock import MagicMock, patch

import cherrypy
import pytest
import requests


SPEAKER_ART = "http://192.168.8.134:1400/getaa?s=1&u=x-sonos-spotify%3atrack"


def _image(status_code=200, content_type="image/jpeg", body=b"\xff\xd8\xff\xe0jpeg"):
    response = MagicMock(status_code=status_code, content=body)
    response.headers = {"Content-Type": content_type}
    return response


class TestRewriting:
    def test_a_speaker_url_becomes_same_origin(self, server_mod):
        assert server_mod._proxied_art(SPEAKER_ART).startswith("/albumart?")

    def test_the_query_survives(self, server_mod):
        """It identifies the track, so dropping it would serve the wrong art."""
        assert "u=x-sonos-spotify%3atrack" in server_mod._proxied_art(SPEAKER_ART)

    def test_the_origin_is_remembered(self, server_mod):
        server_mod._proxied_art(SPEAKER_ART)
        assert server_mod._art_origin == "http://192.168.8.134:1400"

    def test_no_artwork_stays_empty(self, server_mod):
        assert server_mod._proxied_art("") == ""

    def test_an_unfamiliar_path_is_passed_through(self, server_mod):
        """Only /getaa can be rebuilt; anything else is left alone rather than
        proxied into a URL this endpoint cannot reconstruct."""
        other = "http://192.168.8.134:1400/something/else.jpg"
        assert server_mod._proxied_art(other) == other

    def test_a_spotify_cdn_url_is_left_alone(self, server_mod):
        """Search results carry https CDN art, which the browser can load."""
        cdn = "https://i.scdn.co/image/ab67616d0000b273"
        assert server_mod._proxied_art(cdn) == cdn


class TestServingIt:
    def test_it_fetches_from_the_remembered_speaker(self, dj, server_mod, monkeypatch):
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string", "s=1&u=abc", raising=False)
        with patch.object(server_mod.requests, "get", return_value=_image()) as get:
            body = dj.albumart()
        assert get.call_args[0][0] == "http://192.168.8.134:1400/getaa?s=1&u=abc"
        assert body.startswith(b"\xff\xd8")

    def test_the_content_type_is_passed_through(self, dj, server_mod, monkeypatch):
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string", "", raising=False)
        with patch.object(server_mod.requests, "get",
                          return_value=_image(content_type="image/png")):
            dj.albumart()
        assert cherrypy.response.headers["Content-Type"] == "image/png"

    def test_it_is_cacheable(self, dj, server_mod, monkeypatch):
        """The URL changes with the track, and a push now arrives on every
        volume nudge -- without caching each one would refetch the image."""
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string", "", raising=False)
        with patch.object(server_mod.requests, "get", return_value=_image()):
            dj.albumart()
        assert "max-age" in cherrypy.response.headers["Cache-Control"]

    def test_before_any_track_is_known_it_404s(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(cherrypy.request, "query_string", "", raising=False)
        with pytest.raises(cherrypy.HTTPError) as excinfo:
            dj.albumart()
        assert excinfo.value.status == 404

    def test_an_unreachable_speaker_is_a_502(self, dj, server_mod, monkeypatch):
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string", "", raising=False)
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.Timeout("slow")):
            with pytest.raises(cherrypy.HTTPError) as excinfo:
                dj.albumart()
        assert excinfo.value.status == 502

    def test_a_missing_image_is_a_502_not_an_empty_body(self, dj, server_mod, monkeypatch):
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string", "", raising=False)
        with patch.object(server_mod.requests, "get", return_value=_image(status_code=404)):
            with pytest.raises(cherrypy.HTTPError) as excinfo:
                dj.albumart()
        assert excinfo.value.status == 502


class TestItCannotBecomeAnOpenProxy:
    def test_the_caller_cannot_choose_the_host(self, dj, server_mod, monkeypatch):
        """An endpoint that fetched a caller-supplied URL would let anyone with
        a session make the server reach arbitrary hosts. Only the query string
        is honoured; host and path come from what Sonos reported."""
        server_mod._proxied_art(SPEAKER_ART)
        monkeypatch.setattr(cherrypy.request, "query_string",
                            "u=http://169.254.169.254/latest/meta-data/", raising=False)
        with patch.object(server_mod.requests, "get", return_value=_image()) as get:
            dj.albumart()
        assert get.call_args[0][0].startswith("http://192.168.8.134:1400/getaa?")

    def test_it_is_not_public(self, server_mod):
        assert "/albumart" not in server_mod.PUBLIC_PATHS
