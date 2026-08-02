"""Every player action refreshes the queue view.

play, next and queue all mutate the Sonos queue. That is easy to miss for
play: node-sonos-http-api's spotify "now" action does not replace the queue,
it inserts the track at trackNo + 1, seeks to it and plays -- so the list
changes and every position after the insert shifts by one.

Before this only next and queue refreshed, and only for track rows. The + on
an album, playlist or station row went through queueUri, which refreshed
nothing: it showed "Queued" and left the Queue tab displaying the old list,
so adding something looked like it had silently done nothing.
"""
import re

import pytest


@pytest.fixture(scope="module")
def markup():
    with open("static/index.html") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def js(markup):
    return markup.split("<script>", 1)[1].split("</script>", 1)[0]


def body_of(js, name):
    match = re.search(r"\n  function %s\(.*?(?=\n  (?:function|const|let|//)|\Z)"
                      % re.escape(name), js, re.S)
    assert match, "no function " + name
    return match.group(0)


class TestEveryActionRefreshesTheQueue:
    def test_the_shared_implementation_reloads_the_queue(self, js):
        assert "loadQueue();" in body_of(js, "trackAction")

    def test_it_reloads_for_play_too(self, js):
        """The old code had `if (action !== 'play') loadQueue()`, on the
        assumption that play replaces the queue rather than inserting."""
        assert "action !== 'play'" not in js

    def test_it_also_refreshes_what_is_playing(self, js):
        assert "refreshNowPlaying();" in body_of(js, "trackAction")

    @pytest.mark.parametrize("action", ["play", "next", "queue"])
    def test_each_action_has_a_toast(self, js, action):
        assert re.search(r"ACTION_TOAST = \{[^}]*%s:" % action, js)


class TestAllThreeEntryPointsShareIt:
    """A row's + must behave the same whether it is a track, an album, a
    playlist or a station."""

    @pytest.mark.parametrize("name,action", [("playUri", "play"),
                                             ("queueUri", "queue")])
    def test_the_wrapper_delegates(self, js, name, action):
        assert re.search(r"function %s\(uri\) \{ return trackAction\(uri, '%s'\); \}"
                         % (name, action), js)

    @pytest.mark.parametrize("name", ["playUri", "queueUri"])
    def test_the_wrapper_does_not_call_fetch_itself(self, js, name):
        """A second copy of the request is how the two drifted apart."""
        assert "fetch(" not in body_of(js, name)

    def test_only_one_place_builds_these_requests(self, js):
        """Otherwise a later fix lands in one path and not the others."""
        assert len(re.findall(r"fetch\('/' \+ action \+ '\?uri='", js)) == 1
        assert "fetch('/play?uri='" not in js
        assert "fetch('/queue?uri='" not in js


class TestTheRowsStillCallSomething:
    """Guards against a rename that leaves onclick handlers pointing at
    nothing -- the buttons are built as HTML strings, so a broken name is not
    a syntax error, it is a silent no-op at click time."""

    ROW_BUILDERS = ["renderAlbumRow", "trackRow", "showPlaylists", "renderStations"]

    @pytest.mark.parametrize("builder", ROW_BUILDERS)
    def test_the_row_has_a_play_and_an_add_button(self, js, builder):
        body = body_of(js, builder)
        assert re.search(r"(playUri|trackAction)\(", body), builder
        assert re.search(r"(queueUri|trackAction)\(", body), builder

    def test_every_handler_named_in_markup_exists(self, markup, js):
        defined = set(re.findall(r"\bfunction\s+(\w+)\s*\(", js))
        defined |= set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=", js))
        builtin = {"if", "for", "while", "return", "Math", "String", "Number",
                   "JSON", "Object", "Array", "encodeURIComponent",
                   "decodeURIComponent", "parseInt", "parseFloat", "alert",
                   "prompt", "confirm", "fetch", "setTimeout", "replace", "max"}

        called = set()
        for handler in re.findall(r'on\w+="([^"]+)"', markup):
            called |= set(re.findall(r"\b(\w+)\s*\(", handler))
        for handler in re.findall(r"on\w+=\\?['\"]([^'\"]*?)\\?['\"]", js):
            called |= set(re.findall(r"\b(\w+)\s*\(", handler))

        missing = sorted(c for c in called if c not in defined and c not in builtin)
        assert not missing, f"onclick handlers with no function: {missing}"


class TestFailuresAreReported:
    def test_a_server_error_becomes_a_toast(self, js):
        assert "showToast('❌ ' + data.error)" in body_of(js, "trackAction")

    def test_an_error_does_not_also_claim_success(self, js):
        body = body_of(js, "trackAction")
        assert re.search(r"if \(data\.error\) \{[^}]*return; \}", body)

    def test_an_unreachable_server_is_reported(self, js):
        """Without a catch the promise rejects silently and the button looks
        dead -- indistinguishable from the bug this fixes."""
        assert ".catch(" in body_of(js, "trackAction")
