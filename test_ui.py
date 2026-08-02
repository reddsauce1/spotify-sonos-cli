"""Tests for the extracted web UI and its HTML escaping.

Track, album, artist and playlist names come from Spotify and from the Sonos
queue. Anyone who can get a track into either -- a shared playlist, a guest
queueing a song -- controls text the UI used to concatenate straight into
innerHTML.
"""
import json
import os
import re
import shutil
import subprocess

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, 'static', 'index.html')


@pytest.fixture(scope="module")
def markup():
    with open(INDEX) as f:
        return f.read()


class TestExtraction:
    def test_index_html_exists(self, server_mod):
        assert os.path.isfile(server_mod.UI_INDEX_PATH)

    def test_server_points_at_it(self, server_mod):
        assert server_mod.UI_INDEX_PATH == INDEX
        assert server_mod.STATIC_DIR.endswith("static")

    def test_markup_is_a_complete_document(self, markup):
        assert markup.startswith("<!DOCTYPE html>")
        assert markup.rstrip().endswith("</html>")

    def test_no_ui_markup_left_in_server_py(self):
        """The point of the extraction: server.py should no longer carry the
        app's markup. The small login page stays inline deliberately, so it
        still renders if static/ is missing."""
        with open(os.path.join(HERE, 'server.py')) as f:
            source = f.read()
        assert "function escapeHtml" not in source
        assert "refreshNowPlaying" not in source
        assert source.count("<!DOCTYPE html>") == 1  # the login page only


class TestEscapingIsApplied:
    def test_helper_is_defined(self, markup):
        assert "function escapeHtml(" in markup

    def test_ampersand_is_escaped_first(self, markup):
        """If & is not replaced first, the escapes for < and " get double-
        encoded and the output is wrong."""
        body = markup.split("function escapeHtml(", 1)[1]
        order = [m.group(1) for m in re.finditer(r"\.replace\(/(.)/g", body[:600])]
        assert order[0] == "&", order

    @pytest.mark.parametrize("field", [
        "item.name", "item.artist", "item.title", "item.album",
        "data.title", "data.artist", "data.album", "data.artwork",
    ])
    def test_every_dynamic_field_is_escaped(self, markup, field):
        """No occurrence of these may be concatenated into markup raw."""
        pattern = re.compile(r"\+\s*" + re.escape(field) + r"\b")
        for line in markup.splitlines():
            if pattern.search(line) and "escapeHtml" not in line:
                pytest.fail(f"unescaped {field} in: {line.strip()}")

    def test_artwork_url_is_escaped_inside_the_src_attribute(self, markup):
        """It lands in an attribute, so an unescaped quote breaks out of
        src="" and can add an event handler."""
        line = next(l for l in markup.splitlines() if "<img src=" in l)
        assert "escapeHtml(data.artwork)" in line


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestEscapingBehaviour:
    """Run the real function rather than trusting a grep."""

    @staticmethod
    def _run(payloads):
        with open(INDEX) as f:
            markup = f.read()
        start = markup.index("function escapeHtml(")
        end = markup.index("\n        }", start) + len("\n        }")
        fn = markup[start:end]
        script = fn + "\nconsole.log(JSON.stringify(" + json.dumps(payloads) + ".map(escapeHtml)));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    @pytest.mark.parametrize("payload", [
        "<img src=x onerror=alert(1)>",
        '"><script>alert(1)</script>',
        "'; alert(1); //",
        "<svg/onload=alert(1)>",
        '" onmouseover="alert(1)',
    ])
    def test_payload_has_no_active_characters_left(self, payload):
        escaped, = self._run([payload])
        for ch in "<>\"'":
            assert ch not in escaped, f"{ch!r} survived in {escaped!r}"

    def test_ampersand_encoded_once(self):
        """AT&T must render as AT&T, not AT&amp;T, in the browser."""
        escaped, = self._run(["AT&T"])
        assert escaped == "AT&amp;T"

    def test_null_and_undefined_do_not_print_as_text(self):
        """Concatenating a missing field used to render the word 'undefined'."""
        assert self._run([None]) == [""]

    def test_ordinary_names_are_untouched(self):
        assert self._run(["Miles Davis"]) == ["Miles Davis"]


class TestStillAuthGated:
    def test_ui_stays_public_so_it_can_serve_the_login_form(self, server_mod):
        assert "/ui" in server_mod.PUBLIC_PATHS

    def test_static_dir_is_not_mounted_unauthenticated(self, server_mod):
        """Only /, /index, /ui and /login are reachable without credentials --
        adding a static mount must not quietly widen that."""
        assert server_mod.PUBLIC_PATHS == {'', '/index', '/ui', '/login'}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestJavaScriptIsValid:
    """The UI is ~340 lines of JS in a script block. A syntax error there
    breaks the entire page with nothing in the server log to show for it."""

    def test_script_block_parses(self, markup):
        script = markup.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        out = subprocess.run(["node", "--check", "-"], input=script,
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr

    def test_every_handler_referenced_by_onclick_exists(self, markup):
        """An onclick naming a function that was renamed fails silently in the
        browser -- the button just does nothing."""
        called = set(re.findall(r'onclick="(\w+)\(', markup))
        called |= set(re.findall(r"onclick=\\'(\w+)\(", markup))
        defined = set(re.findall(r'function (\w+)\(', markup))
        missing = called - defined
        assert not missing, f"onclick references undefined function(s): {missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestWeeklyCalendar:
    """The calendar is rendered client-side from /schedules."""

    @staticmethod
    def _render(schedules, today_index=0):
        with open(INDEX) as f:
            markup = f.read()
        def fn(name):
            start = markup.index("function " + name + "(")
            return markup[start:markup.index("\n        }", start) + len("\n        }")]

        script = (
            "const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];\n"
            "const ACTION_ICON = {play:'>',pause:'||',resume:'>',skip:'>|',"
            "previous:'|<',volume:'V',clearqueue:'X'};\n"
            + fn("escapeHtml") + "\n"
            "const _out = {};\n"
            "const document = {getElementById: () => ({ set innerHTML(v) { _out.html = v; },"
            " get innerHTML() { return _out.html; } })};\n"
            "const fetch = () => Promise.resolve({json: () => Promise.resolve("
            + json.dumps({"schedules": schedules}) + ")});\n"
            "const Date = class { getDay() { return " + str((today_index + 1) % 7) + "; } };\n"
            + fn("renderCalendar") + "\n"
            "renderCalendar();\n"
            "setTimeout(() => console.log(_out.html), 0);\n"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout

    ROUTINE = {
        "id": "a", "time": "07:00", "days": [0, 1, 2, 3, 4], "label": "Wake-up",
        "enabled": True, "steps": [{"offset": 0, "action": "play", "uri": "spotify:playlist:x"}],
    }

    def test_marks_the_days_a_routine_runs(self):
        html = self._render([self.ROUTINE])
        rows = html.split("<tr>")[2]           # the 07:00 row
        cells = rows.split("<td")[2:]          # skip the time cell
        assert sum('class="hit"' in c for c in cells) == 5
        assert sum('class="miss"' in c for c in cells) == 2

    def test_empty_days_fills_the_whole_week(self):
        html = self._render([{**self.ROUTINE, "days": []}])
        assert html.count('class="hit"') == 7

    def test_disabled_routines_are_hidden(self):
        assert "Nothing enabled" in self._render([{**self.ROUTINE, "enabled": False}])

    def test_no_schedules_says_so(self):
        assert "Nothing enabled" in self._render([])

    def test_times_are_rows_in_order(self):
        html = self._render([
            {**self.ROUTINE, "time": "22:30", "id": "b"},
            self.ROUTINE,
        ])
        assert html.index("07:00") < html.index("22:30")

    def test_today_column_is_marked(self):
        """Monday-based: the scheduler uses 0=Mon, JS getDay() is 0=Sun."""
        html = self._render([self.ROUTINE], today_index=2)   # Wednesday
        headers = html.split("</tr>")[0]
        assert 'class="today">Wed' in headers

    def test_label_is_escaped_in_the_cell(self):
        html = self._render([{**self.ROUTINE, "label": '<img src=x onerror=alert(1)>'}])
        assert "<img" not in html.split("<table")[1]
