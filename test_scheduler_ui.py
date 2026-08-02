"""Tests for the rebuilt scheduler UI.

The old flow had two composers for one idea -- a form that created a routine
plus an invisible first step, and a second form inside each card for every
step after that -- no way to edit anything once created, offsets shown as
"+75m", and no indication anywhere of when a routine would next fire. The
last of those is how a wake-up alarm ran Sunday-only for weeks unnoticed.
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


@pytest.fixture(scope="module")
def css(markup):
    return markup.split("<style>", 1)[1].split("</style>", 1)[0]


class TestTheOldTwoFormFlowIsGone:
    @pytest.mark.parametrize("dead", [
        "buildDayPicker", "schedActionChanged", "addSchedule",
        "maybePasteUri", "toggleStepForm",
    ])
    def test_dead_helper_is_removed(self, markup, dead):
        assert dead not in markup

    @pytest.mark.parametrize("dead_id", [
        "sched-time", "sched-label", "sched-days", "sched-action",
        "sched-uri", "sched-volume",
    ])
    def test_the_standalone_create_form_is_gone(self, markup, dead_id):
        assert 'id="%s"' % dead_id not in markup

    def test_there_is_no_second_per_card_step_form(self, js):
        """Steps are composed in the editor now, not inline in each routine."""
        assert "addstep-" not in js
        assert "stepoff-" not in js


class TestOneComposerForCreateAndEdit:
    def test_the_editor_exists(self, markup):
        assert 'id="editor"' in markup
        assert 'id="ed-body"' in markup

    def test_creating_and_editing_use_the_same_function(self, js):
        assert "function openEditor(routine, prefill)" in js
        assert re.search(r"function editRoutine\(id\) \{.*?openEditor\(routine, null\)",
                         js, re.S)

    def test_the_title_says_which_one_you_are_doing(self, js):
        assert re.search(r"EDITOR\.id \? 'Edit routine' : 'New routine'", js)

    def test_saving_goes_to_the_one_atomic_endpoint(self, js):
        assert "postJson('/schedule_save', payload)" in js

    def test_it_no_longer_calls_the_incremental_endpoints(self, js):
        assert "/schedule_step_add" not in js
        assert "/schedule_step_delete" not in js
        assert "/schedule_add" not in js

    def test_the_id_is_only_sent_when_editing(self, js):
        """Sending id: null on a create would 400 as an unknown schedule."""
        assert "if (EDITOR.id) payload.id = EDITOR.id;" in js

    def test_routines_can_be_edited_from_the_list(self, js):
        assert "editRoutine(" in js


class TestNothingSavesUntilSave:
    def test_the_editor_holds_a_draft(self, js):
        assert re.search(r"let EDITOR = null", js)

    def test_cancel_discards_it(self, js):
        assert re.search(r"function closeEditor\(\) \{\s*EDITOR = null", js)

    def test_escape_closes_it(self, js):
        assert "e.key === 'Escape'" in js

    def test_step_edits_only_touch_the_draft(self, js):
        """Removing a step must not fire a request the way it used to."""
        body = re.search(r"function editorRemoveStep\(index\) \{.*?\n  \}", js, re.S).group(0)
        assert "fetch" not in body and "post" not in body


class TestStepsShowWallClockNotOffsets:
    def test_the_editor_edits_a_time_not_a_minute_count(self, js):
        row = re.search(r"function editorStepRow\(step, index\) \{.*?\n  \}", js, re.S).group(0)
        assert 'type="time"' in row
        assert "placeholder=\"+min\"" not in row

    def test_the_list_renders_the_server_computed_time(self, js):
        routine = re.search(r"function renderRoutine\(s\) \{.*?\n  \}", js, re.S).group(0)
        assert "step.at" in routine
        assert "'m'" not in routine, "no leftover +75m rendering"

    def test_times_convert_back_to_offsets_on_save(self, js):
        assert "offset: ((toMinutes(step.at) - trigger) % 1440 + 1440) % 1440" in js

    def test_a_step_before_the_trigger_is_marked_next_day(self, js):
        assert "const wraps = toMinutes(step.at) < toMinutes(EDITOR.time);" in js
        assert '<span class="tag">next day</span>' in js

    def test_moving_the_start_time_moves_the_whole_routine(self, js):
        """Otherwise shifting a wake-up by 30 minutes means editing every step."""
        body = re.search(r"function editorTimeChanged\(value\) \{.*?\n  \}", js, re.S).group(0)
        assert "const delta = toMinutes(value) - toMinutes(EDITOR.time);" in body
        assert "step.at = fromMinutes(toMinutes(step.at) + delta)" in body


class TestNextRunIsAlwaysVisible:
    """The missing feedback that let a Sunday-only alarm go unnoticed."""

    def test_the_list_shows_it(self, js):
        routine = re.search(r"function renderRoutine\(s\) \{.*?\n  \}", js, re.S).group(0)
        assert "s.next_run" in routine
        assert "describeWhen" in routine

    def test_the_draft_shows_it_before_saving(self, js):
        assert "nextRunPreview(EDITOR.time, EDITOR.days)" in js

    def test_no_days_selected_is_called_out_rather_than_left_blank(self, js):
        assert "Never runs — pick at least one day" in js
        assert "never runs — no days selected" in js

    def test_the_preview_uses_the_servers_monday_first_convention(self, js):
        """JS getDay() is Sunday-first; the scheduler is Monday-first. Getting
        this backwards is exactly the class of bug being fixed."""
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "(when.getDay() + 6) % 7" in body

    def test_the_preview_skips_a_time_that_has_already_passed(self, js):
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (when > now) return when;" in body

    def test_empty_days_previews_as_every_day(self, js):
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "[0, 1, 2, 3, 4, 5, 6]" in body


class TestDayPickingIsHardToGetWrong:
    def test_presets_exist(self, js):
        for preset in ("weekdays", "weekend", "every"):
            assert r"editorPreset(\'%s\')" % preset in js

    def test_weekdays_means_monday_to_friday(self, js):
        body = re.search(r"function editorPreset\(which\) \{.*?\n  \}", js, re.S).group(0)
        assert "which === 'weekdays' ? [0, 1, 2, 3, 4]" in body

    def test_weekend_means_saturday_and_sunday(self, js):
        body = re.search(r"function editorPreset\(which\) \{.*?\n  \}", js, re.S).group(0)
        assert "which === 'weekend' ? [5, 6]" in body

    def test_selected_days_are_visually_distinct(self, css):
        assert ".daybtn.on" in css

    def test_day_buttons_report_their_state_to_a_screen_reader(self, js):
        assert "aria-pressed=" in js


class TestScheduleFromAnywhere:
    """Picking the music was the hardest part of the old flow and had the
    worst control: a dropdown whose escape hatch was a JS prompt asking for a
    URI copied out of the Spotify desktop app."""

    def test_a_track_can_be_scheduled_from_search(self, js):
        row = re.search(r"function trackRow\(item, nested\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_an_album_can_be_scheduled_from_search(self, js):
        row = re.search(r"function renderAlbumRow\(a\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_a_station_can_be_scheduled(self, js):
        row = re.search(r"function renderStations\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_what_is_playing_can_be_scheduled(self, markup, js):
        assert "scheduleNowPlaying()" in markup
        assert re.search(r"function scheduleNowPlaying\(\).*?fetch\('/nowplaying'\)", js, re.S)

    def test_nothing_playing_is_reported_rather_than_scheduling_undefined(self, js):
        body = re.search(r"function scheduleNowPlaying\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (!data || !data.uri)" in body

    def test_it_arrives_prefilled_so_only_the_time_is_left_to_pick(self, js):
        body = re.search(r"function openEditor\(routine, prefill\) \{.*?\n  \}", js, re.S).group(0)
        assert "EDITOR.label = prefill.name" in body
        assert "uri: prefill.uri" in body

    def test_a_uri_from_search_stays_selected_in_the_dropdown(self, js):
        """It is in neither the playlist nor the station list, so without this
        the step silently resets to 'choose a playlist'."""
        body = re.search(r"function editorStepOptions\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (step.uri && !known)" in body
        assert "selected" in body


class TestRoutinesReadAsMusicNotUris:
    def test_steps_name_the_playlist(self, js):
        body = re.search(r"function describeStep\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "sourceName(step.uri)" in body
        assert "step.uri || ''" not in body


class TestTheEditorIsUsableOnAPhone:
    def test_it_is_a_bottom_sheet_by_default(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "position: fixed" in rule
        assert "inset: auto 0 0 0" in rule

    def test_it_becomes_a_centred_dialog_on_a_wide_screen(self, css):
        assert re.search(r"@media \(min-width: 760px\) \{\s*\.editor \{", css)

    def test_it_can_scroll_when_a_routine_has_many_steps(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "overflow-y: auto" in rule

    def test_it_clears_the_home_indicator(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "env(safe-area-inset-bottom)" in rule

    def test_it_is_announced_as_a_dialog(self, markup):
        block = markup[markup.index('id="editor"') - 200:markup.index('id="ed-body"')]
        assert 'role="dialog"' in block
        assert 'aria-modal="true"' in block

    def test_the_scrim_sits_under_the_editor_and_over_the_page(self, css):
        scrim = int(re.search(r"\.scrim \{[^}]*z-index: (\d+)", css).group(1))
        editor = int(re.search(r"\.editor \{[^}]*z-index: (\d+)", css).group(1))
        assert scrim < editor


class TestATitleCannotBreakOutOfTheHandler:
    """A track name now reaches an onclick attribute, which it never did
    before -- it was only ever text content. escapeHtml is not enough there:
    it turns an apostrophe into &#39;, and the HTML parser decodes that back
    to a real quote inside the attribute, ending the JS string early.
    "Livin' on a Prayer" would be enough to break it.
    """

    CALLERS = ["trackRow", "renderAlbumRow", "renderStations"]

    def _call(self, js, fn):
        body = re.search(r"function %s\(.*?\n  \}" % fn, js, re.S).group(0)
        line = re.search(r"scheduleUri.{0,200}", body, re.S)
        assert line, "no scheduleUri call in " + fn
        return line.group(0)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_uri_is_escaped(self, js, fn):
        assert "escapeHtml" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_name_is_percent_encoded(self, js, fn):
        assert "encodeURIComponent" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_apostrophe_is_encoded_too(self, js, fn):
        """encodeURIComponent alone is not enough: ' is in its unreserved set
        (A-Za-z0-9 -_.!~*'()) and comes through untouched."""
        assert r"replace(/'/g, '%27')" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_escapehtml_alone_is_not_relied_on_for_the_name(self, js, fn):
        call = self._call(js, fn)
        assert not re.search(r"escapeHtml\(\s*(String\()?\w+\.name", call)

    def test_the_receiver_decodes_it(self, js):
        assert "decodeURIComponent(encodedName)" in js

    # JS encodeURIComponent leaves exactly these unescaped. The apostrophe
    # being among them is the whole reason for the extra replace.
    JS_UNRESERVED = "!~*'()"

    def _encode_like_js(self, name):
        from urllib.parse import quote
        return quote(name, safe=self.JS_UNRESERVED).replace("'", "%27")

    def test_plain_encoding_would_have_let_an_apostrophe_through(self):
        """Guards the reason for the replace, so nobody removes it as noise."""
        from urllib.parse import quote
        assert "'" in quote("Livin' on a Prayer", safe=self.JS_UNRESERVED)

    def test_no_quote_character_survives_encoding(self):
        hostile = """Livin' on a Prayer</b>');alert(1);//"""
        encoded = self._encode_like_js(hostile)
        assert "'" not in encoded and '"' not in encoded and "<" not in encoded

    def test_the_apostrophe_survives_the_round_trip(self):
        """Stripping quotes would have been safe too, but would have turned
        "Rock 'n' Roll" into "Rock n Roll" in the routine name."""
        from urllib.parse import unquote
        for name in ["Livin' on a Prayer", "Rock 'n' Roll", 'He said "hi"']:
            assert unquote(self._encode_like_js(name)) == name
